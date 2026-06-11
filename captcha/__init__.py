"""captcha — reCAPTCHA solving abstraction for auto-booking.

Currently wraps 2Captcha. Designed so providers can be swapped by
implementing the same CaptchaSolver interface.
"""

from .solver import CaptchaSolver, CaptchaError, RENTCAFE_V2_SITEKEY, RENTCAFE_V3_SITEKEY

__all__ = [
    "CaptchaSolver",
    "CaptchaError",
    "RENTCAFE_V2_SITEKEY",
    "RENTCAFE_V3_SITEKEY",
]
