"""Public legal endpoint — canonical terms + privacy for all platforms."""

from __future__ import annotations

from flask import Blueprint, request

from app.legal import get_legal
from app.api_errors import ok


def register(bp: Blueprint) -> None:
    @bp.get("/legal")
    def legal():
        lang = (request.args.get("lang") or "").strip().lower()[:8]
        if not lang:
            accept = request.headers.get("Accept-Language", "")
            lang = accept.split(",")[0].split(";")[0].strip() if accept else "en"
        return ok(get_legal(lang))
