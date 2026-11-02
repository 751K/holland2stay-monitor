"""Resolve a Web session's personal listing scope without widening on failure."""
from __future__ import annotations

import logging

from flask import abort

from app.auth import current_user_id, is_user


def current_listing_user():
    if not is_user():
        return None
    from users import get_user, load_users

    try:
        user = get_user(load_users(), current_user_id())
    except Exception:
        logging.getLogger(__name__).exception("Unable to load personal listing scope")
        abort(503)
    if user is None or not user.enabled or not user.app_login_enabled:
        abort(401)
    return user
