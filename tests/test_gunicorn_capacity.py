"""Gunicorn 的线程数是**容量上限**，不是性能调优参数。

2026-09-05 的故障
-----------------
全站打不开。不是崩溃——容器 healthy、日志零 error、CPU 和内存都很闲，表现只是
页面一直转圈。原因是线程池被 SSE 长连接占满：

    SSE_MAX_AGE_SECONDS = 300     每条通知流占住一条线程 5 分钟
    retry: 2000                   客户端断开后 2 秒重连
    --threads=8                   总共 8 条

gthread worker 是一请求一线程，SSE 那条线程 5 分钟里几乎全在 ``stop.wait(5)``
上阻塞——不耗 CPU，但位子占着。稳态下每个在线客户端长期占一条线程，于是
**8 个客户端就把整个站堵死**。当时线上 31 条连接、81 台活跃设备。

原来那行注释写的是「--threads=8：支持多路 SSE 长连接并发」——它没有错，只是把
一个**容量约束**当成了一句描述。8 条线程确实"支持并发"，支持 7 个。

这条测试挡什么
--------------
1. 有人为了"省资源"把线程数调回小值。改完不会有任何测试变红，站也不会立刻挂
   ——要等同时在线的人数超过线程数才挂，而那时症状指向别处（页面慢、超时），
   没有一条日志会说"线程不够"。
2. 有人删掉 ``--timeout=0``。gunicorn 默认 30 秒杀 worker，SSE 全部变成每 30 秒
   断一次；客户端会重连，所以功能"看着还在"，只是通知延迟且服务器负载翻几倍。
3. 有人把 ``--workers`` 调大。SQLite 单写者，多进程会撞写锁。

三条都是「改完当天一切正常」的那种。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISORD = ROOT / "docker" / "supervisord.conf"
NOTIFICATION_SERVICE = ROOT / "app" / "services" / "notification_service.py"

#: 线程数至少要能扛住这么多个同时在线的客户端，再留一截给普通页面请求。
#: 依据是故障当天的真实规模：81 台活跃设备、31 条并发连接。
MIN_THREADS = 64


def _gunicorn_command() -> str:
    text = SUPERVISORD.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("command=gunicorn"):
            return line
    raise AssertionError("supervisord.conf 里找不到 gunicorn 启动行")


def _flag(name: str) -> str:
    m = re.search(rf"--{name}=(\S+)", _gunicorn_command())
    assert m, f"gunicorn 启动行里没有 --{name}"
    return m.group(1)


def test_the_thread_pool_can_hold_more_clients_than_it_did_when_it_fell_over():
    threads = int(_flag("threads"))
    assert threads >= MIN_THREADS, (
        f"--threads={threads}，低于 {MIN_THREADS}。这不是性能参数是容量上限："
        "每个在线客户端的 SSE 流会长期占住一条线程（SSE_MAX_AGE_SECONDS=300），"
        f"所以第 {threads + 1} 个客户端之后所有请求都要排队。"
        "2026-09-05 就是这么全站打不开的——而容器 healthy、日志零 error。")


def test_sse_streams_still_outlive_the_default_worker_timeout():
    """``--timeout=0`` 没了的话，SSE 会被 gunicorn 每 30 秒杀一次。"""
    assert _flag("timeout") == "0", (
        f"--timeout={_flag('timeout')}。gunicorn 默认 30 秒杀 worker，而一条 SSE 流"
        f"要活 {_max_age()} 秒。改成非 0 之后通知流会被反复掐断——客户端会自动重连，"
        "所以功能看着还在，只是延迟变大、连接数翻几倍。")


def test_there_is_still_exactly_one_worker():
    """SQLite 单写者。多进程会撞写锁，而症状是偶发的 'database is locked'。"""
    assert _flag("workers") == "1", (
        f"--workers={_flag('workers')}。SQLite 只有一个写者，多进程写会撞锁，"
        "而报错是偶发的 database is locked，看上去像数据损坏。"
        "要扩并发请加 threads，不是 workers。")


def _max_age() -> int:
    m = re.search(r"^SSE_MAX_AGE_SECONDS\s*=\s*(\d+)",
                  NOTIFICATION_SERVICE.read_text(encoding="utf-8"), re.M)
    assert m, "notification_service.py 里找不到 SSE_MAX_AGE_SECONDS"
    return int(m.group(1))


def test_the_capacity_math_is_still_the_math_the_comment_describes():
    """线程数的依据是「一条 SSE 占一条线程」。这个前提变了，上面的数字就要重算。

    比如把 SSE_MAX_AGE_SECONDS 调到 30，客户端的占用就从"长期"变成"每 30 秒
    抢一次"，容量模型完全不同；反过来调到 3600 会让线程更难释放。
    """
    max_age = _max_age()
    assert max_age >= 60, (
        f"SSE_MAX_AGE_SECONDS={max_age}，比 60 秒还短。流的寿命一短，重连频率就"
        "上去，每次重连都要重新鉴权 + 开一个 storage 连接——线程占用没省下多少，"
        "开销反而涨了。改这个值请连着 docker/supervisord.conf 里的容量说明一起改。")
    assert max_age <= 900, (
        f"SSE_MAX_AGE_SECONDS={max_age}，超过 15 分钟。流活得越久，线程越难释放，"
        f"--threads 就要跟着往上加（当前 {_flag('threads')}）。")
