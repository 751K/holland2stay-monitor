"""captcha — reCAPTCHA solving abstraction for auto-booking.

Currently wraps 2Captcha. Designed so providers can be swapped by
implementing the same CaptchaSolver interface.

各页的 sitekey / v3 类型 / action **并不一致**，查
:mod:`captcha.rentcafe_pages` 的实测表，不要用 ``RENTCAFE_V3_SITEKEY``
当默认值——那个坑已经踩过一次了。
"""

from .rentcafe_pages import (
    KIND_NONE,
    KIND_V3,
    KIND_V3_ENTERPRISE,
    RENTCAFE_PAGES,
    PageCaptcha,
    page_captcha,
)
from .solver import CaptchaSolver, CaptchaError, RENTCAFE_V2_SITEKEY, RENTCAFE_V3_SITEKEY

__all__ = [
    "CaptchaSolver",
    "CaptchaError",
    "RENTCAFE_V2_SITEKEY",
    "RENTCAFE_V3_SITEKEY",
    "PageCaptcha",
    "RENTCAFE_PAGES",
    "page_captcha",
    "KIND_V3",
    "KIND_V3_ENTERPRISE",
    "KIND_NONE",
]
