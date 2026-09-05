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

路由比完了，字段没人比
----------------------
上面这套只管**路径**。2026-09-05 发现它挡不住的另一层：``DeviceRegisterRequest``
的 schema 里从来没有 ``language``，而 iOS 从很久以前就在发它（APNs 双语推送靠
它），后端也一直在读。整条路由在 spec 里、写法也对，只是字段少了一个——双向
diff 一路是绿的。同一次还查出 ``POST /diagnostics/crash`` 的 ``platform`` 也没
登记。

字段漂移的后果和路由漂移一样：客户端照着一份不完整的文档在写，发出去的东西
是不是会被读，只能靠翻后端源码。所以下面加了一条按 ``body.get("…")`` 反推的
字段级 diff，方向和路由那条一致——**代码读了而 spec 没写**就是红。

它只比这一个方向。反过来（spec 写了但代码不读）不比，因为那可能是有意的向前
兼容声明；而且 ``body.get`` 这个正则天生只会漏报不会误报——handler 把取值挪进
辅助函数，这条就看不见了。宁可少抓，不可错杀。
"""
from __future__ import annotations

import inspect
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

# ``app/routes/api_v1/*.py`` 里读请求体的写法是统一的：
#     body = request.get_json(silent=True) or {}
#     x = body.get("x")
# 九个带请求体的 handler 全是这个形状，所以一个正则就够反推出「代码实际读了
# 哪些字段」。哪天有人改用别的变量名，这条只会少抓，不会错杀。
_BODY_GET = re.compile(r'body\.get\(\s*["\']([A-Za-z0-9_]+)["\']')


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


def _resolve(spec: dict, ref: str):
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _endpoint_index(test_app) -> dict[tuple[str, str], str]:
    """``(spec 路径, 小写方法) → Flask endpoint 名``。"""
    index: dict[tuple[str, str], str] = {}
    for rule in test_app.url_map.iter_rules():
        if not rule.rule.startswith(API_PREFIX):
            continue
        path = _normalize(rule.rule)
        for method in (rule.methods or set()) - _IMPLICIT_METHODS:
            index[(path, method.lower())] = rule.endpoint
    return index


def _declared_body_fields(spec: dict, operation: dict) -> set[str] | None:
    """spec 为这个操作声明的请求体字段；没有请求体时返回 None。"""
    body = operation.get("requestBody")
    if not body:
        return None
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = _resolve(spec, schema["$ref"])
    return set(schema.get("properties", {}))


def _fields_the_handler_reads(test_app, endpoint: str) -> set[str]:
    view = test_app.view_functions[endpoint]
    # ``bearer_required(...)`` 包了一层；不 unwrap 的话读到的是装饰器的源码。
    source = inspect.getsource(inspect.unwrap(view))
    return set(_BODY_GET.findall(source))


def test_openapi_documents_every_request_field_the_code_reads(test_app) -> None:
    """代码从请求体里读、而 spec 没登记的字段，一个都不该有。

    这是路由级 diff 挡不住的那一层：整条路由在 spec 里，写法也对，只是少了个
    字段。``DeviceRegisterRequest.language`` 就是这么漏了很久的——iOS 一直在
    发，后端一直在读，spec 里根本没有这个属性。
    """
    spec = _load_openapi()
    index = _endpoint_index(test_app)

    checked = 0
    seen_fields: set[str] = set()
    drift: dict[str, list[str]] = {}
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            declared = _declared_body_fields(spec, operation)
            if declared is None:
                continue
            endpoint = index.get((path, method.lower()))
            if endpoint is None:
                continue          # 路由级那两条测试会报这个，不在这里重复
            checked += 1
            read = _fields_the_handler_reads(test_app, endpoint)
            seen_fields |= {f"{endpoint}.{name}" for name in read}
            extra = read - declared
            if extra:
                drift[f"{method.upper()} {path}"] = sorted(extra)

    assert not drift, (
        "这些字段后端会读，但 spec 里没登记：\n"
        + "\n".join(f"  {op}: {fields}" for op, fields in sorted(drift.items()))
        + "\n客户端照着 spec 写，发不发这些字段全靠翻源码。")

    # 上面那句 `not drift` 在「一个字段都没抽出来」时同样成立——恒真的绿。
    # 所以要分别为两件事背书：
    #
    #   checked      端点映射还对得上（spec 路径 ↔ url_map）
    #   seen_fields  正则真的从 handler 源码里抽出了字段
    #
    # 只断言 checked 是不够的：把正则改成匹配不到任何东西，checked 照样是 10。
    # 第一版就是这么写的，变异测试当场证明它是摆设。
    assert checked >= 8, (
        f"只比对了 {checked} 个带请求体的端点，太少了。多半是 spec 的 "
        "requestBody 写法变了、或者路由映射对不上。")
    assert len(seen_fields) >= 20, (
        f"只从 handler 源码里抽出了 {len(seen_fields)} 个字段（预期 20+）。"
        "多半是 handler 改了读请求体的写法（不再是 `body.get(\"…\")`），"
        "于是这条测试什么都没在比——绿的，但空的。"
        f"\n抽到的：{sorted(seen_fields)}")
