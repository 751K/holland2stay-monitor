"""预订链路必须走代理池。

2026-08-03 实测踩到的问题：抓取侧（``scrapers.ourdomain``）一直走代理，预订侧
（``bookers.rentcafe``）却在用裸 ``req.Session()`` 直连。从同一个出口 IP 连着
跑几轮预订之后，``rcformsave.ashx`` 的 **POST 开始整片 403，而 GET 仍然正常**
——WAF 按「IP × 写接口」限流，等 7 分钟都不放行。

也就是说整条链路上最要紧、最不能失败的那一步，恰好被放在了最容易被限流的
位置上。

另一条契约同样重要：**一条会话固定一个出口 IP**。RENTCafe 的流程状态存在服务端
会话里，中途换 IP 可能被直接判失效。换 IP 只发生在 ``open()`` 重建会话的时候
（那时本来就要从头来过）。
"""
from __future__ import annotations

import pytest

from bookers.rentcafe import RentCafeSession


@pytest.fixture
def proxied(monkeypatch):
    """让代理池返回一串可辨认的地址。"""
    seen: list[dict] = []

    def _fake(source="", *, rotating=False):
        seen.append({"source": source, "rotating": rotating})
        return f"http://pool-{len(seen)}.test:8080"

    monkeypatch.setattr("config.get_proxy_url", _fake)
    return seen


class TestSessionGoesThroughProxy:
    def test_session_carries_the_proxy(self, proxied):
        s = RentCafeSession("k", source="xior")._new_session()
        assert s.proxies == {
            "https": "http://pool-1.test:8080",
            "http": "http://pool-1.test:8080",
        }

    def test_asks_the_pool_for_this_source(self, proxied):
        RentCafeSession("k", source="ourdomain")._new_session()
        assert proxied[0]["source"] == "ourdomain"

    def test_requests_a_rotating_exit(self, proxied):
        """每条预订会话都该拿一个新出口——粘住上一轮那个正是被烧掉的那个。"""
        RentCafeSession("k")._new_session()
        assert proxied[0]["rotating"] is True

    def test_each_new_session_gets_a_fresh_exit(self, proxied):
        """open() 换 TLS 指纹重建会话时顺带换 IP——免费的逃生口。"""
        booker = RentCafeSession("k")
        a = booker._new_session()
        b = booker._new_session()
        assert a.proxies != b.proxies


class TestNoProxyConfigured:
    def test_falls_back_to_a_direct_session(self, monkeypatch):
        """没配代理时直连，而不是崩掉——本地开发和自建部署都可能没有代理池。"""
        monkeypatch.setattr("config.get_proxy_url", lambda *a, **k: "")
        assert RentCafeSession("k")._new_session().proxies == {}


class TestBookerPassesItsSource:
    def test_xior_booker_tags_the_session_with_its_source(self):
        """代理池按 source 分配/冷却出口，标错等于和抓取侧抢同一个 IP。"""
        from bookers.rentcafe import XiorBooker

        assert XiorBooker.source == "xior"
        assert RentCafeSession("k", source="xior")._source == "xior"
