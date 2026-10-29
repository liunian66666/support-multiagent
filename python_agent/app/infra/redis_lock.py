# -*- coding: utf-8 -*-
"""[ours] S6 治理：带持有者校验的 Redis 分布式锁（消除"误删他人锁"竞态）。

旧模式缺陷：两端都用固定 value "1"，实例 A 的锁 TTL 到期被 B 抢占后，
A 完成时 delete(lock_key) 会删掉 B 的锁 → 双实例并跑。
本模块：acquire 写入随机 token；release 用 Lua 比对持有者后再删（原子）。
语义与既有行为保持一致的降级面：redis 缺失/异常一律"拿不到锁"（worker 跳过本轮），
release 失败静默（锁随 TTL 自然过期）。
"""
import secrets

# compare-and-delete：只有 value 仍是自己的 token 才删除
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


async def acquire_lock(rc, key: str, ttl: int) -> str | None:
    """SET NX EX 成功返回 token（release 凭据），否则 None。异常视为未获得。"""
    if rc is None:
        return None
    token = secrets.token_hex(8)
    try:
        ok = await rc.set(key, token, nx=True, ex=ttl)
        return token if ok else None
    except Exception:
        return None


async def release_lock(rc, key: str, token: str | None) -> None:
    """Lua 校验持有者后删除；token 为空视为没持锁，直接返回。"""
    if rc is None or not token:
        return
    try:
        await rc.eval(_RELEASE_LUA, 1, key, token)
    except Exception:
        pass  # 删除失败由 TTL 兜底
