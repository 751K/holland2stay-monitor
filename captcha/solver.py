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

# RENTCafe reCAPTCHA sitekeys — same across Xior / OurDomain / all securerc.co.uk
RENTCAFE_V2_SITEKEY = "6LfAdx8TAAAAAOiesnT8CNKNtb1C6doK-RKnB1V0"
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
    ) -> str:
        """Solve a reCAPTCHA v3 Enterprise challenge.

        Parameters
        ----------
        page_url : str
            The full URL of the page containing the reCAPTCHA widget.
        action : str
            The reCAPTCHA action name (must match the page's JS call,
            e.g. ``"GuestRegistration"``, ``"UserLogin"``).
        sitekey : str
            Google reCAPTCHA sitekey (defaults to the RENTCafe v3 key).
        min_score : float
            Minimum requested score (0.0-1.0).  The service may not
            actually achieve it — Google fingerprints solver traffic.
        timeout : int
            Max seconds to wait (default 120).

        Returns
        -------
        str — g-recaptcha-response-v3 token.
        """
        logger.info(
            "Solving reCAPTCHA v3 for %s (action=%s) …",
            page_url[:100], action,
        )
        try:
            result = self._client.recaptcha(
                sitekey=sitekey,
                url=page_url,
                version="v3",
                enterprise=1,
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
