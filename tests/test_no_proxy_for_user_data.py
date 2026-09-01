"""用户数据的出站请求不许走抓取代理。

HTTPS_PROXY / HTTP_PROXY 是为抓取而设的（住宅代理绕 Cloudflare），但 urllib、
httpx、curl_cffi **三者都默认读这两个环境变量**，于是应用里每一个对外请求都被
顺带塞进了那条隧道：APNs 设备令牌、Telegram bot token、Twilio 手机号、Resend
收件人邮箱、房源地址的地理编码……全部经过 webshare（美国公司，随机住宅出口 IP）。

2026-08-24 这条路径真的出过事：代理账户欠费返回 402，**推送整体中断**，日志里是
「APNs 请求异常: 402 Payment Required」。抓取因代理挂掉是设计内的降级，推送跟着
挂掉不是。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import net

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_proxy(monkeypatch):
    """指向一个必然连不上的代理：走代理就会失败，直连就会成功。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")


class TestHelpersActuallyBypass:
    """这三个入口是本次修复的全部依据，必须逐个证明它们真的直连。"""

    def test_curl_needs_empty_strings_not_empty_dict(self):
        """记录一个不显然的坑：``proxies={}`` 不管用，会回落到环境变量。

        2026-08-24 三种写法逐一实测，只有显式空串真的直连。这条用例存在的意义
        是：谁哪天把它"简化"成 ``proxies={}``，测试会失败而不是静默退回老毛病。
        """
        assert net.NO_PROXY_CURL == {"http": "", "https": ""}
        assert net.NO_PROXY_CURL != {}

    def test_httpx_kwargs_disable_env(self):
        assert net.direct_httpx_kwargs()["trust_env"] is False
        # 调用方自己的参数要保留
        assert net.direct_httpx_kwargs(http2=True)["http2"] is True

    def test_curl_session_carries_the_override(self, fake_proxy):
        s = net.direct_curl_session()
        try:
            assert s.proxies == net.NO_PROXY_CURL
        finally:
            s.close()

    def test_caller_can_still_override(self):
        s = net.direct_curl_session(proxies={"https": "http://x:1"})
        try:
            assert s.proxies == {"https": "http://x:1"}
        finally:
            s.close()


class TestCallSitesUseThem:
    """判定逻辑对了、接线断了，等于没做——项目在这上面栽过好几次。

    这里扫源码而不是发真实请求：发请求要么打到外网，要么得把五个服务全 mock
    一遍，而真正要守的是「没有人新写一个裸客户端」。
    """

    #: 这些模块是在跟**用户 / 第三方服务**说话，不许走抓取代理。
    _USER_DATA = {
        "notifier.py":                    "Telegram / Resend / Twilio",
        "app/email_verify.py":            "Resend（验证邮件）",
        "app/routes/inbound.py":          "Resend（入站邮件）",
        # 2026-08-29 把 Photon 调用从 map_routes 提到了 mcore/geocode.py（监控进程
        # 也要用），守卫的清单当时没跟着搬——map_routes 里已经一个 HTTP 调用都没有，
        # 这条从那天起就是空转，而真正发请求的那个文件没人看着。两个都留着：
        # map_routes 仍是地图页的入口，哪天有人在那儿直接发请求同样要拦。
        "app/routes/map_routes.py":       "photon.komoot.io（地理编码，入口）",
        "mcore/geocode.py":               "photon.komoot.io（地理编码，实际调用）",
        "notifier_channels/apns.py":      "APNs",
        "notifier_channels/fcm.py":       "FCM",
        "tools/geocode_all.py":           "photon.komoot.io",
    }

    #: 这些是在跟**房源平台**说话，本来就该走代理，别误伤。
    _SCRAPING = {
        "app/routes/api_v1/auth.py":      "H2S GraphQL 凭据校验",
        "bookers/rentcafe.py":            "RentCafe 下单",
    }

    @pytest.mark.parametrize("rel,who", sorted(_USER_DATA.items()))
    def test_no_bare_client_in_user_data_paths(self, rel, who):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        bare = []
        # httpx 的**模块级函数**（httpx.post / httpx.get / ...）也要认：它们自带
        # trust_env=True，和裸 Client 一样会从环境读 HTTPS_PROXY。此前只扫构造
        # 函数，fcm.py 里换 token 的那次 httpx.post 因此漏了整整一轮，直到代理
        # 欠费把 Android 推送打挂才暴露（2026-08-28）。
        for m in re.finditer(r"^\s*(?!#).*?\b(req\.Session\(|httpx\.(?:Async)?Client\(|"
                             r"httpx\.(?:post|get|put|patch|delete|head|options|request|stream)\(|"
                             r"(?<!direct_)urlopen\()",
                             src, re.M):
            line = src[m.start():src.index("\n", m.start())].strip()
            # 豁免判断必须看**整个调用**而不是首行：多行写法里
            # ``**direct_httpx_kwargs()`` 往往落在几行之后，只看首行会把已经修好
            # 的调用误报成裸客户端。从左括号起做括号配对，取到调用结束为止。
            open_paren = src.index("(", m.end() - 1)
            depth, i = 0, open_paren
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            call = src[m.start():i + 1]
            if "direct_" in call:
                continue
            bare.append(line[:80])
        assert not bare, (
            f"{rel}（{who}）里还有裸客户端，会走抓取代理：\n  " + "\n  ".join(bare)
            + "\n改用 net.direct_* 系列。"
        )

    @pytest.mark.parametrize("rel,who", sorted(_SCRAPING.items()))
    def test_scraping_paths_are_left_alone(self, rel, who):
        """反向守卫：别把该走代理的也一起关掉，那会让抓取直连服务器 IP 吃 403。"""
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "direct_curl_session" not in src and "direct_httpx_kwargs" not in src, (
            f"{rel}（{who}）是在跟房源平台说话，必须走代理"
        )
