"""
App Store Connect API CLI（自家自动化 metadata 工具）
=====================================================

用途
----
通过 ASC REST API 读 / 改 / 提交 App 元数据（description, keywords,
whatsNew, promotionalText, subtitle, app privacy answers）。

设计目标
--------
- 私钥永不出本机：``~/.config/asc/AuthKey_*.p8`` 由本工具读取，
  生成 10 分钟 ES256 JWT，token 仅在内存
- 写操作前**永远先 show 当前值**，由调用方（人 / Claude skill）确认后再 PATCH
- 提审类操作（submit-for-review / submit-metadata-only）需要 ``--yes``
  显式 flag，防误触
- 失败友好：缺 config 给清晰提示而不是 stacktrace；API 4xx 解析后端
  error.detail 后展示，不暴露原始 HTTP

配置文件
--------
``~/.config/asc/config.json``::

    {
      "key_id":    "ABCD1234EF",
      "issuer_id": "69a6de7c-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "p8_path":   "/Users/you/.config/asc/AuthKey_ABCD1234EF.p8",
      "bundle_id": "app.flatradar.ios"
    }

用法
----
    python tools/asc/asc_api.py status
    python tools/asc/asc_api.py show [--lang en-US]
    python tools/asc/asc_api.py add-locale zh-Hans
    python tools/asc/asc_api.py set --lang zh-Hans --field description --file zh-desc.md
    python tools/asc/asc_api.py set --lang zh-Hans --field keywords --value "kw1,kw2,..."
    python tools/asc/asc_api.py submit-metadata-only --yes

字段上限速查
------------
+---------------------+--------+
| Field               | Max    |
+=====================+========+
| name (app-level)    | 30     |
| subtitle            | 30     |
| description         | 4000   |
| whatsNew            | 4000   |
| keywords            | 100    |
| promotionalText     | 170    |
+---------------------+--------+
（API 也会强制；本地预校验给即时报错，省一次 round-trip）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# ── 字段上限（每个 localization） ──────────────────────────────────────
FIELD_LIMITS: dict[str, int] = {
    "name": 30,
    "subtitle": 30,
    "description": 4000,
    "keywords": 100,
    "whatsNew": 4000,
    "promotionalText": 170,
    "marketingUrl": 255,
    "supportUrl": 255,
    "privacyPolicyUrl": 255,
}

# version-level field 用的 attribute key（与 app-level localization 不同表）
VERSION_LOCALE_FIELDS = {
    "description", "keywords", "whatsNew", "promotionalText",
    "marketingUrl", "supportUrl",
}
APP_INFO_LOCALE_FIELDS = {
    "name", "subtitle", "privacyPolicyUrl",
}

ASC_BASE = "https://api.appstoreconnect.apple.com/v1"

# 兼容性常用 locale 别名 → ASC 规范名
LOCALE_ALIASES = {
    "zh": "zh-Hans", "zh_CN": "zh-Hans", "zh-CN": "zh-Hans",
    "zh_TW": "zh-Hant", "zh-TW": "zh-Hant",
    "en": "en-US", "en_US": "en-US",
    "nl_NL": "nl",
}


# ────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    key_id: str
    issuer_id: str
    p8_path: Path
    bundle_id: str


def _config_path() -> Path:
    # 允许 ASC_CONFIG 环境变量覆盖（CI / 多 app 切换）
    env = os.environ.get("ASC_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "asc" / "config.json"


def load_config() -> Config:
    p = _config_path()
    if not p.exists():
        die(f"配置文件不存在: {p}\n"
            f"请按 tools/asc/README.md 准备 config.json + AuthKey_*.p8")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"config.json 解析失败: {e}")

    required = ("key_id", "issuer_id", "p8_path", "bundle_id")
    missing = [k for k in required if not data.get(k)]
    if missing:
        die(f"config.json 缺字段: {', '.join(missing)}")

    p8 = Path(str(data["p8_path"])).expanduser()
    if not p8.exists():
        die(f"私钥文件不存在: {p8}")
    # 检查文件权限（仅警告，不致命）
    try:
        mode = p8.stat().st_mode & 0o777
        if mode & 0o077:
            eprint(f"⚠ 私钥权限过宽 ({oct(mode)}); 建议 chmod 600 {p8}")
    except OSError:
        pass

    return Config(
        key_id=str(data["key_id"]),
        issuer_id=str(data["issuer_id"]),
        p8_path=p8,
        bundle_id=str(data["bundle_id"]),
    )


# ────────────────────────────────────────────────────────────────────
# JWT
# ────────────────────────────────────────────────────────────────────

_token_cache: dict[str, tuple[str, float]] = {}


def jwt_for(cfg: Config) -> str:
    """生成 / 复用 ES256 JWT。ASC 允许的有效期上限 1200s（20 分钟），
    我们用 1000s 留 200s 安全边际；过期前 60s 强制刷新。
    """
    now = time.time()
    cached = _token_cache.get(cfg.key_id)
    if cached and cached[1] - now > 60:
        return cached[0]

    try:
        import jwt as pyjwt
    except ImportError:
        die("缺 PyJWT，请装: pip install 'pyjwt[crypto]>=2.8.0'")

    private_key = cfg.p8_path.read_text(encoding="utf-8")
    exp = int(now) + 1000
    token = pyjwt.encode(
        {
            "iss": cfg.issuer_id,
            "iat": int(now),
            "exp": exp,
            "aud": "appstoreconnect-v1",
        },
        private_key,
        algorithm="ES256",
        headers={"kid": cfg.key_id, "typ": "JWT"},
    )
    if isinstance(token, bytes):  # PyJWT 1.x 返 bytes
        token = token.decode()
    _token_cache[cfg.key_id] = (token, float(exp))
    return token


# ────────────────────────────────────────────────────────────────────
# HTTP
# ────────────────────────────────────────────────────────────────────

def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[dict] = None,
    dry_run: bool = False,
) -> tuple[int, dict]:
    """单点请求（用 urllib 避免新增依赖）。

    返回 ``(status_code, json_or_empty_dict)``。
    """
    import urllib.error
    import urllib.request

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}

    if dry_run and method != "GET":
        eprint(f"[dry-run] {method} {url}")
        if body is not None:
            eprint(f"[dry-run] body = {json.dumps(body, ensure_ascii=False, indent=2)}")
        return 200, {"dry_run": True}

    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        # ASC 错误响应是 JSON，提取 errors[].detail
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return e.code, parsed
    except urllib.error.URLError as e:
        die(f"网络错误: {e.reason}")


def call(
    cfg: Config,
    method: str,
    path: str,
    body: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """对 ASC API 发请求；HTTP 4xx/5xx 抛出 SystemExit + 解析错误展示。"""
    url = f"{ASC_BASE}{path}" if path.startswith("/") else f"{ASC_BASE}/{path}"
    headers = {"Authorization": f"Bearer {jwt_for(cfg)}"}
    status, payload = _http_request(method, url, headers, body, dry_run=dry_run)
    if status >= 400:
        errors = payload.get("errors") or []
        if errors:
            err_lines = []
            for e in errors:
                title = e.get("title", "")
                detail = e.get("detail", "")
                source = (e.get("source") or {}).get("pointer", "")
                err_lines.append(f"  [{e.get('code','?')}] {title}: {detail} {source}".rstrip())
            die(f"ASC API {status}:\n" + "\n".join(err_lines))
        die(f"ASC API {status}: {payload}")
    return payload


# ────────────────────────────────────────────────────────────────────
# Resource lookup helpers
# ────────────────────────────────────────────────────────────────────

def find_app(cfg: Config) -> dict:
    """按 bundle_id 找 app resource。"""
    r = call(cfg, "GET", f"/apps?filter[bundleId]={cfg.bundle_id}&limit=1")
    apps = r.get("data") or []
    if not apps:
        die(f"未找到 app: bundle_id={cfg.bundle_id}\n"
            f"确认 config.json 里的 bundle_id 与 ASC 上一致。")
    return apps[0]


def find_editable_version(cfg: Config, app_id: str) -> Optional[dict]:
    """找当前可编辑的 App Store Version（PREPARE_FOR_SUBMISSION /
    WAITING_FOR_REVIEW / DEVELOPER_REJECTED / METADATA_REJECTED 等可改）。

    注意：``GET /apps/{id}/appStoreVersions`` 这个嵌套端点不接受 ``sort``
    参数（400 PARAMETER_ERROR.ILLEGAL）。客户端拿全部后按 createdDate
    自己排。
    """
    r = call(
        cfg, "GET",
        f"/apps/{app_id}/appStoreVersions"
        f"?filter[platform]=IOS&limit=10"
    )
    versions = r.get("data") or []
    # 按 createdDate 倒序，最近的优先
    versions.sort(
        key=lambda v: v.get("attributes", {}).get("createdDate", ""),
        reverse=True,
    )
    EDITABLE = {
        "PREPARE_FOR_SUBMISSION",
        "WAITING_FOR_REVIEW",
        "DEVELOPER_REJECTED",
        "METADATA_REJECTED",
        "REJECTED",
        "INVALID_BINARY",
        "DEVELOPER_REMOVED_FROM_SALE",
    }
    for v in versions:
        st = v.get("attributes", {}).get("appStoreState", "")
        if st in EDITABLE:
            return v
    # 都不可编辑 = 当前没有进行中的版本（已 ready for sale 之类）
    return None


def find_app_info(cfg: Config, app_id: str) -> dict:
    """找当前可编辑的 app-level appInfo。

    Apple 在用户创建新版本时**会同时建一份 PREPARE_FOR_SUBMISSION 状态
    的草稿 AppInfo**，与线上的 READY_FOR_DISTRIBUTION 并列。改 name /
    subtitle 必须命中草稿那份，否则 409 INVALID_STATE。

    优先级：PREPARE_FOR_SUBMISSION > 其他可编辑状态 > READY_FOR_DISTRIBUTION
    （兜底，仍是只读但有 localization 内容可看）
    """
    r = call(cfg, "GET", f"/apps/{app_id}/appInfos?limit=10")
    infos = r.get("data") or []
    if not infos:
        die("未找到 appInfo（罕见情况，请检查 ASC 状态）")

    PRIORITY = [
        "PREPARE_FOR_SUBMISSION",
        "DEVELOPER_REJECTED",
        "REJECTED",
        "METADATA_REJECTED",
        "READY_FOR_DISTRIBUTION",  # 兜底，只读
    ]
    for state in PRIORITY:
        for info in infos:
            if info.get("attributes", {}).get("state") == state:
                return info
    return infos[0]


def list_version_localizations(cfg: Config, version_id: str) -> list[dict]:
    r = call(cfg, "GET", f"/appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=50")
    return r.get("data") or []


def list_app_info_localizations(cfg: Config, app_info_id: str) -> list[dict]:
    r = call(cfg, "GET", f"/appInfos/{app_info_id}/appInfoLocalizations?limit=50")
    return r.get("data") or []


def normalize_locale(locale: str) -> str:
    return LOCALE_ALIASES.get(locale, locale)


# ────────────────────────────────────────────────────────────────────
# Commands
# ────────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    app = find_app(cfg)
    attrs = app.get("attributes", {})
    print(f"App: {attrs.get('name', '?')}  ({attrs.get('bundleId', '?')})")
    print(f"  id = {app['id']}")
    print(f"  sku = {attrs.get('sku', '?')}")
    print(f"  primaryLocale = {attrs.get('primaryLocale', '?')}")
    print()

    v = find_editable_version(cfg, app["id"])
    if v:
        va = v["attributes"]
        print(f"可编辑版本: {va.get('versionString', '?')}  state={va.get('appStoreState')}")
        print(f"  id = {v['id']}")
        locs = list_version_localizations(cfg, v["id"])
        print(f"  localizations ({len(locs)}):")
        for L in locs:
            la = L["attributes"]
            print(f"    - {la.get('locale')}  desc={len(la.get('description') or '')}c  "
                  f"kw={len(la.get('keywords') or '')}c")
    else:
        print("当前没有可编辑版本（已上架且无新版进行中）。")
    print()

    info = find_app_info(cfg, app["id"])
    print(f"AppInfo: id={info['id']}  state={info['attributes'].get('state')}")
    locs = list_app_info_localizations(cfg, info["id"])
    print(f"  localizations ({len(locs)}):")
    for L in locs:
        la = L["attributes"]
        print(f"    - {la.get('locale')}  name={la.get('name','')!r}  "
              f"subtitle={la.get('subtitle','')!r}")


def cmd_show(args: argparse.Namespace) -> None:
    cfg = load_config()
    app = find_app(cfg)
    v = find_editable_version(cfg, app["id"])
    info = find_app_info(cfg, app["id"])

    target_locale = normalize_locale(args.lang) if args.lang else None
    full = bool(args.full)

    if v:
        for L in list_version_localizations(cfg, v["id"]):
            la = L["attributes"]
            if target_locale and la.get("locale") != target_locale:
                continue
            _print_loc("VERSION", la, full=full)

    for L in list_app_info_localizations(cfg, info["id"]):
        la = L["attributes"]
        if target_locale and la.get("locale") != target_locale:
            continue
        _print_loc("APP_INFO", la, full=full)


def _print_loc(kind: str, la: dict, full: bool = False) -> None:
    print(f"━━ {kind} · {la.get('locale')} ━━")
    for key in ("name", "subtitle",
                "description", "keywords", "whatsNew", "promotionalText",
                "marketingUrl", "supportUrl", "privacyPolicyUrl"):
        v = la.get(key)
        if v is None:
            continue
        s = str(v)
        if full or len(s) <= 200:
            print(f"  {key} [{len(s)}c]:")
            for line in s.splitlines() or [""]:
                print(f"    │ {line}")
        else:
            head = s[:200] + f"… ({len(s)}c)"
            print(f"  {key} [{len(s)}c]: {head!r}")
    print()


def cmd_add_locale(args: argparse.Namespace) -> None:
    cfg = load_config()
    locale = normalize_locale(args.locale)
    app = find_app(cfg)

    # Version 层
    v = find_editable_version(cfg, app["id"])
    if v:
        existing = {L["attributes"]["locale"] for L in list_version_localizations(cfg, v["id"])}
        if locale in existing:
            print(f"version localization {locale} 已存在")
        else:
            call(cfg, "POST", "/appStoreVersionLocalizations", body={
                "data": {
                    "type": "appStoreVersionLocalizations",
                    "attributes": {"locale": locale},
                    "relationships": {
                        "appStoreVersion": {
                            "data": {"type": "appStoreVersions", "id": v["id"]}
                        }
                    }
                }
            }, dry_run=args.dry_run)
            print(f"✅ 已为版本 {v['attributes']['versionString']} 加上 {locale}")
    else:
        print("⚠ 没有可编辑版本，跳过 version-level localization")

    # AppInfo 层
    info = find_app_info(cfg, app["id"])
    existing_info = {L["attributes"]["locale"] for L in list_app_info_localizations(cfg, info["id"])}
    if locale in existing_info:
        print(f"appInfo localization {locale} 已存在")
        return
    call(cfg, "POST", "/appInfoLocalizations", body={
        "data": {
            "type": "appInfoLocalizations",
            "attributes": {"locale": locale},
            "relationships": {
                "appInfo": {"data": {"type": "appInfos", "id": info["id"]}}
            }
        }
    }, dry_run=args.dry_run)
    print(f"✅ AppInfo 已加 {locale}")


def cmd_set(args: argparse.Namespace) -> None:
    cfg = load_config()
    locale = normalize_locale(args.lang)
    field = args.field

    # 读 value
    if args.value is not None:
        value = args.value
    elif args.file:
        value = Path(args.file).read_text(encoding="utf-8").rstrip("\n")
    else:
        value = sys.stdin.read().rstrip("\n")

    limit = FIELD_LIMITS.get(field)
    if limit and len(value) > limit:
        die(f"字段 {field} 长度 {len(value)} 超过上限 {limit}")

    app = find_app(cfg)

    # 判断字段属于 version 还是 appInfo 层
    if field in VERSION_LOCALE_FIELDS:
        v = find_editable_version(cfg, app["id"])
        if not v:
            die("没有可编辑版本，无法设 version-level 字段")
        loc = _find_loc_by_locale(
            list_version_localizations(cfg, v["id"]), locale, "version")
        before = loc["attributes"].get(field) or ""
        _print_diff(field, before, value)
        if not args.yes:
            confirm = input("应用以上变更？[y/N] ").strip().lower()
            if confirm != "y":
                print("已取消")
                return
        call(cfg, "PATCH", f"/appStoreVersionLocalizations/{loc['id']}",
             body={"data": {
                 "type": "appStoreVersionLocalizations",
                 "id": loc["id"],
                 "attributes": {field: value},
             }}, dry_run=args.dry_run)
        print(f"✅ 已更新 {locale} · {field} ({len(value)}c)")

    elif field in APP_INFO_LOCALE_FIELDS:
        info = find_app_info(cfg, app["id"])
        loc = _find_loc_by_locale(
            list_app_info_localizations(cfg, info["id"]), locale, "appInfo")
        before = loc["attributes"].get(field) or ""
        _print_diff(field, before, value)
        if not args.yes:
            confirm = input("应用以上变更？[y/N] ").strip().lower()
            if confirm != "y":
                print("已取消")
                return
        call(cfg, "PATCH", f"/appInfoLocalizations/{loc['id']}",
             body={"data": {
                 "type": "appInfoLocalizations",
                 "id": loc["id"],
                 "attributes": {field: value},
             }}, dry_run=args.dry_run)
        print(f"✅ 已更新 {locale} · {field} ({len(value)}c)")

    else:
        die(f"未知字段 {field!r}。version-level: {sorted(VERSION_LOCALE_FIELDS)};\n"
            f"appInfo-level: {sorted(APP_INFO_LOCALE_FIELDS)}")


def _find_loc_by_locale(locs: list[dict], locale: str, kind: str) -> dict:
    for L in locs:
        if L["attributes"].get("locale") == locale:
            return L
    die(f"{kind} 没有 {locale} localization，先跑: "
        f"asc_api.py add-locale {locale}")


def _print_diff(field: str, before: str, after: str) -> None:
    print(f"━━ Field: {field} ━━")
    print(f"  Before [{len(before)}c]:")
    for line in (before or "(empty)").splitlines() or ["(empty)"]:
        print(f"    │ {line}")
    print(f"  After  [{len(after)}c]:")
    for line in (after or "(empty)").splitlines() or ["(empty)"]:
        print(f"    │ {line}")
    print()


def cmd_submit_metadata_only(args: argparse.Namespace) -> None:
    """触发"仅元数据"审核（不重新上传 build）。"""
    if not args.yes:
        die("submit-metadata-only 必须显式 --yes 确认。")

    cfg = load_config()
    app = find_app(cfg)
    v = find_editable_version(cfg, app["id"])
    if not v:
        die("没有可编辑版本可提交")

    # ASC 提审 = 创建 appStoreVersionSubmission，关联到 version id
    call(cfg, "POST", "/appStoreVersionSubmissions", body={
        "data": {
            "type": "appStoreVersionSubmissions",
            "relationships": {
                "appStoreVersion": {
                    "data": {"type": "appStoreVersions", "id": v["id"]}
                }
            }
        }
    }, dry_run=args.dry_run)
    print(f"✅ 已提交版本 {v['attributes']['versionString']} 至审核")


# ────────────────────────────────────────────────────────────────────
# Utility
# ────────────────────────────────────────────────────────────────────

def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def die(msg: str) -> None:
    eprint(f"❌ {msg}")
    sys.exit(1)


# ────────────────────────────────────────────────────────────────────
# CLI parser
# ────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(
        prog="asc_api.py",
        description="App Store Connect metadata CLI（自家自动化工具）",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="所有非 GET 请求只打印不真发")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="显示 app 状态 + 可编辑版本 + localizations 总览")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("show", help="打印每个 localization 的当前文本内容")
    sp.add_argument("--lang", default=None, help="只看某个 locale（默认全部）")
    sp.add_argument("--full", action="store_true",
                    help="完整打印长文本（默认 description 等长字段截断到 200c）")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("add-locale", help="新增一个 localization（version + appInfo 两层都加）")
    sp.add_argument("locale", help="如 zh-Hans / zh-Hant / nl / fr-FR")
    sp.set_defaults(func=cmd_add_locale)

    sp = sub.add_parser("set", help="修改单个字段")
    sp.add_argument("--lang", required=True, help="locale 如 en-US / zh-Hans")
    sp.add_argument("--field", required=True,
                    help=f"version: {sorted(VERSION_LOCALE_FIELDS)}; "
                         f"appInfo: {sorted(APP_INFO_LOCALE_FIELDS)}")
    sp.add_argument("--value", default=None, help="字段值（短文案）")
    sp.add_argument("--file", default=None, help="从文件读字段值（长文案）")
    sp.add_argument("--yes", action="store_true", help="跳过 diff 确认")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("submit-metadata-only",
                        help="提交当前可编辑版本进入审核（仅元数据）")
    sp.add_argument("--yes", action="store_true", required=False,
                    help="必填，二次确认")
    sp.set_defaults(func=cmd_submit_metadata_only)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
