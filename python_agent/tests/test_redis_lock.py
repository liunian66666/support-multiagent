# -*- coding: utf-8 -*-
"""[ours] S6 治理：带持有者校验的 Redis 锁单测（内存假 Redis，无外部依赖）。

覆盖：抢锁互斥、release 只删自己的锁（他人锁不误删）、token 缺失安全、
Redis 不可用时 fail-closed。
"""
import unittest

from app.infra.redis_lock import acquire_lock, release_lock


class FakeRedis:
    """只实现 set(nx,ex)/get/delete/eval，够锁语义用。"""
    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def eval(self, script, numkeys, key, arg):
        # 复刻 compare-and-delete 语义
        if self.store.get(key) == arg:
            self.store.pop(key, None)
            return 1
        return 0


class FakeDownRedis(FakeRedis):
    async def set(self, *a, **k):
        raise ConnectionError("redis down")


class RedisLockTest(unittest.IsolatedAsyncioTestCase):
    async def test_acquire_returns_token_and_is_exclusive(self):
        rc = FakeRedis()
        t1 = await acquire_lock(rc, "k", 60)
        self.assertTrue(t1)
        self.assertIsNone(await acquire_lock(rc, "k", 60))  # 已被占

    async def test_release_only_clears_own_lock(self):
        rc = FakeRedis()
        mine = await acquire_lock(rc, "k", 60)
        # 模拟他人锁覆盖（A 的锁过期后 B 抢占）：A 用旧 token release 不得删 B
        await rc.set("k", "other-token")  # B 抢到锁
        await release_lock(rc, "k", mine)  # A 误放自己的旧 token
        self.assertEqual(await rc.get("k"), "other-token")  # B 的锁还在

    async def test_release_clears_when_still_holder(self):
        rc = FakeRedis()
        token = await acquire_lock(rc, "k", 60)
        await release_lock(rc, "k", token)
        self.assertIsNone(await rc.get("k"))

    async def test_release_none_token_noop(self):
        rc = FakeRedis()
        await acquire_lock(rc, "k", 60)
        await release_lock(rc, "k", None)
        self.assertIsNotNone(await rc.get("k"))  # 没 token 绝不误删

    async def test_acquire_fail_closed_on_redis_down(self):
        self.assertIsNone(await acquire_lock(FakeDownRedis(), "k", 60))

    async def test_acquire_none_redis(self):
        self.assertIsNone(await acquire_lock(None, "k", 60))


if __name__ == "__main__":
    unittest.main()
