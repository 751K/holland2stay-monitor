"""``docs/openapi.json`` 与后端实际路由的双向契约。

为什么要从 app 反推路由
----------------------
这份 spec 是移动端唯一的机器可读契约。iOS 和 Android 拆到各自仓库之后，后端改
了什么、客户端能不能跟上，全靠它——它一旦落后于代码，客户端就是照着一份过时的
文档在写。

这个测试原本是一份**手写的路径白名单**加 ``issubset``：

    assert set(EXPECTED_PATH_METHODS).issubset(paths)

方向决定了它只能发现「spec 少写了某个老路由」，**永远发现不了「后端新增了路由
而 spec 没跟上」**——因为新路由既不在白名单里，也不在 spec 里，两边一样地缺，
断言照样通过。

它确实没发现：``POST /auth/verify`` 在 2026-09-03 加进后端（iOS 开启 Face ID
要用），spec 里一直没有，测试一路是绿的。同样地 ``/filter/options`` 新增的
``dim_sources`` 字段也没人记录，而 iOS 的 ``PlatformScope`` 正是靠它工作。

所以改成从 ``app.url_map`` 枚举——真实的路由表就是唯一事实来源，两个方向都比：
后端多一条会红，spec 多一条也会红。手写清单不再有维护余地，也就不会再有
「白名单和 spec 同时漏，于是绿着」这种事。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "openapi.json"

API_PREFIX = "/api/v1"

# Flask 的 ``<converter:name>`` → OpenAPI 的 ``{name}``
_CONVERTER = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")

# ``url_map`` 会给每条规则自动附上这两个方法，spec 不描述它们。
_IMPLICIT_METHODS = {"HEAD", "OPTIONS"}

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def _load_openapi() -> dict:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _normalize(rule: str) -> str:
    """``/api/v1/listings/<string:listing_id>`` → ``/listings/{listing_id}``。"""
    return _CONVERTER.sub(r"{\1}", rule[len(API_PREFIX):] or "/")


def _live_routes(test_app) -> dict[str, set[str]]:
    """后端此刻真正挂载的 ``/api/v1/*`` 路由：``{path: {method,...}}``（小写）。"""
    routes: dict[str, set[str]] = {}
    for rule in test_app.url_map.iter_rules():
        if not rule.rule.startswith(API_PREFIX):
            continue
        methods = {m.lower() for m in rule.methods if m not in _IMPLICIT_METHODS}
        routes.setdefault(_normalize(rule.rule), set()).update(methods)
    return routes


def _spec_routes(spec: dict) -> dict[str, set[str]]:
    """spec 描述的路由。过滤掉 ``parameters``/``summary`` 这类非方法键。"""
    return {
        path: {k.lower() for k in ops if k.lower() in _HTTP_METHODS}
        for path, ops in spec["paths"].items()
    }


def _fmt(routes: dict[str, set[str]]) -> str:
    return "\n".join(f"  {p} [{', '.join(sorted(m))}]" for p, m in sorted(routes.items()))


def _diff(left: dict[str, set[str]], right: dict[str, set[str]]) -> dict[str, set[str]]:
    """left 有而 right 没有的 (path, method)。"""
    out = {}
    for path, methods in left.items():
        missing = methods - right.get(path, set())
        if missing:
            out[path] = missing
    return out


def test_openapi_json_is_parseable_and_declares_version() -> None:
    spec = _load_openapi()

    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "FlatRadar Backend API"
    assert spec["info"]["version"]
    assert spec["servers"][0]["url"].endswith("/api/v1")


def test_openapi_documents_every_live_route(test_app) -> None:
    """后端挂了但 spec 没写 —— 客户端会不知道这个端点存在。"""
    undocumented = _diff(_live_routes(test_app), _spec_routes(_load_openapi()))

    assert not undocumented, (
        "以下路由已挂在后端，但 docs/openapi.json 没有描述。"
        "移动端拿这份 spec 当契约，漏一条它们就看不到：\n"
        + _fmt(undocumented)
    )


def test_openapi_describes_no_route_that_does_not_exist(test_app) -> None:
    """spec 写了但后端没有 —— 客户端会照着调一个 404。"""
    phantom = _diff(_spec_routes(_load_openapi()), _live_routes(test_app))

    assert not phantom, (
        "docs/openapi.json 描述了后端并不存在的路由，"
        "客户端照着实现会拿到 404：\n" + _fmt(phantom)
    )


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


def test_every_ref_in_the_spec_resolves() -> None:
    """
    ``$ref`` 打错字不会让 JSON 解析失败，只会让那一段描述静默地指向空气。
    生成客户端代码的工具会在这里炸，而我们应该先炸。
    """
    spec = _load_openapi()
    broken: list[str] = []

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if not ref.startswith("#/"):
                    broken.append(f"{where}: 非本地引用 {ref}")
                else:
                    target = spec
                    for part in ref[2:].split("/"):
                        part = part.replace("~1", "/").replace("~0", "~")
                        if not isinstance(target, dict) or part not in target:
                            broken.append(f"{where}: {ref} 指向不存在的节点")
                            break
                        target = target[part]
            for key, value in node.items():
                walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{where}[{i}]")

    walk(spec, "#")
    assert not broken, "docs/openapi.json 里有悬空的 $ref：\n  " + "\n  ".join(broken)
