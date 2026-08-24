"""不走抓取代理的出站 HTTP 客户端。

为什么需要这个模块
------------------
``HTTPS_PROXY`` / ``HTTP_PROXY`` 是**为抓取而设**的：住宅代理用来绕 Cloudflare、
拿荷兰出口 IP。但 urllib、httpx、curl_cffi **三者都默认读这两个环境变量**，于是
应用里每一个对外请求都被顺带塞进了那条隧道：

    APNs / FCM   设备推送令牌 + 通知正文
    Telegram     bot token + 消息内容
    Twilio       auth token + 手机号 + 消息
    Resend       收件人邮箱 + 验证链接
    photon       房源地址（地理编码）

三个后果：

1. **隐私**：这些都是用户数据，却经过一家未在隐私条款里列出的第三方
   （webshare，美国公司，出口是随机住宅 IP）。而隐私条款 §16 声称
   「数据在欧盟境内存储和处理」。
2. **可靠性**：2026-08-24 代理账户欠费返回 402，**推送整体中断**——日志里是
   「APNs 请求异常: 402 Payment Required」。抓取因代理挂掉是设计内的降级，
   推送跟着挂掉不是。
3. **没有任何好处**：Apple / Telegram / Resend 都不需要住宅 IP，走代理只是多一跳，
   还让请求来源看起来可疑。

用法
----
凡是**不在跟房源平台说话**的出站请求，都走这里的入口。

跟房源平台说话的保持原样——它们本来就该走代理，而且都是显式传 ``proxies=``
的：``scrapers/``、``bookers/rentcafe.py``、``app/routes/api_v1/auth.py`` 里
校验 H2S 凭据的那处。

⚠️ 一个不显然的坑
-----------------
curl_cffi 关代理**不能靠 ``proxies={}``**，空字典会回落到环境变量；也不认
``trust_env=False``（参数收下了但不生效）。必须显式给空串：

    proxies={"http": "", "https": ""}

2026-08-24 三种写法逐一实测，只有最后一种真的直连。有用例钉住。
"""

from __future__ import annotations

from typing import Any

#: curl_cffi 用。见上面「一个不显然的坑」。
NO_PROXY_CURL: dict[str, str] = {"http": "", "https": ""}

#: httpx 用。``trust_env=False`` 同时会关掉从环境读代理和读 .netrc。
NO_PROXY_HTTPX: dict[str, Any] = {"trust_env": False}


def direct_curl_session(**kwargs: Any):
    """curl_cffi Session，绕开抓取代理。

    调用方仍可传 ``impersonate=`` 等参数；显式传 ``proxies=`` 会覆盖默认值
    （bookers 那种本来就该走代理的场景不该用本函数，但不强行拦）。
    """
    import curl_cffi.requests as req

    kwargs.setdefault("proxies", NO_PROXY_CURL)
    return req.Session(**kwargs)


def direct_httpx_kwargs(**kwargs: Any) -> dict[str, Any]:
    """给 ``httpx.Client`` / ``httpx.AsyncClient`` 的构造参数，绕开抓取代理。"""
    kwargs.setdefault("trust_env", False)
    return kwargs


def direct_urlopen(request: Any, timeout: float = 10.0):
    """``urllib.request.urlopen`` 的直连版。

    空 ``ProxyHandler`` 会让 opener 忽略 ``getproxies()``——健康检查里用的是
    同一招（见 docker-compose.yml 的 healthcheck）。
    """
    import urllib.request as _u

    opener = _u.build_opener(_u.ProxyHandler({}))
    return opener.open(request, timeout=timeout)
