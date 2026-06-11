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
from urllib.parse import urljoin

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

    def __init__(self, captcha_api_key: str) -> None:
        self._solver = CaptchaSolver(captcha_api_key)
        self._session: req.Session | None = None
        self._base_url: str = ""
        self._ole_path: str = ""
        self._post_url: str = ""  # e.g. https://host/onlineleasing/rcformsave.ashx
        self._cafeportalkey: str = ""
        self._impersonate: str = get_impersonate()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def open(self, apply_url: str) -> dict:
        """Open the ``oleapplication.aspx`` page and return its hidden fields.

        This is always the first step.  It sets session cookies and extracts
        the ``cafeportalkey`` that must be carried through every subsequent
        request.
        """
        self._session = req.Session()
        resp = self._session.get(apply_url, impersonate=self._impersonate, timeout=30)

        if resp.status_code != 200:
            raise RentCafeError(
                f"oleapplication.aspx returned {resp.status_code} for {apply_url}"
            )

        self._base_url, self._ole_path = _parse_base_and_path(resp.url)
        self._post_url = f"{self._base_url}/onlineleasing/rcformsave.ashx"
        self._cafeportalkey = _extract_cafeportalkey(resp.text)

        fields = _extract_hidden_fields(resp.text)
        logger.info(
            "RENTCafe session opened base=%s cpk=%s…",
            self._base_url,
            self._cafeportalkey[:20],
        )
        return fields

    def register(
        self,
        first_name: str,
        last_name: str,
        email: str,
        password: str,
        phone: str = "",
        birth_date: str = "",
    ) -> dict:
        """Create a new RENTCafe account and return the next-step page data.

        Returns the hidden fields from the page after successful registration
        (typically the Applicant Info step).
        """
        reg_url = f"{self._base_url}{self._ole_path}/register.aspx"
        logger.info("Navigating to register: %s", reg_url)

        resp = self._get(reg_url)
        fields = _extract_hidden_fields(resp.text)
        cpk = _extract_cafeportalkey(resp.text) or self._cafeportalkey

        # Solve v2 (our v3 score will be too low, so v2 fallback is inevitable).
        v2_token = self._solver.solve_v2(page_url=reg_url)
        # Also grab a v3 token — some server-side checks want it present.
        v3_token = self._solver.solve_v3(page_url=reg_url, action="GuestRegistration")

        data = dict(fields)
        data.update({
            "txtName": first_name,
            "txtName2": last_name,
            "txtEmail": email,
            "txtPassword": password,
            "txtPhone": phone,
            "BirthDate": birth_date,
            "Prefer": "byEmail",
            "SubscribeToEmails": "false",
            "g-recaptcha-response-v3": v2_token,  # v2 token → v3 hidden field
            "failed-captcha-3": "false",
            "cafeportalkey": cpk,
        })

        resp2 = self._post(self._post_url, data, referer=reg_url)
        return self._handle_response(resp2, "register")

    def login(self, email: str, password: str) -> dict:
        """Log in to an existing RENTCafe account.

        Returns the hidden fields from the page after successful login.
        """
        login_url = f"{self._base_url}{self._ole_path}/guestlogin.aspx"
        logger.info("Navigating to login: %s", login_url)

        resp = self._get(login_url)
        fields = _extract_hidden_fields(resp.text)
        cpk = _extract_cafeportalkey(resp.text) or self._cafeportalkey

        v2_token = self._solver.solve_v2(page_url=login_url)
        v3_token = self._solver.solve_v3(page_url=login_url, action="UserLogin")

        data = dict(fields)
        data.update({
            "Username": email,
            "Password": password,
            "CheckUserAuth": "1",
            "g-recaptcha-response-v3": v2_token,
            "failed-captcha-3": "false",
            "cafeportalkey": cpk,
        })

        resp2 = self._post(self._post_url, data, referer=login_url)
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
    # internal helpers
    # ------------------------------------------------------------------

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
        """Execute the full RENTCafe booking flow.

        Reads credentials from ``request.user.auto_book``.  The listing URL
        must be a valid ``applyOnlineURL`` (stored in ``listing.url`` by
        XiorScraper / OurDomainScraper).
        """
        ab = request.user.auto_book
        dry_run = ab.dry_run if ab.dry_run else request.dry_run
        if self.source == "xior":
            email, password = ab.xior_email, ab.xior_password
        elif self.source == "ourdomain":
            email, password = ab.ourdomain_email, ab.ourdomain_password
        else:
            email, password = ab.email, ab.password

        session = RentCafeSession(self._api_key)

        try:
            # 1. Open the oleapplication session.
            _fields = session.open(request.listing.url)
            logger.info("Step 1/2: oleapplication opened (email=%s)", email)

            # 2. Log in with user's pre-registered account.
            #    We do NOT auto-register — the user must create their
            #    RENTCafe account manually in a browser first.
            if not email or not password:
                return BookingResult(
                    success=False,
                    message="RENTCafe 账号未配置（邮箱/密码为空），请在 Web 面板填写后重试。",
                    phase="no_credentials",
                )
            next_page = session.login(email, password)
            logger.info("Step 3: logged in, next page keys=%s", list(next_page.keys())[:10])

            # 3. Walk through remaining form steps.
            #    Each step returns a dict; if it contains ``_redirect``
            #    we follow that URL; otherwise we submit the returned
            #    fields to the next step.
            form_data = next_page
            for _step_index in range(6):  # safety cap: max 6 form steps
                redirect = form_data.pop("_redirect", None)
                if redirect:
                    # Follow JS redirect to next page.
                    full_url = urljoin(session._base_url, redirect) if "://" not in redirect else redirect
                    logger.info("Following redirect → %s", full_url[:120])
                    resp = session._get(full_url)
                    form_data = _extract_hidden_fields(resp.text)
                    cpk = _extract_cafeportalkey(resp.text)
                    if cpk:
                        form_data["cafeportalkey"] = cpk
                    continue

                # Check if we're done.
                step_type = form_data.get("type", form_data.get("_raw", ""))
                if isinstance(step_type, str) and "success" in step_type.lower():
                    logger.info("Booking flow complete: %s", form_data)
                    return BookingResult(success=True, message="Booking submitted")

                # If we have a formName2, that tells us the next page.
                next_form = form_data.get("formName2", "")
                if not next_form:
                    logger.info("No formName2 in response — flow complete or blocked")
                    break

                logger.info("Submitting step → %s", next_form)
                form_data = session.submit_step(
                    step_name=next_form,
                    form_data=form_data,
                    page_path=f"{next_form}.aspx",
                )

            # 4. Final submission (if not dry_run).
            if dry_run:
                return BookingResult(
                    success=True,
                    message="Dry run: booking flow validated (stopped before final commit)",
                )

            return BookingResult(success=True, message="Booking submitted (check email)")

        except RentCafeBlockedError as exc:
            logger.error("RENTCafe booking blocked: %s", exc)
            return BookingResult(success=False, message=str(exc), should_retry=True)
        except RentCafeError as exc:
            logger.error("RENTCafe booking failed: %s", exc)
            return BookingResult(success=False, message=str(exc))
        except CaptchaError as exc:
            logger.error("reCAPTCHA solving failed: %s", exc)
            return BookingResult(success=False, message=str(exc), should_retry=True)
        except Exception as exc:
            logger.error("Unexpected booking error: %s", exc, exc_info=True)
            return BookingResult(success=False, message=str(exc))


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


def _extract_cafeportalkey(html: str) -> str:
    """Extract the encrypted session token ``cafeportalkey`` from a page."""
    m = re.search(r'name=["\']cafeportalkey["\'][^>]*value=["\']([^"\']+)["\']', html)
    return m.group(1) if m else ""


def _extract_js_redirect(html: str) -> str:
    """Extract ``window.location.href = '...'`` or ``window.location = '...'``."""
    m = re.search(
        r"""window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
        html,
    )
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# platform-specific subclasses (registered in bookers/__init__.py)
# ---------------------------------------------------------------------------


class XiorBooker(RentCafeBooker):
    """RentCafeBooker pre-bound to the ``"xior"`` source."""
    source = "xior"


class OurDomainBooker(RentCafeBooker):
    """RentCafeBooker pre-bound to the ``"ourdomain"`` source."""
    source = "ourdomain"
