"""Canonical legal texts. Single source of truth for all platforms."""

from pathlib import Path

_dir = Path(__file__).parent

_terms_en = (_dir / "terms.txt").read_text()
_privacy_en = (_dir / "privacy.txt").read_text()
_terms_zh = (_dir / "termszh.txt").read_text()
_privacy_zh = (_dir / "privacyzh.txt").read_text()

UPDATED_AT = "2026-05-25"


def get_legal(lang: str = "en") -> dict:
    """Return {"terms": ..., "privacy": ..., "updated_at": ...} for the given language."""
    if lang.startswith("zh"):
        return {
            "terms": _terms_zh,
            "privacy": _privacy_zh,
            "updated_at": UPDATED_AT,
        }
    return {
        "terms": _terms_en,
        "privacy": _privacy_en,
        "updated_at": UPDATED_AT,
    }
