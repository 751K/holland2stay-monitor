"""``close()`` 之后必须确认 profile 目录真的空了。

2026-09-09 生产事故的根因。链条是这样的：

    1. 连接断了（Chromium 被 OOM 杀 / 驱动崩了 / 代理把页面拖死）
    2. close() 里 `self._browser.close()` 外面包着 `except: pass`——对着一根断掉
       的管子说话，不报错，也不生效，进程照旧活着
    3. close() 紧接着 _release_lock()，槽位放了
    4. 下一次 _open_browser 拿到同一个目录，_clear_stale_singleton_locks 把还活
       着那个实例的单实例锁删掉
    5. 两个 Chromium 同时开同一个 profile → 很快又死 → 回到第 1 步

实测：常驻浏览器每次「就绪」5–6 分钟就死，一次泄漏一整套（node 驱动 + 整棵进程
树）。18 小时泄漏 5 套，容器内存 1.14 → 1.73 GiB（上限 2 GiB），随后 xior 渲染器
卡死 600 秒、Holland2Stay 熔断。

所以断言的不是「调了 close()」，是「目录真的没人占了」。
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

import browser_fetcher as bf


def _spawn_holding(path) -> subprocess.Popen:
    """起一个假装开着这个 profile 的进程：命令行里带 --user-data-dir=<path>。

    用真进程而不是打桩，是因为被测的正是「进程到底还在不在」——桩掉进程查询，
    这条测试就退化成「它调了我打的桩」，那正是原缺陷能通过测试的方式。
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)",
         f"--user-data-dir={path}"],
    )


class TestFindingProcesses:

    def test_it_finds_a_process_holding_the_profile(self, tmp_path):
        p = _spawn_holding(tmp_path / "prof-0")
        try:
            for _ in range(50):
                if p.pid in bf._pids_using_profile(tmp_path / "prof-0"):
                    break
                time.sleep(0.1)
            assert p.pid in bf._pids_using_profile(tmp_path / "prof-0")
        finally:
            p.kill(); p.wait()

    def test_it_does_not_match_a_different_profile(self, tmp_path):
        """只认自己那个目录——误伤别的浏览器比不回收更糟。"""
        p = _spawn_holding(tmp_path / "prof-0")
        try:
            time.sleep(0.4)
            assert bf._pids_using_profile(tmp_path / "prof-1") == []
        finally:
            p.kill(); p.wait()

    def test_empty_profile_yields_nothing(self, tmp_path):
        assert bf._pids_using_profile(tmp_path / "nobody") == []


class TestReaping:

    def test_a_survivor_is_killed(self, tmp_path):
        prof = tmp_path / "prof-0"
        p = _spawn_holding(prof)
        try:
            for _ in range(50):
                if bf._pids_using_profile(prof):
                    break
                time.sleep(0.1)
            assert bf._pids_using_profile(prof), "没起来，这条测试什么也没测到"

            bf._reap_profile(prof)
            assert bf._pids_using_profile(prof) == [], "close() 之后进程还活着"
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_reaping_an_empty_profile_is_a_noop(self, tmp_path):
        assert bf._reap_profile(tmp_path / "nobody") == 0

    def test_it_warns_so_the_leak_is_visible(self, tmp_path, caplog):
        """泄漏必须留下痕迹。上一次就是因为没有，才靠数进程才发现。"""
        import logging
        prof = tmp_path / "prof-0"
        p = _spawn_holding(prof)
        try:
            for _ in range(50):
                if bf._pids_using_profile(prof):
                    break
                time.sleep(0.1)
            with caplog.at_level(logging.WARNING, logger="browser_fetcher"):
                bf._reap_profile(prof)
            assert "仍有" in caplog.text and "强制回收" in caplog.text
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()


class TestCloseActuallyReaps:
    """接线，**用行为测，不看源码**。

    第一版是 `inspect.getsource(close)` 里找 "_reap_profile" 这个子串，还比较它
    和 "_release_lock" 的先后。两个变异（把回收整行删掉、把回收挪到放锁之后）都
    没被抓到——因为 close() 里有一行**注释**提到了 _reap_profile，子串在、位置也
    在前面。断言匹配到散文上，等于没测。今天第三次栽在这个形状上。
    """

    def _fetcher_with_dead_connection(self, prof):
        """一个「连接已经断了」的 BrowserFetcher：close() 会抛，进程还活着。

        这正是生产里的形状——close() 对着断掉的管子说话。
        """
        f = bf.BrowserFetcher(headless=True)

        class _DeadBrowser:
            def close(self_inner):
                raise RuntimeError("Target page, context or browser has been closed")

        f._browser = _DeadBrowser()
        f._page = None
        f._profile_path = prof
        f._profile_lock = open(prof.parent / "slot.lock", "w")
        return f

    def test_close_kills_the_survivor(self, tmp_path):
        prof = tmp_path / "prof-0"
        prof.mkdir()
        p = _spawn_holding(prof)
        try:
            for _ in range(50):
                if bf._pids_using_profile(prof):
                    break
                time.sleep(0.1)
            assert bf._pids_using_profile(prof), "没起来，这条测试什么也没测到"

            self._fetcher_with_dead_connection(prof).close()

            assert bf._pids_using_profile(prof) == [], (
                "close() 之后 Chromium 还活着——槽位却已经放了，"
                "下一个人会在同一个目录上再开一个"
            )
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_the_slot_is_still_held_while_reaping(self, tmp_path, monkeypatch):
        """回收必须在放锁**之前**。

        顺序反了的话回收照做、日志照打，但槽位已经被下一个线程抢走了——两个
        Chromium 开同一个 profile 的窗口原样存在，而这条链子正是靠这个窗口
        自我维持的。
        """
        prof = tmp_path / "prof-0"
        prof.mkdir()
        seen = []
        real_release = bf._release_lock
        monkeypatch.setattr(
            bf, "_release_lock",
            lambda h: (seen.append(bf._pids_using_profile(prof)), real_release(h))[1])

        p = _spawn_holding(prof)
        try:
            for _ in range(50):
                if bf._pids_using_profile(prof):
                    break
                time.sleep(0.1)
            assert bf._pids_using_profile(prof)

            self._fetcher_with_dead_connection(prof).close()

            assert seen, "根本没放锁"
            assert seen[0] == [], (
                f"放锁的那一刻 profile 上还有活进程 {seen[0]}——窗口就是这么开的"
            )
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_close_still_releases_the_slot(self, tmp_path):
        """回收不能把放锁挤掉：扣着槽位会让所有人退回临时 profile，流量翻几倍。"""
        prof = tmp_path / "prof-0"
        prof.mkdir()
        f = self._fetcher_with_dead_connection(prof)
        handle = f._profile_lock
        f.close()
        assert handle.closed, "槽位锁没放"
        assert f._profile_lock is None and f._profile_path is None
