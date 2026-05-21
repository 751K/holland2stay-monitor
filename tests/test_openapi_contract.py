from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "openapi.json"


EXPECTED_PATH_METHODS = {
    "/auth/login": {"post"},
    "/auth/register": {"post"},
    "/auth/logout": {"post"},
    "/auth/me": {"get"},
    "/auth/password": {"post"},
    "/stats/public/summary": {"get"},
    "/stats/public/charts": {"get"},
    "/stats/public/charts/{key}": {"get"},
    "/listings": {"get"},
    "/listings/{listing_id}": {"get"},
    "/map": {"get"},
    "/calendar": {"get"},
    "/notifications": {"get"},
    "/notifications/read": {"post"},
    "/notifications/stream": {"get"},
    "/me/summary": {"get"},
    "/me/filter": {"get", "put"},
    "/me": {"delete"},
    "/me/export": {"get"},
    "/filter/options": {"get"},
    "/devices/register": {"post"},
    "/devices": {"get"},
    "/devices/{device_id}": {"delete"},
    "/devices/test": {"post"},
    "/feedback": {"post"},
    "/diagnostics/crash": {"post"},
    "/admin/users": {"get"},
    "/admin/users/{user_id}/toggle": {"post"},
    "/admin/users/{user_id}": {"delete"},
    "/admin/monitor/status": {"get"},
    "/admin/monitor/start": {"post"},
    "/admin/monitor/stop": {"post"},
    "/admin/monitor/reload": {"post"},
}


def _load_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_openapi_json_is_parseable_and_declares_version() -> None:
    spec = _load_openapi()

    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "FlatRadar Backend API"
    assert spec["info"]["version"]
    assert spec["servers"][0]["url"].endswith("/api/v1")


def test_openapi_covers_current_mobile_api_paths() -> None:
    spec = _load_openapi()
    paths = spec["paths"]

    assert set(EXPECTED_PATH_METHODS).issubset(paths)
    for path, expected_methods in EXPECTED_PATH_METHODS.items():
        assert expected_methods.issubset(paths[path]), path


def test_openapi_defines_shared_mobile_contract_schemas() -> None:
    spec = _load_openapi()
    schemas = spec["components"]["schemas"]

    for name in [
        "SuccessEnvelope",
        "ErrorEnvelope",
        "ApiErrorCode",
        "Listing",
        "ListingFilter",
        "Notification",
        "DeviceRegisterRequest",
        "ChartKey",
    ]:
        assert name in schemas

    assert set(schemas["ApiErrorCode"]["enum"]) == {
        "unauthorized",
        "forbidden",
        "not_found",
        "validation",
        "conflict",
        "rate_limited",
        "server_error",
    }

    assert "android" in schemas["DeviceRegisterRequest"]["properties"]["platform"]["enum"]
