"""通知路径上的四处「一处失败带走全部」。

  fcm  取 token 在 try 外 + gather 无 return_exceptions → 一次 OAuth 失败丢整批
  fcm  device_dead 拿散文 message 当判据 → 永不命中，失效 token 永不清理
  notifier  返回 False 也重试 → 配额拒发记两笔、超时可能重复投递
  monitor   热重载先 close 旧的再构造新的 → 构造抛错则所有渠道永久哑掉
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest


def _code(fn) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln)
                     for ln in inspect.getsource(fn).split("\n"))


class TestFcmBatchSurvivesOneFailure:
    def test_gather_uses_return_exceptions(self):
        """少了它，一台设备抛异常就把整批结果一起丢——包括已经发成功的。"""
        from notifier_channels.fcm import FcmClient

        src = _code(FcmClient.send_many)
        assert "return_exceptions=True" in src

    def test_one_bad_device_does_not_lose_the_batch(self):
        """真跑一次：一台抛异常，其余的结果必须还在。"""
        from notifier_channels.fcm import FcmClient, FcmResult

        class _Cfg:
            concurrency = 4

        class _C(FcmClient):
            def __init__(self):
                self.cfg = _Cfg()

            async def send_one(self, *, device_token, payload, collapse_key=""):
                if device_token == "boom":
                    raise RuntimeError("kaboom")
                return FcmResult(status=200, reason="OK", device=device_token)

        targets = [{"device_token": t} for t in ("a", "boom", "b")]
        out = asyncio.run(_C().send_many(targets, payload={"message": {}}))
        assert len(out) == 3
        assert [r.device for r in out] == ["a", "boom", "b"]
        assert out[0].ok and out[2].ok
        assert not out[1].ok and out[1].status == 0

    def test_token_is_fetched_inside_try(self):
        """取 token 在 try 外的话，OAuth 一失败异常就穿出 send_one。"""
        from notifier_channels.fcm import FcmClient

        src = _code(FcmClient.send_one)
        i = src.index("try:")
        j = src.index("_auth.token")
        assert i < j, "取 token 在第一个 try 之前"

    def test_token_does_not_block_the_event_loop(self):
        """_auth.token() 缓存过期时会走**同步** httpx 换 token。

        直接调会把整个 asyncio 停在这一行——包括其余渠道的推送和 web 的 SSE。

        **真验一次**，不 grep 源码：函数里别处也有 to_thread，grep 版对
        「这一处退回同步调用」抓不住（第一版就是那样，变异当场漏网）。做法是让
        token() 阻塞一段真实时间，同时在事件循环上跑一个心跳——没让出去的话心跳
        会一起卡住。
        """
        import time

        from notifier_channels.fcm import FcmClient

        class _Auth:
            def token(self):
                time.sleep(0.35)          # 同步阻塞，模拟换 token
                return "t"

        class _Client:
            async def post(self, *_a, **_kw):
                raise OSError("not reached")

        c = FcmClient.__new__(FcmClient)
        c._auth = _Auth()
        c._client = _Client()

        class _Cfg:
            project_id = "p"
        c.cfg = _Cfg()

        async def go():
            beats = 0

            async def _heart():
                nonlocal beats
                for _ in range(20):
                    await asyncio.sleep(0.02)
                    beats += 1

            h = asyncio.create_task(_heart())
            await asyncio.sleep(0)          # 让心跳先起跑
            await c.send_one(device_token="d", payload={"message": {}})
            # **在 send_one 返回的那一刻取数。** 先 await h 再取的话心跳会被跑完，
            # 两种情况都是 20 次——第一版就是那么写的，变异当场漏网。
            during = beats
            h.cancel()
            try:
                await h
            except asyncio.CancelledError:
                pass
            return during

        # 让出去的话 0.35s 里心跳能跑十几次；没让出去则一次都跑不了
        assert asyncio.run(go()) >= 8, "同步换 token 把事件循环卡住了"


    def test_401_refresh_also_leaves_the_loop(self):
        """401 之后的强刷走的是同一个同步 _exchange，同样不能直接调。

        主路径那条测试走不到这里（它在拿 token 后就抛了），所以单独覆盖——
        少了这条，把 force_refresh 退回同步调用是漏网的。
        """
        import time

        from notifier_channels.fcm import FcmClient

        class _Auth:
            def token(self):
                return "t"

            def force_refresh(self):
                time.sleep(0.35)
                return "t2"

        class _Resp401:
            status_code = 401

        class _Client:
            def __init__(self):
                self.n = 0

            async def post(self, *_a, **_kw):
                self.n += 1
                if self.n == 1:
                    return _Resp401()
                raise OSError("second call not reached")

        c = FcmClient.__new__(FcmClient)
        c._auth, c._client = _Auth(), _Client()

        class _Cfg:
            project_id = "p"
        c.cfg = _Cfg()

        async def go():
            beats = 0

            async def _heart():
                nonlocal beats
                for _ in range(20):
                    await asyncio.sleep(0.02)
                    beats += 1

            h = asyncio.create_task(_heart())
            await asyncio.sleep(0)
            await c.send_one(device_token="d", payload={"message": {}})
            during = beats
            h.cancel()
            try:
                await h
            except asyncio.CancelledError:
                pass
            return during

        assert asyncio.run(go()) >= 8, "401 强刷把事件循环卡住了"


class TestDeviceDeadUsesMachineReadableCode:
    def test_prose_message_never_counts(self):
        from notifier_channels.fcm import FcmResult

        prose = "The registration token is not a valid FCM registration token"
        assert not FcmResult(status=404, reason=prose, device="t").device_dead

    def test_error_codes_count(self):
        from notifier_channels.fcm import FcmResult

        for code in ("UNREGISTERED", "INVALID_ARGUMENT", "SENDER_ID_MISMATCH"):
            assert FcmResult(status=404, reason=code, device="t").device_dead

    def test_other_failures_are_not_device_dead(self):
        """500 / 429 是我们这边或平台的问题，软停设备是误伤。"""
        from notifier_channels.fcm import FcmResult

        assert not FcmResult(status=500, reason="INTERNAL", device="t").device_dead
        assert not FcmResult(status=429, reason="QUOTA_EXCEEDED", device="t").device_dead


class TestHotReloadKeepsChannelsAlive:
    def test_new_notifiers_are_built_before_closing_old(self):
        """反过来的话，构造一抛错就「继续使用旧配置」——而旧的已经 close 了，
        所有渠道永久哑掉，日志上还写着一切照旧。"""
        import monitor

        src = _code(monitor.main_loop)
        i = src.index("_build_user_notifiers(users)")
        j = src.index("await n.close()")
        assert i < j, "先 close 后构造：构造失败会让所有渠道永久哑掉"

    def test_closing_old_ones_cannot_break_the_reload(self):
        """关旧的失败最多泄漏一个 session，不该连累这次热重载。"""
        import monitor

        src = _code(monitor.main_loop)
        i = src.index("await n.close()")
        assert "except Exception" in src[max(0, i - 200):i + 300]
