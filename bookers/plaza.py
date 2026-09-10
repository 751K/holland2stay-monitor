"""
bookers/plaza.py — Plaza (newnewnew.space) 自动应征
====================================================

侦察全文见 ``docs/PLAZA.md``（2026-09-10，真实账号实测）。本文件只记与实现直接
相关的几条，以及三个**容易写错且错了不报错**的地方。

为什么 Plaza 能做到另外两个 RENTCafe 平台做不到的
--------------------------------------------------
提交只有两个 id：

    POST /portal/object/frontend/react/format/json
    add=<toewijzingId>&dwellingID=<objectId>

没有表单、没有证件上传、没有 IBAN、没有支付方式。Xior / OurDomain 卡在「走到
Save 之后要填 IBAN」（代填金融凭据是硬限制），H2S 要用户先绑好支付方式；Plaza
两样都不需要。资料在**注册**阶段一次性交给站点（€27.50/年），booker 不碰。

也没有 Cloudflare、没有 captcha——H2S 那套预热浏览器 + 过挑战 + 借 lane 的复杂度
一条都用不上，纯 HTTP 即可。

⚠️ 三个坑
---------
**1. Content-Type 发错是静默失败。**
``getallobjects`` 吃 JSON，**其余 portal 端点只吃 form-urlencoded**。发 JSON 会
返回 HTTP 200 + 合法 JSON + 只有 ``sAngularServiceData`` 没有 ``result`` + 零报错。
也就是「参数发错了」和「这个账号什么都反应不了」长得一模一样。侦察时在这上面绕了
四五轮，先后怀疑过没登录、没付费、要传 objectId——都不是。
所以 :func:`_portal_post` **断言 ``result`` 存在**，缺了就抛，不 fail-open 成空。

**2. 下单参数原样回传——但那只是 body 的一半。**
``reactionData.url`` 是服务端给的 query string（``?add=11882&dwellingID=16613``），
原样解析回传即可，不要按 ``toewijzingId`` + ``objectId`` 手拼：DTH 那批多一个
``redirect=1``，手拼会漏，而漏参数多半不报错、只会落到不对的分支。

**另一半是提交信封。** 2026-09-10 抓到真实请求才发现：

    __id__=Portal_Form_SubmitOnly&__hash__=672e…&add=11884&dwellingID=16626

前两个不在 ``reactionData.url`` 里，来自 :data:`FORM_CONFIG_URL`。本文件的第一版
只回传后两个——那样的 POST 不会成功。「原样回传服务端给的参数」是必要条件，
不是充分条件。

**3. ``kanReageren`` 不等于「能应征」，要连 ``action`` 一起看。**
它回答的是「能不能操作」。**已经应征过的房源它仍然是 true**，因为确实可以操作——
那个操作是撤回：``action: "remove"``、``url: "?remove=<reactionId>&dwellingID=…"``。

只看 ``kanReageren`` 的实现会预检通过、然后把 url 原样回传 = POST 一个 remove，
**静默撤销用户已有的应征**，再报一句「已提交但回查不到」。2026-09-10 端到端第一次
跑就撞上了；在那之前所有单元测试都是绿的，因为测试替身里 ``action`` 永远是 ``"add"``。

所以有两道闸：``action`` 必须是 ``add``，且解析出来的参数里必须有 ``add``、不能有
``remove``。两个信号都来自服务端且互相独立，同时要求才不可能出现「以为在应征、
实际在撤回」。

``kanReageren`` 为 false 时**绝不发写请求**；未知的 ``redenMagNietReagerenCode``
一律当作不能应征（fail-safe），没有完整清单。

登录是两步
----------
``POST {PROXY}/v1/oauth/token``（凭据换 token）→
``POST /portal/account/frontend/loginbyservice/format/json``（token 换 portal 会话）。

第二步没有 body，作用是让**旧 portal 端点**认这个会话。只做第一步的话
``getobject`` 会返回一个 ``loggedin: false`` 的 reactionData，看起来像"这条不能
应征"，实际是"你没登录"——这两者的 ``kanReageren`` 都是 false，靠它区分不了，
所以 :meth:`_reaction_data` 单独检查 ``loggedin``。

token 请求的字段名是验过的（2026-09-10）
----------------------------------------
没有采集真实登录请求的 body（里面是明文密码），改用**形状探针**：拿一个明显不存在
的账号打这个端点，看它报什么错。

    {client_id, grant_type: "password", username, password}
        → invalid_grant  "The user credentials were incorrect."
    漏掉 grant_type / 写成别的值
        → unsupported_grant_type  "...Check that all required parameters..."

第一种错误说明请求形状**被完全接受**、一路走到了校验凭据那一步；两者可区分，所以
这不是"没报错就当对了"。响应里取 ``access_token``——字段名来自 bundle 里消费这同一
个端点的 refresh 分支（它解构 ``{access_token, refresh_token}``）。

``react`` 的真实形状（2026-09-10 抓包）
--------------------------------------
用户自己在浏览器里点了一次 Reply，抓下来的请求与响应：

请求头只要三个，**没有 CSRF 头、没有 referer 要求**——防重放靠 body 里的
``__hash__``：

    Accept: application/json, text/plain, */*
    Content-Type: application/x-www-form-urlencoded; charset=UTF-8
    X-Requested-With: XMLHttpRequest

响应顶层是 ``success`` / ``reactionData`` / ``reactionId`` / ``messages`` /
``numberOfReactions``——**没有 ``result``**。所以 :meth:`_portal_post` 的 ``expect``
必须能关掉；写死成 ``result`` 会把一次**成功**的应征抛成传输错误，那比失败更糟：
房子应征上了，用户收到的却是「失败」，于是既不去站点确认，也不撤回。

即便如此，成功与否仍以回查 ``getactievereacties`` 为准（:meth:`_has_active_reaction`），
``success: False`` 只用来提前短路。响应里的 ``reactionData`` 是**提交之后**的状态
（``kanReageren`` 变 false、原因码 ``WINKEL-REACTIE-DUBBEL``），别拿它当提交前的判断。

DTH 不自动应征
--------------
见 :data:`_DTH_MESSAGE`。一句话：那句「不再收到其它 offer」的范围站点没写明，
在事实未知时两边代价不对称，所以不按。**刻意没做成开关。**

端到端已验证（2026-09-10）
--------------------------
用真实账号跑通了全部三条路径：

- ``action="remove"``（已应征过）→ 提前返回 success，**不发写请求**
- ``action="add"`` + ``dry_run`` → 预检通过、信封取到、停在提交前
- ``action="add"`` + 真提交 → 应征成立

``plaza`` 仍**不在** ``monitor._AUTO_BOOK_SOURCES`` 里——那是**产品开关**，不是技术
门槛：打开之后系统会替用户自动应征。凭据本身是第二道开关
（``monitor._can_auto_book``），没配 ``plaza_username`` / ``plaza_password`` 的用户
不受影响。

门户配置里的 ``maxAantalReacties = "0"`` **是「不限」**（读 bundle 的
``reactiePlaatsenMogelijk``：``0 < maxAantalReacties`` 是启用上限的前提，为 0 则不启用），
与实测一致——挂着 5 条在跑的应征时新房源仍可应征。它是前端的闸，服务端有没有自己的
上限没测过；真撞上会表现为一个我们没见过的原因码，booker 对未知码 fail-safe。
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import parse_qsl

from booker import BookingResult

from .base import AbstractBooker, BookingRequest

logger = logging.getLogger(__name__)

BASE_URL = "https://plaza.newnewnew.space"
#: Hexia REST 挂在这个前缀后面，**不在根路径上**。侦察时打 ``{BASE}/v1/reactie``
#: 得到 404，据此断言过「这个部署没开 Hexia API」——错的，只是差这个前缀。
PROXY_API = f"{BASE_URL}/portal/proxy/frontend/api"

TOKEN_URL = f"{PROXY_API}/v1/oauth/token"
LOGIN_BY_SERVICE_URL = f"{BASE_URL}/portal/account/frontend/loginbyservice/format/json"
OBJECT_URL = f"{BASE_URL}/portal/object/frontend/getobject/format/json"
#: 提交信封（``__id__`` / ``__hash__``）的来源。**GET**，而且返回的是 ``form``
#: 不是 ``result``。见 :meth:`PlazaBooker._form_envelope`。
FORM_CONFIG_URL = (
    f"{BASE_URL}/portal/core/frontend/getformsubmitonlyconfiguration/format/json")
REACT_URL = f"{BASE_URL}/portal/object/frontend/react/format/json"
ACTIVE_REACTIONS_URL = (
    f"{BASE_URL}/portal/registration/frontend/getactievereacties/format/json")

#: 站点前端自报的 OAuth client（读 bundle：``{clientId: "wzp"}``）。
CLIENT_ID = "wzp"

_TIMEOUT = 30

#: 已知的「不能应征」原因码 → (phase, 人话)。
#:
#: 只有第一个是实测见过的。其余是从站点 ``gettranslations`` 里认出来的同族码，
#: **没有一个被真正触发过**——所以映射表之外的码走 fail-safe 分支，不猜。
_REASON_PHASES: dict[str, tuple[str, str]] = {
    "WINKEL-REACTIE-NIETMEERGEPUBLICEERD": (
        "race_lost", "这条广告已经不再发布（多半是被别人先应征了或已下架）"),
    # 2026-09-10 实测：这个码出现在**提交的响应**里（提交完那一刻的 reactionData）。
    # 稳态的预检里**不会**看到它——已应征的房源预检给的是 action="remove"，
    # kanReageren 仍为 true。所以「已经应征过」的判据是 action，不是这个码；
    # 留着这条映射只是为了它万一真出现在预检里时不被当成未知码。
    "WINKEL-REACTIE-DUBBEL": ("success", "这个账号已经应征过这条房源"),
}


def is_dth(obj: dict) -> bool:
    """这条房源是不是 DTH（站点标题 "Eerste reactie"）。

    **判据用服务端的原始布尔，不用 label 文案。** ``reactionData.label`` 是本地化的
    ——荷兰语界面是 "Boeken"、英文界面是 "Reply"，按文案判在英文界面下会把 DTH
    当成普通房源。``model.modelCategorie.code`` 也不行：DTH 那批的 modelCategorie
    整个是 null（见 ``scrapers/plaza.py`` 的 ``_EXTRA_AANBOD_NOTE``）。
    """
    model = (obj or {}).get("model") or {}
    return bool(model.get("advertentieSluitenNaEersteReactie"))


#: 为什么 DTH 不自动应征——**这是一条刻意的产品决定，不是没做完。**
#:
#: DTH 应征前会弹一个确认框：「Wil je deze kamer definitief boeken? Dat betekent dat
#: je deze kamer accepteert en **geen andere aanbieding meer krijgt**。」
#:
#: 问题在于**没人知道那句话管多大范围**：是只管这一轮，还是会连带影响用户手上其它
#: 在跑的应征。站点文案里查不到（2026-09-10 把 3595 条 gettranslations 全搜过，
#: 除了确认框本身没有第二处提到它），而这一条恰恰决定了代价有多大。
#:
#: 在事实未知的情况下，两边的代价不对称：
#:
#:   不自动应征  少自动化一类房源——而这类当前一条都够不着（在架的 29 条 DTH
#:               全是额外供给，已被判成 Not available）。用户自己点一下即可。
#:   照常应征    可能自动放弃用户手上**全部**其它 offer，中间没有任何人工介入。
#:
#: **刻意不做成用户开关。** 开关的前提是用户能做出知情选择，但这里谁都不知道那句话
#: 的范围——给一个开关等于把我们的无知包装成用户的同意。等哪天真出现一条在架的
#: DTH、能实际观察到行为了，再回来改这里。
#:
#: 用 ``unsupported``：``book_with_fallback`` 会把这类候选过滤掉，不产生「预订失败」
#: 的误报通知；用户照常收到这条房源的普通通知，里面带着 Allocation 特征
#: （``scrapers/plaza.py`` 把站点那句原话写进去了），他自己判断要不要点。
_DTH_MESSAGE = (
    "这是一条 DTH（「Eerste reactie」）房源：应征前站点会要求确认「definitief "
    "boeken」——接受这一套，并且不再收到其它 offer。那句话的具体范围站点没有说明，"
    "所以系统不替你按下去。请点通知里的链接自己决定。"
)


class PlazaLoginError(RuntimeError):
    """凭据被平台拒了。与网络错误分开——重试和换 IP 都救不回来。"""


class PlazaTransportError(RuntimeError):
    """传输层 / 协议层出错（含「响应里没有 result」）。"""


def _object_id(listing_id: str) -> str:
    """``pz_16613`` → ``16613``。

    scraper 给所有 Plaza 房源加了 ``pz_`` 前缀（``scrapers/plaza.py`` 的
    ``id=f"pz_{oid}"``）。站点端点只认裸 id，带着前缀发过去会被当成未知对象。
    """
    lid = (listing_id or "").strip()
    return lid[3:] if lid.startswith("pz_") else lid


class PlazaBooker(AbstractBooker):
    """Plaza 自动应征。纯 HTTP，无浏览器。"""

    source = "plaza"

    # ── HTTP ────────────────────────────────────────────────────────
    @staticmethod
    def _session():
        """与 scraper 同一套代理策略。

        ``get_proxy_url`` 返回空串时必须显式给 ``NO_PROXY_CURL``——curl 拿到空字典
        会回落到 ``HTTPS_PROXY`` 环境变量，也就是回到那个刚被判定为失效的代理。
        """
        import curl_cffi.requests as req

        from config import get_impersonate, get_proxy_url
        from net import NO_PROXY_CURL

        proxy = get_proxy_url("plaza")
        proxies = {"http": proxy, "https": proxy} if proxy else NO_PROXY_CURL
        return req.Session(impersonate=get_impersonate(), proxies=proxies)

    @staticmethod
    def _portal_post(session, url: str, data: Optional[dict] = None,
                     *, expect: str = "result") -> dict:
        """打一个旧 portal 端点，返回 ``expect`` 指定的那个键。

        **form-urlencoded，不是 JSON**，理由见模块 docstring 坑 1。指定的键不存在
        一律抛——那是「请求发错了」，不是「没有数据」。

        ``expect`` 需要参数化是因为 **``react`` 的响应里根本没有 ``result``**
        （2026-09-10 抓包实测，顶层是 ``success`` / ``reactionData`` /
        ``reactionId`` / ``messages``）。写死成 ``result`` 的话，一次**成功**的
        应征会被当成传输错误抛出来——比失败更糟：房子应征上了，用户收到的却是
        「失败」，于是他不会去站点确认，也不会撤回。传 ``expect=""`` 拿整个响应。
        """
        try:
            resp = session.post(
                url, data=data or {}, timeout=_TIMEOUT,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            raise PlazaTransportError(f"{url} 请求失败: {type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            raise PlazaTransportError(f"{url} 返回 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except Exception as e:
            raise PlazaTransportError(
                f"{url} 返回的不是 JSON（{len(resp.content)} 字节）") from e
        if not isinstance(payload, dict):
            raise PlazaTransportError(f"{url} 的响应不是一个对象")
        if not expect:
            return payload
        if expect not in payload:
            raise PlazaTransportError(
                f"{url} 的响应里没有 {expect} 键——多半是 Content-Type 发错了"
                f"（本端点只吃 form-urlencoded），拿到的键：{list(payload)[:6]}")
        return payload[expect]

    # ── 登录 ────────────────────────────────────────────────────────
    def _login(self, session, username: str, password: str) -> None:
        """两步登录，见模块 docstring。失败抛 :class:`PlazaLoginError`。"""
        try:
            resp = session.post(
                TOKEN_URL, timeout=_TIMEOUT,
                json={"client_id": CLIENT_ID, "grant_type": "password",
                      "username": username, "password": password},
                headers={"Accept": "application/json",
                         "Content-Type": "application/json"},
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            raise PlazaTransportError(f"取 token 失败: {type(e).__name__}: {e}") from e

        if resp.status_code in (400, 401, 403):
            raise PlazaLoginError(f"Plaza 拒绝了这组凭据（HTTP {resp.status_code}）")
        if resp.status_code != 200:
            raise PlazaTransportError(f"取 token 返回 HTTP {resp.status_code}")
        try:
            token = (resp.json() or {}).get("access_token") or ""
        except Exception as e:
            raise PlazaTransportError("取 token 的响应不是 JSON") from e
        if not token:
            raise PlazaLoginError("Plaza 没有返回 access_token")

        # 第二步：让旧 portal 端点认这个会话。没有 body。
        try:
            resp = session.post(
                LOGIN_BY_SERVICE_URL, timeout=_TIMEOUT,
                headers={"Accept": "application/json",
                         "Authorization": f"Bearer {token}"},
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            raise PlazaTransportError(
                f"portal 会话建立失败: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise PlazaTransportError(
                f"portal 会话建立返回 HTTP {resp.status_code}")

    # ── 房源侧 ──────────────────────────────────────────────────────
    def _object(self, session, object_id: str) -> dict:
        """取整条房源。**不只要 reactionData**——``model`` 也要用（见 :func:`is_dth`）。"""
        return self._portal_post(session, OBJECT_URL, {"id": object_id}) or {}

    def _reaction_data(self, session, object_id: str, result: dict) -> dict:
        rd = (result or {}).get("reactionData")
        if not isinstance(rd, dict):
            raise PlazaTransportError(
                f"房源 {object_id} 的响应里没有 reactionData")
        # ⚠️ 这道检查是**唯一**挡住「没有会话就发写请求」的东西，不要以为
        # kanReageren 能兜住。2026-09-10 实测（会话掉线后偶然撞见）：
        #
        #     loggedin: false      ← 没登录
        #     kanReageren: true    ← 仍然是 true
        #
        # 站点这两个字段答的是不同问题：kanReageren 说的是「这条广告本身开不开放
        # 应征」，与你是谁无关；loggedin 才是「这个会话认不认你」。匿名访客点下去
        # 得到的是登录弹窗，不是提交。
        #
        # 本文件第一版的注释写的是「两者都为 false，靠 kanReageren 区分不了」——
        # 那是猜的，而且猜反了。只查 kanReageren 的实现会带着一个无效会话一路走到
        # POST react。
        if not rd.get("loggedin"):
            raise PlazaLoginError("会话没有被站点识别为已登录")
        return rd

    def _form_envelope(self, session) -> dict:
        """取 ``__id__`` / ``__hash__``——**每次提交前现取，不缓存。**

        2026-09-10 抓到真实的 react 请求，body 是：

            __id__=Portal_Form_SubmitOnly&__hash__=672e…&add=11884&dwellingID=16626

        前两个**不在** ``reactionData.url`` 里，来自这个单独的端点。所以「原样回传
        服务端给的参数」是必要条件而不是充分条件——信封是另一处来的，漏掉它整个
        POST 就没意义了。

        不缓存的理由：``formService`` 里有一段「提交的响应若带 ``formHash``，就用它
        替换掉下次的 ``__hash__``」（读 bundle），也就是这个令牌会轮换。每次现取
        直接绕开轮换问题，代价是一次 GET。实测同一会话内连取两次值相同，所以
        平时也不会真的多花什么。

        这个端点是 **GET**，返回的是 ``form`` 不是 ``result``。
        """
        try:
            resp = session.get(
                FORM_CONFIG_URL, timeout=_TIMEOUT,
                headers={"Accept": "application/json",
                         "X-Requested-With": "XMLHttpRequest"})
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            raise PlazaTransportError(
                f"取提交信封失败: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise PlazaTransportError(f"取提交信封返回 HTTP {resp.status_code}")
        try:
            form = (resp.json() or {}).get("form") or {}
        except Exception as e:
            raise PlazaTransportError("提交信封的响应不是 JSON") from e

        form_id = form.get("id") or ""
        form_hash = ((form.get("elements") or {}).get("__hash__") or {}).get(
            "initialData") or ""
        if not (form_id and form_hash):
            raise PlazaTransportError(
                f"提交信封不完整：id={form_id!r} hash={'有' if form_hash else '无'}")
        return {"__id__": form_id, "__hash__": form_hash}

    def _has_active_reaction(self, session, object_id: str) -> bool:
        """回头查一遍：这条房源在不在「我的在跑应征」里。

        不解析 ``react`` 的响应——它的形状没验证过（见模块 docstring）。宁可多打
        一次请求，也不信一个没见过的响应体去告诉用户"抢到了"。
        """
        result = self._portal_post(session, ACTIVE_REACTIONS_URL)
        items = (result or {}).get("items") or []
        return any(
            str((it.get("object") or {}).get("id") or "") == str(object_id)
            for it in items if isinstance(it, dict))

    # ── 入口 ────────────────────────────────────────────────────────
    def book(self, request: BookingRequest) -> BookingResult:
        listing = request.listing
        ab = request.user.auto_book
        username = (ab.plaza_username or "").strip()
        password = ab.plaza_password or ""

        def _res(success: bool, message: str, phase: str, **kw: Any) -> BookingResult:
            return BookingResult(listing=listing, success=success, message=message,
                                 phase=phase, dry_run=request.dry_run, **kw)

        if not (username and password):
            return _res(False,
                        "没有配置 Plaza 账号。应征需要一个已注册的 Plaza 账号"
                        "（€27,50/年），在面板里填用户名和密码即可——不需要上传任何资料。",
                        "not_configured")

        object_id = _object_id(listing.id)
        try:
            with self._session() as session:
                self._login(session, username, password)
                obj = self._object(session, object_id)
                rd = self._reaction_data(session, object_id, obj)

                # DTH 一律不自动应征，见 is_dth 的说明。放在预检之前：无论这条
                # 现在能不能应征，我们都不打算替用户按下去。
                if is_dth(obj):
                    return _res(False, _DTH_MESSAGE, "unsupported")

                # ⚠️ ``kanReageren`` 回答的是「能不能操作」，不是「能不能应征」。
                # 已经应征过的房源它**仍然是 true**，因为你确实可以操作——那个操作
                # 是**撤回**：
                #
                #     action: "remove"   url: "?remove=999001&dwellingID=16626"
                #
                # 2026-09-10 端到端跑通的第一次就撞上了这个。只看 kanReageren 的话，
                # 预检会通过，然后把 url 原样回传 = **POST 一个 remove**，
                # 静默撤销用户已有的应征，再报一句「已提交但回查不到」。
                # 毁掉用户要的东西、报告还不说实话，是这个项目最坏的一类 bug。
                action = (rd.get("action") or "").strip().lower()
                if action == "remove":
                    return _res(True,
                                "这个账号已经应征过这条房源了（站点当前给出的动作是"
                                "撤回，说明应征还在）。", "success", pay_url=listing.url)
                if action and action != "add":
                    return _res(False,
                                f"站点给的动作是 {action!r}，不是 add——没侦察过这种"
                                f"情况，不动。", "unknown_error")

                if not rd.get("kanReageren"):
                    code = (rd.get("redenMagNietReagerenCode") or "").strip()
                    phase, why = _REASON_PHASES.get(
                        code,
                        # 未知码 fail-safe：不发写请求，把原码带给用户/日志。
                        ("unknown_error",
                         f"站点拒绝应征，原因码 {code or '(空)'}——这个码还没侦察过"))
                    if phase == "success":
                        # 已经应征过：期望的终态成立。报失败会让上层不停重试一条
                        # 已经到手的候选。
                        return _res(True, why, phase, pay_url=listing.url)
                    return _res(False, f"Plaza 不允许应征这条房源：{why}", phase)

                if request.dry_run:
                    # dry_run 也把信封取掉。它是一次 GET、不产生任何副作用，但
                    # **拿不到信封的话真提交必然失败**——不取的话 dry_run 会说
                    # 「可以应征」而真跑起来在下一步就死，那种 dry_run 等于没验。
                    # 2026-09-10 端到端第一版就是这样：dry_run 通过，而
                    # _form_envelope 这条路从没被真实服务器跑过。
                    envelope = self._form_envelope(session)
                    return _res(True,
                                f"试运行：已登录，站点确认这条房源现在可以应征，"
                                f"提交信封也取到了（{len(envelope)} 项）。未提交。",
                                "dry_run")

                # 服务端给的参数，原样回传（坑 2）。
                params = dict(parse_qsl(
                    str(rd.get("url") or "").lstrip("?"), keep_blank_values=True))
                if not params:
                    raise PlazaTransportError(
                        f"房源 {object_id} 的 reactionData.url 解析不出参数："
                        f"{rd.get('url')!r}")
                # 第二道闸。``action`` 和 ``url`` 是服务端给的两个独立信号，
                # 要求两者都说 add，才不可能出现「以为在应征、实际在撤回」。
                # 只信其中一个的话，上游哪天让它们不一致，我们就会替用户撤单。
                if "remove" in params or "add" not in params:
                    raise PlazaTransportError(
                        f"房源 {object_id} 的提交参数不是一次应征（{params}）——"
                        f"action={action!r}。拒绝提交。")

                # 信封在前、房源参数在后：万一哪天两边键名撞上，以服务端给这条
                # 房源的那份为准。
                payload = {**self._form_envelope(session), **params}
                resp = self._portal_post(session, REACT_URL, payload, expect="")
                if resp.get("success") is False:
                    return _res(False,
                                f"站点拒绝了这次应征：{resp.get('messages') or resp}",
                                "save_rejected")

                if not self._has_active_reaction(session, object_id):
                    return _res(False,
                                "已提交应征，但回查「我的应征」里没有这条——"
                                "请自己去站点确认一次。", "unknown_error")

                return _res(True, "已成功应征这条房源。", "success",
                            pay_url=listing.url)

        except PlazaLoginError as e:
            return _res(False, f"Plaza 登录失败：{e}", "auth_failed")
        except PlazaTransportError as e:
            logger.warning("Plaza 应征 %s 失败: %s", listing.id, e)
            return _res(False, f"Plaza 应征失败：{e}", "unknown_error")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logger.exception("Plaza 应征 %s 未预期异常", listing.id)
            return _res(False, f"Plaza 应征出错：{type(e).__name__}: {e}",
                        "unknown_error")
