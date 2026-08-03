"""captcha/solver.py — 2Captcha wrapper for RENTCafe reCAPTCHA solving.

Usage::

    solver = CaptchaSolver(api_key="...")
    token = solver.solve_v2(page_url="https://.../register.aspx")
    # or with explicit sitekey:
    token = solver.solve_v2(page_url=url, sitekey=RENTCAFE_V2_SITEKEY)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# v2 checkbox 的 sitekey 全站一致（实测三个有验证码的页面都是它）。
RENTCAFE_V2_SITEKEY = "6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0"

# ⚠️ v3 的 sitekey **不是全站一致的**。这个常量只是 register / guestlogin 用的
# Enterprise key，保留是为了兼容既有调用；条款页（oleapplication）用的是标准 v3
# 和另一个 key。正确做法是查 captcha.rentcafe_pages.page_captcha()，不要拿它当默认。
RENTCAFE_V3_SITEKEY = "6LfBeqEaAAAAALsbENKGUsE98xFoA3ZpqkbzogBI"


class CaptchaError(Exception):
    """reCAPTCHA solving failed (timeout, bad key, no balance, etc.)."""


class CaptchaSolver:
    """Solve reCAPTCHA challenges via 2Captcha.

    Parameters
    ----------
    api_key : str
        2Captcha API key.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        # Lazy-import so the module is importable even without the SDK.
        from twocaptcha import TwoCaptcha
        self._client = TwoCaptcha(api_key)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def balance(self) -> float:
        """Return current account balance in USD."""
        try:
            return float(self._client.balance())
        except Exception as exc:
            raise CaptchaError(f"2Captcha balance check failed: {exc}") from exc

    def solve_v2(
        self,
        page_url: str,
        sitekey: str = RENTCAFE_V2_SITEKEY,
        timeout: int = 120,
    ) -> str:
        """Solve a reCAPTCHA v2 checkbox challenge.

        Parameters
        ----------
        page_url : str
            The full URL of the page containing the reCAPTCHA widget.
        sitekey : str
            Google reCAPTCHA sitekey (defaults to the RENTCafe v2 key).
        timeout : int
            Max seconds to wait for a human solver (default 120).

        Returns
        -------
        str — g-recaptcha-response token (~2000+ chars).
        """
        logger.info("Solving reCAPTCHA v2 for %s …", page_url[:100])
        try:
            result = self._client.recaptcha(
                sitekey=sitekey,
                url=page_url,
            )
            token: str = result["code"]
            logger.info("reCAPTCHA v2 solved (%d chars)", len(token))
            return token
        except Exception as exc:
            raise CaptchaError(
                f"reCAPTCHA v2 solve failed for {page_url[:120]}: {exc}"
            ) from exc

    def solve_v3(
        self,
        page_url: str,
        action: str = "GuestRegistration",
        sitekey: str = RENTCAFE_V3_SITEKEY,
        min_score: float = 0.3,
        timeout: int = 120,
        enterprise: bool = True,
    ) -> str:
        """Solve a reCAPTCHA v3 challenge (standard or Enterprise).

        Parameters
        ----------
        page_url : str
            The full URL of the page containing the reCAPTCHA widget.
        action : str
            The reCAPTCHA action name (must match the page's JS call,
            e.g. ``"GuestRegistration"``, ``"UserLogin"``,
            ``"start_application"``).
        sitekey : str
            Google reCAPTCHA sitekey.  **Not the same on every RENTCafe
            page** — look it up in :mod:`captcha.rentcafe_pages` rather
            than relying on the default.
        min_score : float
            Minimum requested score (0.0-1.0).  The service may not
            actually achieve it — Google fingerprints solver traffic.
        timeout : int
            Max seconds to wait (default 120).
        enterprise : bool
            True for ``enterprise.js`` pages, False for plain ``api.js``.
            This used to be hardcoded to True, which is wrong for the
            ``oleapplication`` terms page — it loads standard ``api.js``
            with a different sitekey, and an Enterprise-solved token for
            it is rejected server-side.  Always pass the value from
            :func:`captcha.rentcafe_pages.page_captcha`.

        Returns
        -------
        str — g-recaptcha-response-v3 token.
        """
        logger.info(
            "Solving reCAPTCHA v3%s for %s (action=%s, sitekey=%s…) …",
            " Enterprise" if enterprise else "",
            page_url[:100], action, sitekey[:12],
        )
        try:
            result = self._client.recaptcha(
                sitekey=sitekey,
                url=page_url,
                version="v3",
                enterprise=1 if enterprise else 0,
                action=action,
                score=min_score,
            )
            token: str = result["code"]
            logger.info("reCAPTCHA v3 solved (%d chars)", len(token))
            return token
        except Exception as exc:
            raise CaptchaError(
                f"reCAPTCHA v3 solve failed for {page_url[:120]} "
                f"(action={action}): {exc}"
            ) from exc
