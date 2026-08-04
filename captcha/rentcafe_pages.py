"""captcha/rentcafe_pages.py — RENTCafe 各页的 reCAPTCHA 实测配置
=================================================================

**这张表的每一行都是从真实页面 HTML 抓出来的**，不是从文档抄的。抓取脚本见
``docs/XIOR.md`` §8.3 的方法说明；原始页面留在侦察记录里。

为什么需要这张表
----------------
原来的实现假设「RENTCafe 全线用同一套 Enterprise reCAPTCHA」，于是
``solve_v3()`` 写死 ``enterprise=1`` 并默认用 ``6LfBeqEa...``。实测这个假设
在**条款页上是错的**——那一页用的是标准 v3（``api.js``）+ 另一个 sitekey。
拿 Enterprise 参数去解标准 v3，或反过来，出来的 token 服务端不认。

三个维度都按页不同，缺一不可：

===================  ==========  =====================================
维度                  差异         后果
===================  ==========  =====================================
v3 类型               标准 / 企业   求解任务类型不同，选错拿不到有效 token
v3 sitekey           两个不同的     sitekey 错则 token 与页面不匹配
action               三种          Google 按 action 计分，错了分数会低
回退标志字段           两种命名      填错服务端不知道该不该走 v2
===================  ==========  =====================================

未覆盖的页
----------
第 3 步（Applicant Info）及之后的页面**尚未到达**——需要真实账号登录，且
需要有一个在售单元。等走通之后再往这张表里补，不要凭 register/login 的形状
猜（这张表存在的理由恰恰就是「同一站点不同页并不一致」）。
"""
from __future__ import annotations

from dataclasses import dataclass

# 标准 reCAPTCHA v3：https://www.google.com/recaptcha/api.js
KIND_V3 = "v3"
# reCAPTCHA Enterprise v3：https://www.google.com/recaptcha/enterprise.js
KIND_V3_ENTERPRISE = "v3_enterprise"
# 该页无 reCAPTCHA
KIND_NONE = "none"

# v2 checkbox 回退。三个有验证码的页面用的是同一个 key（这一项文档原本就是对的）。
RENTCAFE_V2_SITEKEY = "6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0"


@dataclass(frozen=True, slots=True)
class PageCaptcha:
    """一个 RENTCafe 页面的 reCAPTCHA 契约。"""

    page: str            # aspx 文件名（不含扩展名）
    kind: str            # KIND_*
    v3_sitekey: str = ""
    action: str = ""     # grecaptcha.execute 的 action，必须与页面 JS 一致
    v3_field: str = "g-recaptcha-response-v3"   # v3 token 填进哪个隐藏字段
    #: v2 回退的 token 字段。**和 v3 不是同一个**——v2 由 grecaptcha.render()
    #: 渲染成 checkbox，token 落在标准的 g-recaptcha-response 上。
    #: 2026-08-03 实测踩过：把 v2 token 塞进 v3 字段，服务端一直回
    #: 「Please verify that you are not a robot」，看不出是字段错了。
    v2_field: str = "g-recaptcha-response"
    fallback_flag: str = ""                     # 触发 v2 回退的标志字段
    v2_sitekey: str = RENTCAFE_V2_SITEKEY
    form_name: str = ""                         # 主表单的 id / formName2

    @property
    def is_enterprise(self) -> bool:
        return self.kind == KIND_V3_ENTERPRISE

    @property
    def has_captcha(self) -> bool:
        return self.kind != KIND_NONE


# 实测于 2026-08-03，zernikestraat-xiorstudenthousing.securerc.co.uk
RENTCAFE_PAGES: dict[str, PageCaptcha] = {
    # 第 2 步「Rental Options」。注意这里是**标准 v3**，不是 Enterprise，
    # 而且 sitekey 与登录/注册页完全不同——文档此前把整站记成了一套。
    "oleapplication": PageCaptcha(
        page="oleapplication",
        kind=KIND_V3,
        v3_sitekey="6LcjBc4UAAAAABfXlERv_hq_KE3IWDAqbiWkbPzl",
        action="start_application",
        fallback_flag="failed-captcha-3-rentable",
        form_name="termsandotheritems",
    ),
    # OurDomain 的第 2 步。同一个步骤（Rental Options），但**页面不同**：
    # Xior 把它嵌在 oleapplication.aspx 里，OurDomain 是独立的
    # termsandotheritems.aspx。2026-08-04 在两栋 OurDomain 楼上实测，验证码
    # 契约与 Xior **逐字相同**——同样的标准 v3、同样的 sitekey、同样的 action、
    # 同样的回退字段名，连页面 JS 的函数名（callReCaptchaV2Rentable）都一样。
    #
    # 单独列一行而不是让 page_captcha() 做别名，是因为这张表的用途就是记录
    # 「哪一页实测是什么样」。合并会把「两页碰巧一致」写成「本来就是一页」，
    # 哪天 Yardi 只改其中一边，就看不出来了。
    "termsandotheritems": PageCaptcha(
        page="termsandotheritems",
        kind=KIND_V3,
        v3_sitekey="6LcjBc4UAAAAABfXlERv_hq_KE3IWDAqbiWkbPzl",
        action="start_application",
        fallback_flag="failed-captcha-3-rentable",
        form_name="termsandotheritems",
    ),
    "guestlogin": PageCaptcha(
        page="guestlogin",
        kind=KIND_V3_ENTERPRISE,
        v3_sitekey="6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI",
        action="UserLogin",
        fallback_flag="failed-captcha-3",
        form_name="UserLogin",
    ),
    "register": PageCaptcha(
        page="register",
        kind=KIND_V3_ENTERPRISE,
        v3_sitekey="6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI",
        action="GuestRegistration",
        fallback_flag="failed-captcha-3",
        form_name="Registration",
    ),
    # 全站唯一没有 reCAPTCHA 的入口（实测 0 个 sitekey、无 recaptcha 脚本）。
    # 但它只是个「选租约类型」的落地页，两个出口都指回 register.aspx —— 也就是
    # **绕不过注册页的验证码**，只是换了个入口。别把它当旁路。
    "flexregistrationlandingpage": PageCaptcha(
        page="flexregistrationlandingpage",
        kind=KIND_NONE,
        form_name="FlexRegistrationLandingPage",
    ),
}


def page_captcha(page: str) -> PageCaptcha | None:
    """按 aspx 名取配置；未侦察过的页返回 None（调用方必须显式处理）。

    刻意不给「默认值兜底」：这张表存在的全部理由就是各页不一致，随便挑一个
    当默认，等于把已经踩过的坑重新埋回去。
    """
    return RENTCAFE_PAGES.get(page.replace(".aspx", "").strip().lower())
