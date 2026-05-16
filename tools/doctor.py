"""
Pre-release deployment health checks.

Run:
    python -m tools.doctor
    python -m tools.doctor --no-network

The command is intentionally read-only: it never writes configuration, sends
notifications, starts/stops monitor processes, or creates database schema.
"""
from __future__ import annotations

import argparse
import os
import platform
import smtplib
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import request as urllib_request

from dotenv import dotenv_values

from config import BASE_DIR, DATA_DIR, DB_PATH, ENV_PATH, get_impersonate, get_proxy_url


Status = str
OK: Status = "OK"
WARN: Status = "WARN"
FAIL: Status = "FAIL"
SKIP: Status = "SKIP"


@dataclass
class CheckResult:
    status: Status
    name: str
    message: str
    detail: str = ""


@dataclass
class DoctorContext:
    env_path: Path
    env: dict[str, str]
    no_network: bool
    smtp_login: bool
    timeout: float


def _mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..." + "*" * min(8, max(0, len(value) - keep))


def _bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_duplicates(path: Path) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    if not path.exists():
        return dupes
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in seen and key not in dupes:
            dupes.append(key)
        seen.add(key)
    return dupes


def _path_writable(path: Path) -> tuple[bool, str]:
    path.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(prefix=".doctor-", dir=path, delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _section(title: str) -> None:
    print(f"\n== {title} ==")


def _emit(results: Iterable[CheckResult]) -> tuple[int, int]:
    fails = 0
    warns = 0
    for r in results:
        if r.status == FAIL:
            fails += 1
        elif r.status == WARN:
            warns += 1
        print(f"[{r.status:<4}] {r.name}: {r.message}")
        if r.detail:
            for line in r.detail.splitlines():
                print(f"       {line}")
    return fails, warns


def check_env(ctx: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    env = ctx.env
    if ctx.env_path.exists():
        results.append(CheckResult(OK, ".env", f"found at {ctx.env_path}"))
    else:
        results.append(CheckResult(FAIL, ".env", f"missing at {ctx.env_path}"))
        return results

    dupes = _env_duplicates(ctx.env_path)
    if dupes:
        results.append(CheckResult(WARN, ".env duplicates", ", ".join(sorted(dupes))))
    else:
        results.append(CheckResult(OK, ".env duplicates", "no duplicate keys found"))

    web_password = env.get("WEB_PASSWORD", "").strip()
    if web_password:
        results.append(CheckResult(OK, "WEB_PASSWORD", "set"))
    else:
        results.append(CheckResult(WARN, "WEB_PASSWORD", "empty; public deployments should require login"))

    flask_secret = env.get("FLASK_SECRET", "").strip()
    if len(flask_secret) >= 32:
        results.append(CheckResult(OK, "FLASK_SECRET", "set"))
    elif flask_secret:
        results.append(CheckResult(WARN, "FLASK_SECRET", "set but short; use a high-entropy value"))
    else:
        results.append(CheckResult(WARN, "FLASK_SECRET", "empty; app may auto-generate, existing sessions may reset"))

    if _bool(env.get("SESSION_COOKIE_SECURE", "")):
        results.append(CheckResult(OK, "SESSION_COOKIE_SECURE", "true"))
    else:
        results.append(CheckResult(WARN, "SESSION_COOKIE_SECURE", "false; set true behind HTTPS/Caddy"))

    db_raw = env.get("DB_PATH", "data/listings.db").strip() or "data/listings.db"
    results.append(CheckResult(OK, "DB_PATH", str(DB_PATH), f"raw value: {db_raw}"))
    return results


def check_paths(_: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for label, path in (
        ("BASE_DIR", BASE_DIR),
        ("DATA_DIR", DATA_DIR),
        ("DB parent", DB_PATH.parent),
        ("logs dir", BASE_DIR / "logs"),
    ):
        ok, detail = _path_writable(path)
        if ok:
            results.append(CheckResult(OK, label, f"writable: {path}"))
        else:
            results.append(CheckResult(FAIL, label, f"not writable: {path}", detail))
    return results


def check_database(_: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not DB_PATH.exists():
        return [CheckResult(WARN, "SQLite database", f"not found: {DB_PATH}; monitor will create it on first run")]

    uri = f"file:{DB_PATH}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except Exception as exc:
        return [CheckResult(FAIL, "SQLite database", f"cannot open read-only: {DB_PATH}", str(exc))]

    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        status = OK if integrity == "ok" else FAIL
        results.append(CheckResult(status, "SQLite integrity", integrity))

        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {r[0] for r in rows}
        required = {
            "listings", "status_changes", "meta", "web_notifications",
            "geocode_cache", "app_tokens", "device_tokens",
        }
        missing = sorted(required - tables)
        if missing:
            results.append(CheckResult(WARN, "SQLite schema", "missing tables", ", ".join(missing)))
        else:
            results.append(CheckResult(OK, "SQLite schema", "required tables present"))

        count = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0] if "listings" in tables else 0
        results.append(CheckResult(OK, "SQLite listings", f"{count} rows"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "SQLite database", "query failed", str(exc)))
    finally:
        con.close()
    return results


def check_users(_: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        from app.db import storage
        from users import USERS_FILE, USERS_MIGRATION_META_KEY, load_users

        users = load_users()
    except RuntimeError as exc:
        return [CheckResult(FAIL, "user_configs", "legacy migration failed", str(exc))]
    except Exception as exc:
        return [CheckResult(FAIL, "user_configs", "cannot load", str(exc))]

    enabled = sum(1 for u in users if u.enabled)
    results.append(CheckResult(OK, "user_configs", f"{len(users)} users ({enabled} enabled)"))

    try:
        st = storage()
        try:
            migrated = st.get_meta(USERS_MIGRATION_META_KEY, default="")
        finally:
            st.close()
        if migrated != "1":
            results.append(CheckResult(WARN, "user migration", "meta flag not set"))
    except Exception as exc:
        results.append(CheckResult(WARN, "user migration", "cannot read meta flag", str(exc)))

    if USERS_FILE.exists():
        backups = sorted(USERS_FILE.parent.glob(f"{USERS_FILE.name}.migrated.*.bak"))
        msg = "legacy users.json still present"
        if backups:
            msg += f"; {len(backups)} migration backup(s) found"
        results.append(CheckResult(WARN, "legacy users.json", msg, str(USERS_FILE)))

    if users and not any(u.enabled for u in users):
        results.append(CheckResult(WARN, "enabled users", "all users are disabled"))
    if users and not any(u.notifications_enabled and u.notification_channels for u in users if u.enabled):
        results.append(CheckResult(WARN, "notification channels", "no enabled user has notification channels"))
    return results


def check_caddy(_: DoctorContext) -> list[CheckResult]:
    path = BASE_DIR / "Caddyfile"
    if not path.exists():
        return [CheckResult(WARN, "Caddyfile", f"not found at {path}; OK for local/non-Caddy deployments")]
    text = path.read_text(encoding="utf-8", errors="replace")
    results = [CheckResult(OK, "Caddyfile", f"found at {path}")]
    if "your.domain.com" in text:
        results.append(CheckResult(FAIL, "Caddyfile domain", "still uses placeholder your.domain.com"))
    else:
        results.append(CheckResult(OK, "Caddyfile domain", "placeholder not present"))
    if "reverse_proxy" in text and "8088" in text:
        results.append(CheckResult(OK, "Caddy reverse proxy", "points to app port 8088"))
    else:
        results.append(CheckResult(WARN, "Caddy reverse proxy", "could not find reverse_proxy to port 8088"))
    return results


def check_apns(ctx: DoctorContext) -> list[CheckResult]:
    env = ctx.env
    if not _bool(env.get("APNS_ENABLED", "")):
        return [CheckResult(SKIP, "APNs", "APNS_ENABLED is not true")]

    results: list[CheckResult] = []
    missing = [
        key for key in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC")
        if not env.get(key, "").strip()
    ]
    if missing:
        results.append(CheckResult(FAIL, "APNs config", "missing required keys", ", ".join(missing)))
        return results
    results.append(CheckResult(OK, "APNs config", "required keys set"))

    key_path = Path(env["APNS_KEY_PATH"]).expanduser()
    if not key_path.is_absolute():
        key_path = BASE_DIR / key_path
    if not key_path.exists():
        results.append(CheckResult(FAIL, "APNs key", f"missing: {key_path}"))
    elif not os.access(key_path, os.R_OK):
        results.append(CheckResult(FAIL, "APNs key", f"not readable: {key_path}"))
    else:
        head = key_path.read_text(encoding="utf-8", errors="replace")[:120]
        if "PRIVATE KEY" in head:
            results.append(CheckResult(OK, "APNs key", f"readable: {key_path}"))
        else:
            results.append(CheckResult(WARN, "APNs key", "file is readable but does not look like a .p8 private key"))

    env_default = env.get("APNS_ENV_DEFAULT", "production").strip().lower()
    if env_default in {"production", "sandbox"}:
        results.append(CheckResult(OK, "APNS_ENV_DEFAULT", env_default))
    else:
        results.append(CheckResult(WARN, "APNS_ENV_DEFAULT", f"unexpected value: {env_default}"))
    return results


def _email_users() -> list:
    try:
        from users import load_users

        return [u for u in load_users() if "email" in [c.lower() for c in u.notification_channels]]
    except Exception:
        return []


def check_smtp(ctx: DoctorContext) -> list[CheckResult]:
    users = _email_users()
    if not users:
        return [CheckResult(SKIP, "SMTP", "no user has Email channel enabled")]

    results: list[CheckResult] = []
    for user in users:
        prefix = f"SMTP [{user.name}]"
        has_auth = bool(user.email_username or user.email_password)
        missing = []
        if not user.email_smtp_host:
            missing.append("host")
        if not user.email_to:
            missing.append("to")
        if not (user.email_from or user.email_username):
            missing.append("from/username")
        if has_auth and not (user.email_username and user.email_password):
            missing.append("username+password")
        if missing:
            results.append(CheckResult(FAIL, prefix, "incomplete Email settings", ", ".join(missing)))
            continue

        sec = (user.email_smtp_security or "starttls").lower()
        if sec not in {"starttls", "ssl", "none", "tls", "smtps", "plain"}:
            results.append(CheckResult(WARN, prefix, f"unknown security mode {sec!r}; app will normalize to STARTTLS"))
        else:
            results.append(CheckResult(OK, prefix, f"{user.email_smtp_host}:{user.email_smtp_port} ({sec})"))

        if ctx.no_network:
            results.append(CheckResult(SKIP, f"{prefix} network", "--no-network"))
            continue
        try:
            if sec in {"ssl", "smtps"}:
                with smtplib.SMTP_SSL(user.email_smtp_host, user.email_smtp_port, timeout=ctx.timeout) as client:
                    client.noop()
            else:
                with smtplib.SMTP(user.email_smtp_host, user.email_smtp_port, timeout=ctx.timeout) as client:
                    client.ehlo()
                    if sec in {"starttls", "tls"}:
                        client.starttls()
                        client.ehlo()
                    if ctx.smtp_login and user.email_username:
                        client.login(user.email_username, user.email_password)
            msg = "handshake OK"
            if ctx.smtp_login and user.email_username:
                msg += " + login OK"
            elif user.email_username:
                msg += " (login not tested; pass --smtp-login to test auth)"
            results.append(CheckResult(OK, f"{prefix} network", msg))
        except Exception as exc:
            results.append(CheckResult(FAIL, f"{prefix} network", "SMTP connection/auth failed", str(exc)))
    return results


def check_proxy(ctx: DoctorContext) -> list[CheckResult]:
    proxy = get_proxy_url()
    if not proxy:
        return [CheckResult(WARN, "Proxy", "no HTTP_PROXY / HTTPS_PROXY / ALL_PROXY configured")]

    results = [CheckResult(OK, "Proxy config", _mask_secret(proxy, keep=16))]
    if ctx.no_network:
        results.append(CheckResult(SKIP, "Proxy network", "--no-network"))
        return results

    try:
        opener = urllib_request.build_opener(
            urllib_request.ProxyHandler({"http": proxy, "https": proxy})
        )
        with opener.open("https://api.ipify.org", timeout=ctx.timeout) as resp:
            ip = resp.read(100).decode("utf-8", errors="replace").strip()
        results.append(CheckResult(OK, "Proxy exit IP", ip))
    except Exception as exc:
        results.append(CheckResult(FAIL, "Proxy exit IP", "failed to fetch through proxy", str(exc)))
    return results


def check_h2s_network(ctx: DoctorContext) -> list[CheckResult]:
    if ctx.no_network:
        return [CheckResult(SKIP, "H2S GraphQL", "--no-network")]
    proxy = get_proxy_url()
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    try:
        import curl_cffi.requests as req

        resp = req.post(
            "https://api.holland2stay.com/graphql/",
            json={"query": "{ __typename }"},
            headers={
                "Content-Type": "application/json",
                "Origin": "https://www.holland2stay.com",
                "Referer": "https://www.holland2stay.com/",
                "Accept": "application/json",
            },
            impersonate=get_impersonate(),
            proxies=proxies,
            timeout=ctx.timeout,
        )
        body = resp.text[:220].replace("\n", "\\n")
        if resp.status_code == 200:
            return [CheckResult(OK, "H2S GraphQL", "HTTP 200")]
        if resp.status_code in {403, 429}:
            return [CheckResult(FAIL, "H2S GraphQL", f"HTTP {resp.status_code}; proxy/IP likely blocked", body)]
        return [CheckResult(WARN, "H2S GraphQL", f"HTTP {resp.status_code}", body)]
    except Exception as exc:
        return [CheckResult(FAIL, "H2S GraphQL", "request failed", str(exc))]


def check_supervisor(_: DoctorContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    conf = BASE_DIR / "docker" / "supervisord.conf"
    if conf.exists():
        text = conf.read_text(encoding="utf-8", errors="replace")
        needed = ("[unix_http_server]", "[rpcinterface:supervisor]", "[supervisorctl]")
        missing = [part for part in needed if part not in text]
        if missing:
            results.append(CheckResult(FAIL, "Supervisor config", "missing supervisorctl sections", ", ".join(missing)))
        else:
            results.append(CheckResult(OK, "Supervisor config", "supervisorctl socket configured"))
    else:
        results.append(CheckResult(WARN, "Supervisor config", f"missing: {conf}"))

    in_docker = Path("/.dockerenv").exists() or os.environ.get("container")
    if not in_docker:
        results.append(CheckResult(SKIP, "Supervisor runtime", "not running inside Docker container"))
        return results

    try:
        from app.process_ctrl import supervisorctl_available, supervisorctl_monitor

        if not supervisorctl_available():
            results.append(CheckResult(FAIL, "Supervisor runtime", "supervisorctl is not available"))
            return results
        r = supervisorctl_monitor("status")
        status = OK if r.returncode == 0 else FAIL
        results.append(CheckResult(status, "Supervisor runtime", "status checked", (r.stdout or r.stderr).strip()))
    except Exception as exc:
        results.append(CheckResult(FAIL, "Supervisor runtime", "status failed", str(exc)))
    return results


def build_context(args: argparse.Namespace) -> DoctorContext:
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    env = {k: str(v or "") for k, v in values.items()}
    # Include process env because Docker injects proxy/APNS overrides this way.
    for k, v in os.environ.items():
        if k.startswith(("APNS_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")):
            env.setdefault(k, v)
    return DoctorContext(
        env_path=ENV_PATH,
        env=env,
        no_network=args.no_network,
        smtp_login=args.smtp_login,
        timeout=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FlatRadar pre-release health checks.")
    parser.add_argument("--no-network", action="store_true", help="skip proxy/H2S/SMTP network probes")
    parser.add_argument("--smtp-login", action="store_true", help="also test SMTP username/password login")
    parser.add_argument("--timeout", type=float, default=8.0, help="network timeout in seconds")
    args = parser.parse_args(argv)

    ctx = build_context(args)
    print("FlatRadar doctor")
    print(f"Python: {sys.version.split()[0]} ({platform.system()} {platform.release()})")
    print(f"Base:   {BASE_DIR}")

    total_fails = 0
    total_warns = 0
    sections = [
        ("Environment", check_env),
        ("Paths", check_paths),
        ("Database", check_database),
        ("Users", check_users),
        ("Caddy", check_caddy),
        ("APNs", check_apns),
        ("SMTP", check_smtp),
        ("Proxy", check_proxy),
        ("Holland2Stay", check_h2s_network),
        ("Docker Supervisor", check_supervisor),
    ]
    for title, func in sections:
        _section(title)
        fails, warns = _emit(func(ctx))
        total_fails += fails
        total_warns += warns

    print("\n== Summary ==")
    if total_fails:
        print(f"FAIL: {total_fails} failure(s), {total_warns} warning(s)")
        return 1
    if total_warns:
        print(f"WARN: 0 failures, {total_warns} warning(s)")
        return 0
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
