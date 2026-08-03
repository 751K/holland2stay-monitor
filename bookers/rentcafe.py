"""bookers/rentcafe.py — RENTCafe automated booking for Xior / OurDomain.

Targets the securerc.co.uk multi-step form flow (oleapplication.aspx →
register/guestlogin → rcformsave.ashx → terms → lease creation).

Usage::

    from bookers.rentcafe import RentCafeBooker
    booker = RentCafeBooker(captcha_api_key="...")
    result = booker.book(request)

Architecture
------------
``RentCafeSession`` is the low-level HTTP + form engine that drives one
booking session.  ``RentCafeBooker`` wraps it in the ``AbstractBooker``
interface expected by the monitor / web panel.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlparse

import curl_cffi.requests as req

from bookers.base import AbstractBooker, BookingRequest, BookingResult
from captcha import CaptchaSolver, CaptchaError, RENTCAFE_V2_SITEKEY, RENTCAFE_V3_SITEKEY
from config import get_impersonate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

# RENTCafe form endpoints (appended to ole path, e.g. .../register.aspx)
_ACTION = "rcformsave.ashx"


class RentCafeError(Exception):
    """Unrecoverable RENTCafe booking error."""


class RentCafeBlockedError(RentCafeError):
    """IP blocked / 403 — retry later with different IP."""


class RentCafeSaveRejectedError(RentCafeError):
    """服务端拒绝保存申请表。

    单独一类，是为了让「草稿没存下」永远不可能被报成 ``draft_saved``——
    那条消息会让用户以为表单已经填好、安心去传证件，而实际什么都没有。
    """


class RentCafeAuthError(RentCafeError):
    """凭据被服务端拒绝。

    和 :class:`RentCafeError` 分开，是因为处置方式完全不同：这个要提示用户去
    面板改这栋楼的账号密码，重试多少次都没用。
    """


# ---------------------------------------------------------------------------
# RentCafeSession — low-level HTTP + multi-step form engine
# ---------------------------------------------------------------------------


class RentCafeSession:
    """Drive one RENTCafe online-leasing session.

    Parameters
    ----------
    captcha_api_key : str
        2Captcha API key.
    """

    def __init__(self, captcha_api_key: str, source: str = "xior") -> None:
        self._solver = CaptchaSolver(captcha_api_key)
        self._source = source
        self._session: req.Session | None = None
        self._base_url: str = ""
        self._ole_path: str = ""
        self._post_url: str = ""  # e.g. https://host/onlineleasing/rcformsave.ashx
        self._cafeportalkey: str = ""
        self._impersonate: str = get_impersonate()
        # 最近一次拿到的页面 HTML。多步流程里每一步都要在下一步之前核对
        # 「我落到哪儿了」，把它存下来省得每步都重新 GET 一次。
        self._last_html: str = ""

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def open(self, apply_url: str) -> dict:
        """打开 ``oleapplication.aspx``，返回页面的隐藏字段。

        永远是第一步：它建立会话 cookie，并取出后续每个请求都要带的
        ``cafeportalkey``。

        TLS 指纹要轮换
        --------------
        SecureRC 的 WAF 按 TLS 指纹拒绝——2026-08-03 实测同一 URL 同一时刻，
        ``chrome124`` / ``chrome136`` / ``safari18_0`` / ``firefox135`` 返 200，
        而 ``chrome131`` / ``edge101`` 返 403。``get_impersonate()`` 是从池里
        随机取的，所以不轮换的话每次预订都有约四分之一的概率一上来就 403。

        直接复用 ``scrapers.ourdomain`` 那套指纹冷却状态机——**打的是同一个
        SecureRC 集群**，某个指纹被烧对抓取和预订同时生效，共享状态等于互相
        提供情报，各记一套反而会重复踩同一个坑。
        """
        from scrapers.ourdomain import (
            _impersonate_attempts, _mark_fingerprint_blocked, _mark_fingerprint_good,
        )

        attempts = _impersonate_attempts() or [self._impersonate]
        resp = None
        tried: list[str] = []
        for idx, impersonate in enumerate(attempts, start=1):
            tried.append(impersonate)
            self._session = self._new_session()
            r = self._session.get(apply_url, impersonate=impersonate, timeout=30)
            if r.status_code != 403:
                _mark_fingerprint_good(impersonate)
                self._impersonate = impersonate   # 后续步骤沿用这个成功的指纹
                resp = r
                break
            _mark_fingerprint_blocked(impersonate)
            if idx < len(attempts):
                logger.warning(
                    "RENTCafe 403，切换 TLS 指纹重试 %d/%d: %s → %s",
                    idx + 1, len(attempts), impersonate, attempts[idx],
                )
        if resp is None:
            # 全部指纹都 403 = 出口 IP 被盯上了，换指纹救不回来。
            # 归类为 blocked（可重试）而不是 unknown_error——后者会让上层
            # 当成代码 bug，而这其实是环境问题。
            raise RentCafeBlockedError(
                f"oleapplication.aspx 全部 TLS 指纹均返回 403（{', '.join(tried)}）。"
                "多半是出口 IP 被 WAF 盯上，换 HTTPS_PROXY 或稍后再试。"
            )

        if resp.status_code != 200:
            raise RentCafeError(
                f"oleapplication.aspx returned {resp.status_code} for {apply_url}"
            )

        self._base_url, self._ole_path = _parse_base_and_path(resp.url)
        self._post_url = f"{self._base_url}/onlineleasing/rcformsave.ashx"
        #: 登录成功后要重新进入的落地页。登录响应体是空的（见 login()），
        #: 里面没有 window.location 可跟，只能自己回到这个入口。
        self._apply_url = str(resp.url)
        self._cafeportalkey = _extract_cafeportalkey(resp.text)

        self._last_html = resp.text
        fields = _extract_hidden_fields(resp.text)
        logger.info(
            "RENTCafe session opened base=%s cpk=%s…",
            self._base_url,
            self._cafeportalkey[:20],
        )
        return fields

    # ------------------------------------------------------------------
    # 这里原本有一个 register()，2026-08-03 删掉了。两条理由：
    #
    # 1. **它是错的。** 实测注册表单的字段是 txtName / txtName2 / txtEmail /
    #    txtPassword / drpCurrentCountry / txtPhone / ddlLanguage /
    #    HowDidYouHear / SubscribeToEmails，外加反自动化的 txtCodeVal /
    #    txtRenderTime / txtvalue1 / txtvalue2。而它发的是 BirthDate 和
    #    Prefer=byEmail —— 这两个字段**根本不存在**；它还把 v2 的 token 塞进
    #    了 v3 的隐藏字段。没有任何调用方，所以这些错误一直没被发现。
    #
    # 2. **按设计它就不该存在。** 账号由用户自己在浏览器里注册，系统只使用
    #    用户填进面板的凭据。批量自动注册账号对平台是滥用。
    # ------------------------------------------------------------------

    #: 密码登录用的表单名。``guestlogin.aspx`` 上一共 4 个表单，别搞混：
    #:
    #:   Login       Username + Password + CheckUserAuth，按钮 Continue  ← 就是它
    #:   UserLogin   Username + reCAPTCHA + otpclickedUserLogin
    #:               → 「Send Verification Code」/「Email a Link to Login」，
    #:                 是 **OTP / 免密登录**那条路，不是密码登录
    #:   OtpOptions  选 OTP 发送方式
    #:   VerifyOTP   6 位验证码
    #:
    #: **密码登录这个表单上没有任何 reCAPTCHA 字段**（实测 2026-08-03）。
    #: 验证码在 UserLogin 那条 OTP 路径上。所以走密码登录不需要解验证码——
    #: 旧实现在这里解了 v2+v3 又塞进不存在的字段，纯属浪费还可能被当异常流量。
    _LOGIN_FORM = "Login"

    def login(self, email: str, password: str) -> dict:
        """用密码登录已有的 RENTCafe 账号。

        页面上看起来是「先填邮箱 → Continue → 再填密码」的两段式，但那只是
        **前端的渐进显示**：``Login`` 表单本身同时含 Username 和 Password。
        实测网络层是两个 POST，都打 ``rcformsave.ashx``，靠 ``CheckUserAuth``
        区分——第一次探测账号是否存在，第二次带密码真正登录。

        这里按同样的顺序发：先探测（拿到服务端更新后的隐藏字段），再登录。
        两步都用服务端下发的字段，不自己编。

        .. note::

           不解 reCAPTCHA：密码登录表单上没有那些字段（见 :attr:`_LOGIN_FORM`）。

        Returns
        -------
        登录后页面的隐藏字段。RENTCafe 会**恢复上次的申请上下文**——账号里有
        未完成的申请时直接落到那一步（实测落到 ApplicantInfo 而不是 Apartments）。
        """
        login_url = f"{self._base_url}{self._ole_path}/guestlogin.aspx"
        logger.info("RENTCafe 登录: %s", login_url)

        resp = self._get(login_url)
        # 只取 Login 表单自己的隐藏字段——整页抓会把另外三个表单的同名字段
        # 混进来（见 _extract_form_fields）。
        fields = _extract_form_fields(resp.text, self._LOGIN_FORM)
        if not fields:
            raise RentCafeError(
                f"登录页上找不到 form#{self._LOGIN_FORM}，页面结构可能已变。"
            )
        cpk = _extract_cafeportalkey(resp.text) or self._cafeportalkey

        base = dict(fields)
        # formName2 **必须原样带回服务端下发的值**（实测是 "mylistlogin"，不是
        # 表单 id）。2026-08-03 踩过：这里曾写成 base["formName2"] = "Login"，
        # 服务端认不出这个表单身份，两次 POST 都回 200 + 空 body，登录静默失败。
        base["cafeportalkey"] = cpk
        base["Username"] = email

        # ① 账号探测。服务端据此决定要不要收密码。
        probe = dict(base)
        probe["CheckUserAuth"] = "1"
        probe_resp = self._post(self._post_url, probe, referer=login_url)
        _raise_if_login_error(probe_resp.text, "账号探测")
        # 探测响应通常是空的（没有错误就没有内容），这是正常的；只有真下发了
        # 表单才更新字段。
        probed = _extract_form_fields(
            probe_resp.text, self._LOGIN_FORM, required=False,
        )
        if probed:
            base.update(probed)
            base["Username"] = email

        # ② 带密码正式登录。CheckUserAuth 在展开密码框时被页面 JS 置为**空串**
        #    （`f['CheckUserAuth'].value=''`），不是 "0"——发 "0" 服务端当成未知
        #    状态，同样静默失败。
        data = dict(base)
        data["Password"] = password
        data["CheckUserAuth"] = ""
        resp2 = self._post(self._post_url, data, referer=login_url)
        _raise_if_login_error(resp2.text, "登录")

        # 登录响应体为空 = 没有错误提示要显示 = 成功（页面 JS 只把返回的 HTML
        # 塞进错误提示框）。因为里面没有 window.location 可跟，得自己回到申请
        # 入口；服务端会话已带上登录态，会恢复到选房那一步。
        followed = self._follow(resp2, login_url)
        self._last_html = followed if _has_unit_list(followed) else self._get(
            getattr(self, "_apply_url", "") or login_url
        ).text
        return self._handle_response(resp2, "login")

    def submit_step(self, step_name: str, form_data: dict, page_path: str = "") -> dict:
        """Submit a generic form step and return the response data.

        Parameters
        ----------
        step_name : str
            Human-readable step name for logging.
        form_data : dict
            Form fields to POST (hidden fields + visible values).
        page_path : str
            The .aspx page name for the Referer header (e.g. ``"ApplicantInfo.aspx"``).

        Returns
        -------
        dict — the server's JSON response (``{"type": "success", ...}``) or the
               hidden fields from the HTML page that follows a redirect.
        """
        if not page_path:
            page_path = "oleapplication.aspx"

        ref = f"{self._base_url}{self._ole_path}/{page_path}"
        # Always inject cafeportalkey if not present.
        if "cafeportalkey" not in form_data:
            form_data["cafeportalkey"] = self._cafeportalkey

        # If this step has reCAPTCHA, solve v2.
        if self._needs_recaptcha(form_data):
            v2 = self._solver.solve_v2(page_url=ref)
            form_data["g-recaptcha-response-v3"] = v2
            form_data["failed-captcha-3"] = "false"

        logger.info("Submitting step '%s' to %s", step_name, self._post_url)
        resp = self._post(self._post_url, form_data, referer=ref)
        return self._handle_response(resp, step_name)

    # ------------------------------------------------------------------
    # 半自动预订：条款 → 选房 → 填申请表 → 存草稿
    #
    # 每一步都**先核对落到了哪里再往下走**。这条流程里任何一步猜错，后果不是
    # 报错而是「在用户真实账号下默默提交一份错的申请」，所以宁可停下来报失败。
    # ------------------------------------------------------------------

    def submit_terms(self, page_fields: dict, *, move_in_date: str = "") -> str:
        """提交第 2 步的条款表单（页面上的 Start Application），返回结果页 HTML。

        这一页是**标准 v3** reCAPTCHA，不是 Enterprise，sitekey 也和登录页不同
        （见 :mod:`captcha.rentcafe_pages`）。用错类型或 sitekey，服务端不认。
        """
        from captcha import page_captcha

        cfg = page_captcha("oleapplication")
        ref = f"{self._base_url}{self._ole_path}/oleapplication.aspx"

        data = dict(page_fields)
        data["cafeportalkey"] = data.get("cafeportalkey") or self._cafeportalkey
        if move_in_date:
            data["sMoveInDate"] = move_in_date
        if cfg and cfg.has_captcha:
            data[cfg.v3_field] = self._solver.solve_v3(
                page_url=ref, action=cfg.action,
                sitekey=cfg.v3_sitekey, enterprise=cfg.is_enterprise,
            )
            data[cfg.fallback_flag] = "false"

        resp = self._post(self._post_url, data, referer=ref)

        # v3 分数不够时服务端不报错，而是回一段要求走 v2 的 JS。实测
        # （2026-08-03）2Captcha 的 v3 token **一次都没过**过这一页的阈值，
        # 所以 v2 回退是常态路径而不是例外——成本要按 v2 算，不是 §8.4 里
        # 那个乐观的 v3 估值。
        if cfg and cfg.has_captcha and _needs_v2_fallback(resp.text):
            logger.info("v3 未通过服务端阈值，改解 v2 重试")
            data[cfg.v2_field] = self._solver.solve_v2(
                page_url=ref, sitekey=cfg.v2_sitekey,
            )
            data[cfg.fallback_flag] = "true"     # 告诉服务端这是回退后的提交
            resp = self._post(self._post_url, data, referer=ref)
            if _needs_v2_fallback(resp.text):
                raise RentCafeError(
                    "条款页 reCAPTCHA 连 v2 回退也未通过——服务端仍要求验证。"
                )

        self._last_html = self._follow(resp, ref)
        return self._last_html

    def select_unit(self, unit) -> str:
        """选中某个具体单元（页面上的 Reserve this room），返回结果页 HTML。

        ``unit`` 是 :class:`bookers.rentcafe_units.UnitOption`。按钮的 onclick
        是 ``ContinueClick(unitId, floorPlanId, propertyId, availableDate, …,
        nextUrl)``，这里把这些参数带到它自己指定的 ``nextUrl`` 上。

        名字唬人，但实测这一步**只是跳到 ApplicantInfo，不是当场锁房**。
        """
        target = unit.next_url or "oleapplication.aspx?stepname=ApplicantInfo"
        sep = "&" if "?" in target else "?"
        url = (
            f"{self._base_url}{self._ole_path}/{target}{sep}"
            f"UnitID={unit.unit_id}&FloorPlanID={unit.floor_plan_id}"
            f"&myOlePropertyId={unit.property_id}&MoveInDate={unit.available_date}"
        )
        logger.info("选中单元 %s（%s）", unit.label or unit.unit_id, unit.listing_id)
        self._last_html = self._get(url).text
        return self._last_html

    #: Applicant Info 页里那个证件上传 iframe。
    _UPLOAD_IFRAME_RE = re.compile(
        r"""src=['"](?P<url>[^'"]*rcLoadContent\.ashx\?"""
        r"""contentclass=PropertySiteImageUpload[^'"]*)['"]""",
        re.I,
    )

    def upload_document(self, page_html: str, filename: str, data: bytes) -> bool:
        """把证件传到申请上。返回是否上传成功。

        服务端在这份文档到位之前**拒绝保存申请表的任何内容**（2026-08-03 实测，
        错误文案 ``Please upload required documents before Proceeding.``）。

        接口契约（抄自 iframe 自己的 ``uploadImage()``）::

            POST  <iframe 自己的 URL>          # form 的 action="" → 提交回本页
            Content-Type: multipart/form-data
                image[]      文件
                objType / objPointer / objSubPointer / docType   ← 页面下发，原样带

        没有验证码，也没有额外 token。URL 里带着 ``ProspectID``——**每份申请一个**，
        所以文档是按申请上传的，不能只传一次到账号上。
        """
        m = self._UPLOAD_IFRAME_RE.search(unescape(page_html or ""))
        if not m:
            logger.warning("Applicant Info 页上找不到证件上传控件")
            return False

        src = m.group("url")
        url = src if "://" in src else f"{self._base_url}{src}"
        # iframe 内那几个隐藏字段，值就在 URL 的查询串里，原样回传。
        qs = dict(parse_qsl(urlparse(url).query))
        fields = {
            "objType": qs.get("objType", ""),
            "objPointer": qs.get("objPointer", ""),
            "objSubPointer": qs.get("objSubPointer", ""),
            "docType": qs.get("docType", ""),
        }
        resp = self._session.post(
            url, data=fields, files={"image[]": (filename, data)},
            headers={"Referer": url}, impersonate=self._impersonate, timeout=120,
        )
        if resp.status_code == 403:
            raise RentCafeBlockedError(f"403 on document upload {url}")
        ok = resp.status_code == 200
        logger.info(
            "证件上传 %s：%s（%d 字节，HTTP %d）",
            "成功" if ok else "失败", filename, len(data), resp.status_code,
        )
        return ok

    def save_applicant_info(self, page_html: str, profile_fields: dict) -> str:
        """填 Applicant Info 并点 **Save**（只存草稿），返回结果页 HTML。

        用 ``Save`` 而不是 ``Save & Continue``：前者把申请存在用户账号下就停住，
        不往付费和签约方向推进——那两步该由用户自己做。
        """
        from bookers.rentcafe_applicant import carry_over_fields

        fields = _extract_hidden_fields(page_html)
        ref = f"{self._base_url}{self._ole_path}/oleapplication.aspx"

        data = dict(fields)
        data.update(carry_over_fields(fields))   # 反自动化字段原样回传
        data.update(profile_fields)
        data["formName2"] = "ApplicantInformation"   # 实测页面自带值就是它
        data["cafeportalkey"] = (
            _extract_cafeportalkey(page_html) or self._cafeportalkey
        )
        # 点 Save 时页面 JS（onclickfunctions('Save')）**只**动这三个字段：
        #
        #     myButtonClicked = 'Save'
        #     ContentclassName = 'ApplicantInformation'
        #     FormError = $(".formError").length      // 客户端校验错误数
        #
        # 它**从不碰 IsSave 和 SaveContinueClicked**——这两个在页面上的默认值
        # 都是空串。2026-08-03 踩过：这里凭空发了 IsSave="1"，服务端切到「提交
        # 申请」的校验路径，回「Please upload required documents before
        # Proceeding.」，草稿一个字都没存下。**页面 JS 不设的字段就别自己编。**
        data["myButtonClicked"] = "Save"
        data.setdefault("ContentclassName", "ApplicantInformation")
        data["FormError"] = "0"                 # 我们是程序填的，没有客户端校验错误
        data.pop("IsSave", None)
        data.pop("SaveContinueClicked", None)

        resp = self._post(self._post_url, data, referer=ref)
        # 必须验收。服务端拒绝时照样回 HTTP 200，错误只藏在响应体的
        # showMessage 里——不检查就会把「什么都没存下」报成「已起草申请」。
        err = server_error_message(resp.text)
        if err:
            raise RentCafeSaveRejectedError(f"申请表保存被 RENTCafe 拒绝：{err}")
        self._last_html = self._follow(resp, ref)
        return self._last_html

    def current_page_html(self) -> str:
        """最近一次落地页的 HTML。多步流程里用来核对「我到哪一步了」。"""
        return self._last_html

    def _follow(self, resp, referer: str) -> str:
        """跟随响应里的 JS 跳转，返回最终页面 HTML。

        RENTCafe 的 ``rcformsave.ashx`` 常返回一段 ``window.location=...``
        而不是 302，所以不能只靠 HTTP 重定向。
        """
        html = resp.text or ""
        # 会连跳：登录成功先跳 multiloginwrapper.aspx，它再跳回申请流程。
        # 限 5 跳，跳环了就把手上这页交出去，别把自己转死。
        seen: set[str] = set()
        for _ in range(5):
            target = _extract_js_redirect(html)
            if not target:
                break
            url = target if "://" in target else urljoin(referer, target)
            if url in seen:
                break
            seen.add(url)
            referer = url
            html = self._get(url).text
        return html

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _new_session(self) -> req.Session:
        """新建一条走代理的会话。

        为什么必须走代理
        ----------------
        2026-08-03 实测：从同一个出口 IP 连着跑几轮预订后，``rcformsave.ashx``
        的 **POST 开始整片 403，而 GET 仍然正常**——WAF 是按「IP × 写接口」限流
        的，等 7 分钟都不放行。抓取侧（``scrapers.ourdomain``）一直是走代理池的，
        预订侧却在用裸 Session 直连，等于把真正要紧的那条链路暴露在最容易被限
        流的位置上。

        为什么整条会话固定一个出口
        --------------------------
        RENTCafe 的流程状态存在服务端会话里，中途换 IP 既会显得可疑，也可能被
        直接判失效。所以只在 :meth:`open` 重建会话（换 TLS 指纹）时才顺带换
        IP——那时本来就要从头来过，换个出口正好是免费的逃生口。
        """
        from config import get_proxy_url

        proxy = get_proxy_url(self._source, rotating=True)
        if not proxy:
            return req.Session()
        return req.Session(proxies={"https": proxy, "http": proxy})

    def _get(self, url: str) -> req.Response:
        assert self._session is not None
        resp = self._session.get(url, impersonate=self._impersonate, timeout=30)
        if resp.status_code == 403:
            raise RentCafeBlockedError(f"403 on GET {url} — IP may be blocked")
        resp.raise_for_status()
        return resp

    def _post(self, url: str, data: dict, referer: str = "") -> req.Response:
        assert self._session is not None
        headers = {}
        if referer:
            headers["Referer"] = referer
        resp = self._session.post(
            url, data=data, headers=headers,
            impersonate=self._impersonate, timeout=30,
        )
        if resp.status_code == 403:
            raise RentCafeBlockedError(f"403 on POST {url} — IP may be blocked")
        if resp.status_code == 404:
            logger.warning("404 on POST %s — endpoint may not exist", url)
        return resp

    @staticmethod
    def _needs_recaptcha(form_data: dict) -> bool:
        """Return True if the form has reCAPTCHA hidden fields."""
        return any(
            k in form_data
            for k in ("g-recaptcha-response-v3", "g-recaptcha-response", "failed-captcha-3")
        )

    @staticmethod
    def _handle_response(resp: req.Response, step: str) -> dict:
        """Parse the server response after a form POST.

        RENTCafe responds with either:
        - JSON (``{"type": "success", ...}`` or ``{"type": "error", "text": "..."}``)
        - An HTML redirect (200 with embedded JS ``window.location``)
        - A 302 redirect (followed automatically by curl_cffi)
        """
        text = resp.text.strip()

        # JSON response
        if text.startswith("{"):
            import json

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("[%s] invalid JSON response: %s", step, text[:300])
                return {"_raw": text}

            msg_type = data.get("type", "")
            if msg_type == "error":
                msg = data.get("text", "Unknown error")
                logger.error("[%s] server error: %s", step, msg)
                raise RentCafeError(f"RENTCafe [{step}] error: {msg}")
            logger.info("[%s] success (type=%s)", step, msg_type)
            return data

        # HTML response — might contain a JS redirect to the next step.
        fields = _extract_hidden_fields(text)
        cpk = _extract_cafeportalkey(text)
        if cpk:
            fields["cafeportalkey"] = cpk

        # Check for JS redirects (window.location = '...')
        redirect = _extract_js_redirect(text)
        if redirect:
            logger.info("[%s] JS redirect → %s", step, redirect[:120])
            fields["_redirect"] = redirect

        return fields


# ---------------------------------------------------------------------------
# RentCafeBooker — AbstractBooker implementation
# ---------------------------------------------------------------------------


class RentCafeBooker(AbstractBooker):
    """Automated booking for RENTCafe-powered platforms (Xior, OurDomain).

    Reads user-specific credentials from ``request.user.auto_book`` (stored
    in SQLite ``user_configs.auto_book_json``).  The global CAPTCHA_API_KEY
    is read from environment.
    """

    source = "rentcafe"  # overridden by platform subclasses

    def __init__(self) -> None:
        self._api_key = os.environ.get("CAPTCHA_API_KEY", "")
        if not self._api_key:
            logger.warning("CAPTCHA_API_KEY not set — reCAPTCHA solving will fail")

    # ------------------------------------------------------------------
    # AbstractBooker interface
    # ------------------------------------------------------------------

    def book(self, request: BookingRequest) -> BookingResult:
        """半自动预订：把申请起草到「只差上传证件和付款」为止。

        为什么不做完整自动化
        --------------------
        流程里有必填的 ID/Passport 上传，且最后要付款。这两步系统都不该代劳——
        替一群用户保管护照扫描件是拿一个巨大的泄露责任去换几十秒，替用户付钱
        更不用说。所以 booker 走到 **Save**（存草稿）就停。

        **它不保证抢到房，锁定发生在付款那一步**（2026-08-03 侦察确认，不再是
        推测）：``ApplicationCharges`` 步骤的表单 ``PaymentApplicationChargeStep1``
        要填 ``sAcct``（IBAN）/ ``sSwiftCode`` / ``sName``，页面原文是
        「in order to continue the application process, all administration fees
        will need to be paid」。

        **填银行账户是硬限制，不是设计选择**——所以边界只能是 Save，往前一步
        都没有。返回的 phase 是 ``draft_saved`` 而不是 ``success``，通知文案
        明写「房源还没占住，请立刻去付款」：把它当成功报给用户，用户会以为房
        到手了，慢悠悠去付款，结果被别人抢走。

        每一步都先核对落到了哪里再往下走：这条流程里猜错一步的后果不是报错，
        而是在用户真实账号下默默提交一份错的申请。
        """
        import applicant_docs
        from bookers.rentcafe_form import parse_applicant_form
        from bookers.rentcafe_applicant import (
            FormShapeChangedError, ProfileIncompleteError, build_form_fields,
        )
        from bookers.rentcafe_units import find_unit
        from scrapers.xior import building_key_for

        listing = request.listing
        ab = request.user.auto_book

        # ── 前置校验：不触网 ──────────────────────────────────────────
        # RENTCafe 连续失败会锁 30 分钟。缺凭据/缺档案时先在本地挡掉，别拿
        # 一次注定失败的请求去消耗真正需要它的额度。
        building = building_key_for(listing)
        if not building:
            return BookingResult(
                listing=listing, success=False, phase="unsupported",
                message=f"无法从 listing 反查 Xior 楼栋（city={listing.city!r}）",
            )
        email, password = ab.xior_account_for(building)
        if not (email and password):
            return BookingResult(
                listing=listing, success=False, phase="not_configured",
                message=f"未配置该楼栋（{listing.city}）的 Xior 账号，已跳过。",
            )
        profile = ab.applicant_profile
        if not profile.is_complete():
            return BookingResult(
                listing=listing, success=False, phase="not_configured",
                message="申请人档案不完整，已跳过。缺：" + ", ".join(profile.missing_fields()),
            )
        if not ab.has_screening_consent():
            return BookingResult(
                listing=listing, success=False, phase="not_configured",
                message=(
                    "未授权系统代勾申请表上的信用/背景调查声明，已跳过。"
                    "请在面板的自动预订设置里先行授权。"
                ),
            )
        if request.dry_run or ab.dry_run:
            return BookingResult(
                listing=listing, success=True, phase="dry_run", dry_run=True,
                message=f"试运行：凭据与档案齐备，可为 {listing.name} 起草申请。",
            )

        session = RentCafeSession(self._api_key, source=self.source)
        try:
            # ① 打开 applyOnlineURL。这是唯一入口——选房步骤的上下文存在服务端
            #    会话里，直接深链 stepname=Apartments 会得到一个空搜索。
            page_fields = session.open(listing.url)

            # ② 条款页 → Start Application（标准 v3 reCAPTCHA）
            session.submit_terms(page_fields)

            # ③ 登录。登录后 RENTCafe 会恢复上次的申请上下文。
            session.login(email, password)

            # ④ 找到目标单元。找不到就是被人抢先了——交给上层换备选房源。
            html = session.current_page_html()
            unit = find_unit(html, listing.id)
            if unit is None:
                return BookingResult(
                    listing=listing, success=False, phase="race_lost",
                    message="该单元已不在可订列表中（可能已被他人选走）。",
                )

            # ⑤ 选中单元 → 落到 Applicant Info
            applicant_html = session.select_unit(unit)

            # ⑤b 传证件。服务端在这份文档到位前**拒绝保存申请表的任何内容**，
            #     所以必须排在填表之前。文档是**按申请**上传的（URL 里带
            #     ProspectID），每抢一个新单元都要重传一次。
            doc = applicant_docs.load(request.user.id)
            if doc is None:
                return BookingResult(
                    listing=listing, success=False, phase="not_configured",
                    message=(
                        "还没上传证件，申请表存不了。\n"
                        "请在面板的自动预订设置里上传护照/身份证。"
                    ),
                )
            if not session.upload_document(applicant_html, doc[0], doc[1]):
                return BookingResult(
                    listing=listing, success=False, phase="unknown_error",
                    message=f"证件上传失败，申请未提交。\n{listing.url}",
                )
            # 上传后页面上的隐藏字段会变（文档状态、ProspectId 之类），
            # **重新取一遍**再填表——拿上传前的那份去解析，字段值可能已经过期。
            applicant_html = session.select_unit(unit)

            # ⑥ 填表并**只存草稿**
            #
            # 字段名必须从**这一页**解析：有一批名字里嵌着 prospect id
            # （drpGender2057105），硬编码不了。2026-08-03 踩过：上一版 15 个
            # 字段名全写错，命中 0，提交上去的是一份空白申请，且毫无报错。
            form = parse_applicant_form(applicant_html)
            if form is None:
                return BookingResult(
                    listing=listing, success=False, phase="unknown_error",
                    message="申请表页面结构无法识别（可能已改版），已中止。",
                )
            fields = build_form_fields(
                profile, form,
                school_id=unit.school_id,
                screening_consent=ab.has_screening_consent(),
            )
            session.save_applicant_info(applicant_html, fields)

            logger.info("Xior 申请已起草：%s（%s）", listing.name, unit.listing_id)
            return BookingResult(
                listing=listing, success=True, phase="draft_saved",
                message=(
                    f"已为你起草 {listing.name} 的申请，证件也已上传。\n\n"
                    f"**请点击链接付款**，否则可能被他人抢先。\n"
                    f"{listing.url}"
                ),
                pay_url=listing.url,
                contract_start_date=unit.available_date,
            )

        except FormShapeChangedError as exc:
            # 上游改版了。必须显式失败——默默提交一份缺项的申请，用户看不出
            # 任何异常，等发现时房子已经没了。
            logger.error("Xior 申请表结构变化: %s", exc)
            return BookingResult(
                listing=listing, success=False, phase="unknown_error",
                message=f"{exc}\n请到网站上手动完成。\n{listing.url}",
            )
        except ProfileIncompleteError as exc:
            return BookingResult(listing=listing, success=False,
                                 phase="not_configured", message=str(exc))
        except RentCafeSaveRejectedError as exc:
            logger.error("Xior 申请表保存被拒: %s", exc)
            return BookingResult(
                listing=listing, success=False, phase="save_rejected",
                message=f"{exc}\n申请**没有**保存，请到网站上手动完成。\n{listing.url}",
            )
        except RentCafeAuthError as exc:
            logger.error("Xior 登录失败: %s", exc)
            return BookingResult(
                listing=listing, success=False, phase="auth_failed",
                message=f"{exc}\n请在面板里核对这栋楼的 Xior 账号密码。",
            )
        except RentCafeBlockedError as exc:
            logger.error("Xior 预订被屏蔽: %s", exc)
            return BookingResult(listing=listing, success=False,
                                 phase="blocked", message=str(exc))
        except CaptchaError as exc:
            logger.error("Xior 预订 reCAPTCHA 求解失败: %s", exc)
            return BookingResult(listing=listing, success=False,
                                 phase="blocked", message=str(exc))
        except RentCafeError as exc:
            logger.error("Xior 预订失败: %s", exc)
            return BookingResult(listing=listing, success=False,
                                 phase="unknown_error", message=str(exc))
        except Exception as exc:
            logger.error("Xior 预订未预期错误: %s", exc, exc_info=True)
            return BookingResult(listing=listing, success=False,
                                 phase="unknown_error", message=str(exc))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_base_and_path(url: str) -> tuple[str, str]:
    """Split ``https://host/onlineleasing/PROP/oleapplication.aspx?...`` into
    ``(base, ole_path)``."""
    m = re.match(r"(https://[^/]+)(/onlineleasing/[^/]+)/", url)
    if not m:
        raise RentCafeError(f"Cannot parse RENTCafe URL: {url}")
    return m.group(1), m.group(2)


def _extract_hidden_fields(html: str) -> dict[str, str]:
    """Return all ``<input type="hidden">`` fields as a flat dict."""
    fields: dict[str, str] = {}
    for inp in re.findall(r'<input[^>]*type=["\']hidden["\'][^>]*>', html):
        name = re.search(r'name=["\']([^"\']+)["\']', inp)
        value = re.search(r'value=["\']([^"\']*)["\']', inp)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


_FORM_RE_TMPL = r'<form[^>]*\bid=["\']{fid}["\'][^>]*>(?P<body>.*?)</form>'


def _extract_form_fields(
    html: str, form_id: str, *, required: bool = True,
) -> dict[str, str]:
    """只取**某一个 form 内部**的隐藏字段。

    为什么不能整页抓：``guestlogin.aspx`` 上有 4 个表单（Login / UserLogin /
    OtpOptions / VerifyOTP），它们各有自己的 ``formName``、``SecurityCode*``、
    ``myFormName*``。整页抓会让它们互相覆盖，拼出来的请求服务端不认——实测
    表现是 POST 返回 HTTP 200 但 body 全空，既不报错也不跳转（2026-08-03）。

    找不到该 form 时返回空 dict，由调用方决定怎么办（**不要**退回整页抓，
    那正是这个函数要避免的东西）。

    ``required=False`` 时不打 WARNING：有些响应**本来就不含表单**（登录探测那
    步返回的是一句 ``ShowPasswordInline();``），在成功路径上稳定刷警告只会让人
    学会无视警告。
    """
    m = re.search(
        _FORM_RE_TMPL.format(fid=re.escape(form_id)), html or "", re.S | re.I,
    )
    if not m:
        if required:
            logger.warning("页面里找不到 form#%s", form_id)
        else:
            logger.debug("响应里没有 form#%s（该步骤可以没有）", form_id)
        return {}
    return _extract_hidden_fields(m.group("body"))


def _extract_cafeportalkey(html: str) -> str:
    """Extract the encrypted session token ``cafeportalkey`` from a page."""
    m = re.search(r'name=["\']cafeportalkey["\'][^>]*value=["\']([^"\']+)["\']', html)
    return m.group(1) if m else ""


#: 服务端要求走 v2 回退时会回的标记。它返回 HTTP 200 + 一段 JS，不是错误码，
#: 所以只能靠内容判断——照抄实测响应里的两个特征。
_V2_FALLBACK_MARKERS = (
    "callReCaptchaV2",              # 渲染 v2 checkbox 的函数
    "Please verify that you are not a robot",
)


def _needs_v2_fallback(html: str) -> bool:
    """响应是不是在要求 v2 回退。"""
    body = html or ""
    return any(m in body for m in _V2_FALLBACK_MARKERS)


#: 服务端把错误提示当成一段 JS 返回，由页面塞进 ``#noticeboxdiv_*``。
#: 形如 ``$.showMessage({type: "error",text:"Invalid login credentials.",…})``。
_SHOW_MESSAGE_RE = re.compile(
    r"""showMessage\s*\(\s*\{[^}]*?type:\s*['"]error['"][^}]*?"""
    r"""text:\s*['"](?P<text>[^'"]*)['"]""",
    re.S | re.I,
)


def server_error_message(html: str) -> str:
    """从任一步的响应里取出服务端的错误文案；没有错误返回空串。

    **全站统一的成败判据**，登录、条款、申请表都是这一套：每个表单都用 AJAX
    提交，成功回调只是把返回的 HTML 追加进那一步自己的错误提示框
    （``$('#noticeboxdiv_<表单名>').append(...)``）。所以：

    - 响应体为空 → 没有错误要显示 → **成功**
    - 响应体里有 ``$.showMessage({type:"error", text:"…"})`` → **失败**

    两个方向都踩过（2026-08-03）：

    - 把空 body 当失败 → 登录其实早已成功，流程却一路报到 race_lost；
    - 不检查错误就当成功 → 服务端回了「Please upload required documents
      before Proceeding.」，代码照样告诉用户「已为你起草申请，请迅速上传
      证件」。**这个方向危险得多**：用户会以为表单已经填好了，安心去传证件，
      实际上什么都没存下。
    """
    m = _SHOW_MESSAGE_RE.search(html or "")
    return m.group("text").strip() if m else ""


def _raise_if_login_error(html: str, stage: str) -> None:
    msg = server_error_message(html)
    if msg:
        raise RentCafeAuthError(f"{stage}被 RENTCafe 拒绝：{msg}")


def _has_unit_list(html: str) -> bool:
    """页面上有没有可选单元（``ContinueClick(...)`` 是选房按钮的处理器）。"""
    return "ContinueClick(" in (html or "")


def _extract_js_redirect(html: str) -> str:
    """取出响应里的 JS 跳转目标。

    四种写法都要认——RENTCafe 在不同步骤用的不是同一种：

        window.location.href = '…'
        window.location      = '…'
        location.href        = '…'   ← 登录成功用的就是这个（无 window. 前缀）
        location.replace('…')

    2026-08-03 踩过：正则只写了带 ``window.`` 的两种，登录成功返回的
    ``location.href='…/multiloginwrapper.aspx?AllowRedirect=1'`` 跟不上，
    于是登录明明成功却停在原页面，找不到单元列表。
    """
    m = re.search(
        r"""(?:window\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]"""
        r"""|(?:window\.)?location\.replace\(\s*['"]([^'"]+)['"]""",
        html or "",
    )
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


# ---------------------------------------------------------------------------
# platform-specific subclasses (registered in bookers/__init__.py)
# ---------------------------------------------------------------------------


class XiorBooker(RentCafeBooker):
    """RentCafeBooker pre-bound to the ``"xior"`` source."""
    source = "xior"


class OurDomainBooker(RentCafeBooker):
    """RentCafeBooker pre-bound to the ``"ourdomain"`` source."""
    source = "ourdomain"
