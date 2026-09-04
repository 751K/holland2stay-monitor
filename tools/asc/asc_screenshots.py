#!/usr/bin/env python3
"""把截图上传到 App Store Connect。

``asc_api.py`` 只管文本元数据；截图是另一套 API，流程也不一样——不是一次
PATCH，而是**三步**：

1. ``POST /appScreenshots`` 预留一个位置，声明文件名与字节数。响应里带回
   ``uploadOperations``（一个或多个 HTTP 请求的描述）。
2. 按 ``uploadOperations`` 逐段 PUT 文件内容。大文件会被切成多段，段数与偏移
   由服务端决定，不能自己拆。
3. ``PATCH`` 该 screenshot，``uploaded=true`` 并附上文件的 MD5。**这一步不做
   等于白传**：前两步都成功，但资源停在 AWAITING_UPLOAD，商店页面上看不到。

顺序与替换
----------
同一个 screenshotSet 内的顺序就是商店里的展示顺序，由上传顺序决定。``replace``
模式会先删掉集合里已有的截图——不删的话新图追加在后面，商店里会同时出现新旧
两版。

用法::

    python3 asc_screenshots.py list --version 2.0.0
    python3 asc_screenshots.py upload --version 2.0.0 --lang en-US \\
        --display-type APP_IPHONE_67 --dir path/to/pngs [--replace] [--yes]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import pathlib
from pathlib import Path

import jwt

CONFIG = Path.home() / ".config" / "asc" / "config.json"
BASE = "https://api.appstoreconnect.apple.com/v1/"


def _cfg() -> dict:
    if not CONFIG.exists():
        print(f"配置文件不存在: {CONFIG}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(CONFIG.read_text())


def _token(cfg: dict) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": cfg["issuer_id"], "iat": now, "exp": now + 900,
         "aud": "appstoreconnect-v1"},
        Path(cfg["p8_path"]).read_text(),
        algorithm="ES256", headers={"kid": cfg["key_id"], "typ": "JWT"})


def _req(cfg, method, path, body=None, absolute=False, raw=None, headers=None):
    url = path if absolute else BASE + path
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    # 预签名的上传 URL **不能**带 Authorization。
    #
    # 那些 URL 自带签名，再附一个 Bearer 头，对端直接 400 Invalid request：
    #   <Error><Code>400</Code><Message>Invalid request: </Message></Error>
    # 只有 App Store Connect 自己的 API 需要 Bearer。
    h: dict[str, str] = {} if absolute else {"Authorization": "Bearer " + _token(cfg)}
    if raw is None and body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        payload = resp.read()
        return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:400].decode("utf-8", "ignore")}


def _die(code, out, what):
    print(f"{what} 失败 HTTP {code}", file=sys.stderr)
    for err in out.get("errors", []) or [out]:
        print("  ", err.get("title", ""), "|", err.get("detail", err), file=sys.stderr)
    raise SystemExit(1)


def _get(cfg, path, **params):
    code, out = _req(cfg, "GET", path + ("?" + urllib.parse.urlencode(params) if params else ""))
    if code != 200:
        _die(code, out, f"GET {path}")
    return out


def _version_id(cfg, version: str) -> str:
    app = _get(cfg, "apps", **{"filter[bundleId]": cfg["bundle_id"]})["data"]
    if not app:
        print("未找到 app:", cfg["bundle_id"], file=sys.stderr)
        raise SystemExit(1)
    for v in _get(cfg, f"apps/{app[0]['id']}/appStoreVersions", **{"limit": 20})["data"]:
        if v["attributes"]["versionString"] == version:
            return v["id"]
    print(f"未找到版本 {version}", file=sys.stderr)
    raise SystemExit(1)


def _localization_id(cfg, version_id: str, locale: str) -> str:
    for L in _get(cfg, f"appStoreVersions/{version_id}/appStoreVersionLocalizations")["data"]:
        if L["attributes"]["locale"] == locale:
            return L["id"]
    print(f"该版本没有 {locale} 本地化", file=sys.stderr)
    raise SystemExit(1)


def _screenshot_set(cfg, loc_id: str, display_type: str, create: bool) -> str | None:
    for s in _get(cfg, f"appStoreVersionLocalizations/{loc_id}/appScreenshotSets")["data"]:
        if s["attributes"]["screenshotDisplayType"] == display_type:
            return s["id"]
    if not create:
        return None
    code, out = _req(cfg, "POST", "appScreenshotSets", {
        "data": {"type": "appScreenshotSets",
                 "attributes": {"screenshotDisplayType": display_type},
                 "relationships": {"appStoreVersionLocalization": {
                     "data": {"type": "appStoreVersionLocalizations", "id": loc_id}}}}})
    if code not in (200, 201):
        _die(code, out, "创建 screenshotSet")
    return out["data"]["id"]


def cmd_list(cfg, args):
    vid = _version_id(cfg, args.version)
    for L in _get(cfg, f"appStoreVersions/{vid}/appStoreVersionLocalizations")["data"]:
        lc = L["attributes"]["locale"]
        sets = _get(cfg, f"appStoreVersionLocalizations/{L['id']}/appScreenshotSets")["data"]
        if not sets:
            print(f"  {lc:8s} （无截图集）")
            continue
        for s in sets:
            shots = _get(cfg, f"appScreenshotSets/{s['id']}/appScreenshots")["data"]
            print(f"  {lc:8s} {s['attributes']['screenshotDisplayType']:<24} {len(shots)} 张")
            for sh in shots:
                a = sh["attributes"]
                asset = a.get("imageAsset") or {}
                print("      %-40s %sx%s  %s" % (
                    a.get("fileName", "?"), asset.get("width", "?"),
                    asset.get("height", "?"), a.get("assetDeliveryState", {}).get("state", "")))


def _upload_one(cfg, set_id: str, path: Path) -> None:
    blob = path.read_bytes()
    code, out = _req(cfg, "POST", "appScreenshots", {
        "data": {"type": "appScreenshots",
                 "attributes": {"fileName": path.name, "fileSize": len(blob)},
                 "relationships": {"appScreenshotSet": {
                     "data": {"type": "appScreenshotSets", "id": set_id}}}}})
    if code not in (200, 201):
        _die(code, out, f"预留 {path.name}")
    sid = out["data"]["id"]

    # 分段由服务端决定，逐段照做。自己按整个文件 PUT 一次，在多段场景下会
    # 传成一个损坏的资源，而 API 不会报错——直到审核时才发现图是坏的。
    for op in out["data"]["attributes"]["uploadOperations"]:
        chunk = blob[op["offset"]:op["offset"] + op["length"]]
        headers = {h["name"]: h["value"] for h in (op.get("requestHeaders") or [])}
        c, o = _req(cfg, op["method"], op["url"], absolute=True, raw=chunk, headers=headers)
        if c not in (200, 201, 204):
            _die(c, o, f"PUT {path.name} @{op['offset']}")

    # 第三步不做等于白传：资源会停在 AWAITING_UPLOAD。
    code, out = _req(cfg, "PATCH", f"appScreenshots/{sid}", {
        "data": {"type": "appScreenshots", "id": sid,
                 "attributes": {"uploaded": True,
                                "sourceFileChecksum": hashlib.md5(blob).hexdigest()}}})
    if code != 200:
        _die(code, out, f"提交 {path.name}")


# App Store Connect 每个截图集最多 10 张。超了会在「预留」那步 409：
#
#     Too many screenshots. | Set: <id> has already 10 appScreenshots
#
SET_CAPACITY = 10


class PreflightError(Exception):
    """上传前的本地体检没过。此时**一张旧图都还没删**。"""


def preflight(paths: list[pathlib.Path], cap: int = SET_CAPACITY) -> None:
    """删任何东西之前，先确认这批文件真的传得上去。

    这个顺序是「先删后传」唯一的安全阀。删完再发现某张图是 0 字节、或者这一
    组有 11 张，那时集合已经空了，而 iPad 截图是提审必需项——版本会卡住，
    直到有人重跑。

    体检本身很便宜（读每个文件的前八个字节），放在最前面就把那个窗口关掉了。

    这里**故意不**保证「集合在任何时刻都不为空」。上一版为此写了边传边删，
    代价是十来行状态机。放弃它是有前提的：这批图由 Xcode Cloud 生成、本地有
    副本，删错了重跑一次就回来。哪天这个工具要去传一批不可复现的图（比如人
    手做的），这个取舍就不成立了——那时该回到边传边删，而不是把这段注释删掉。
    """
    if not paths:
        raise PreflightError("没有可上传的 PNG")
    if len(paths) > cap:
        raise PreflightError(
            f"一组最多 {cap} 张，给了 {len(paths)} 张——"
            "删光旧图之后这批也传不完，所以现在就停下")
    bad = []
    for p in paths:
        if not p.is_file():
            bad.append(f"{p.name}: 不存在")
        elif p.stat().st_size == 0:
            bad.append(f"{p.name}: 0 字节")
        elif p.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            bad.append(f"{p.name}: 不是 PNG")
    if bad:
        raise PreflightError("这些文件有问题，未删除任何旧截图：\n  "
                             + "\n  ".join(bad))


def cmd_upload(cfg, args):
    pngs = sorted(Path(args.dir).glob("*.png"))
    try:
        preflight(pngs)
    except PreflightError as e:
        print(f"上传前检查未通过：{e}", file=sys.stderr)
        raise SystemExit(1)

    vid = _version_id(cfg, args.version)
    loc = _localization_id(cfg, vid, args.lang)
    set_id = _screenshot_set(cfg, loc, args.display_type, create=True)
    existing = _get(cfg, f"appScreenshotSets/{set_id}/appScreenshots")["data"]

    print(f"版本 {args.version} · {args.lang} · {args.display_type}")
    print(f"  集合内现有 {len(existing)} 张"
          + ("，将全部删除" if args.replace and existing else ""))
    print(f"  待上传 {len(pngs)} 张（顺序即商店展示顺序）：")
    for p in pngs:
        print(f"    {p.name}  {p.stat().st_size // 1024} KB")

    if not args.yes:
        if input("\n继续？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return

    # 先删后传。体检已经在上面跑过了——到这一步文件都确认可用，
    # 「删完了传不上」的窗口基本关掉了。
    if args.replace and existing:
        for sh in existing:
            code, out = _req(cfg, "DELETE", f"appScreenshots/{sh['id']}")
            if code not in (200, 204):
                _die(code, out, "删除旧截图")
        print(f"  🗑  已删除 {len(existing)} 张旧截图")

    for i, p in enumerate(pngs, 1):
        _upload_one(cfg, set_id, p)
        print(f"  [{i}/{len(pngs)}] ✅ {p.name}")
    print("完成")


def main() -> int:
    ap = argparse.ArgumentParser(description="App Store Connect 截图上传")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出某版本各语言的截图")
    p.add_argument("--version", required=True)

    p = sub.add_parser("upload", help="上传一个目录的 PNG")
    p.add_argument("--version", required=True)
    p.add_argument("--lang", required=True)
    p.add_argument("--display-type", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--replace", action="store_true", help="先删掉集合内已有截图")
    p.add_argument("--yes", action="store_true", help="跳过确认")

    args = ap.parse_args()
    cfg = _cfg()
    {"list": cmd_list, "upload": cmd_upload}[args.cmd](cfg, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
