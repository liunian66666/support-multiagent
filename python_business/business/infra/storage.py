"""Short-lived MySQL connections and the shared Redis client factory."""

import os

import pymysql
import redis


def db():
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"], port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"], password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"], charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
        connect_timeout=3, read_timeout=5, write_timeout=5,
    )


def cache():
    return redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=3)


def one(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def many(sql, args=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def write(sql, args):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        # [ours] 返回受影响行数，供条件 UPDATE 做 CAS 判定；既有调用方均忽略返回值，零行为影响。
        return cur.rowcount
