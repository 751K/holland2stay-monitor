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

# ── 查询参数 ────────────────────────────────────────────────────────
#
# 路由比过了、请求体字段比过了，2026-09-09 的全量核对发现第三层没人比：**查询
# 参数**，而且不是「有没有」，是「写的值对不对」。
#
# 当时查出两处，都是值不对而不是缺失：
#
#   * ``DaysQuery`` 的 default 写 30，代码 ``DEFAULT_STATS_DAYS`` 是 7
#   * ``LimitQuery`` 被 ``/listings``（100/500）和 ``/notifications``（50/200）
#     共用——一个组件同时对两个边界不同的端点说话，对其中一个必然是错的。
#     客户端照 spec 要 500 条通知，拿到的是被静默截断的 200 条。
#
# 也因此这个测试**必须解 $ref**。审计脚本第一版只看 ``prm["in"] == "query"``，
# 而 ``$ref`` 形式的参数没有 ``in`` 键，于是三条早就写好的参数被当成「spec 里没
# 有」报了出来——差点照着这份假清单往 spec 里补重复定义。

_ARG_GET = re.compile(
    r'request\.args\.get\(\s*["\']([A-Za-z0-9_]+)["\']\s*,?\s*([^)]*)\)')


def _resolve_param(spec: dict, prm: dict) -> dict:
    """把 ``$ref`` 形式的参数解开。不解的话它连 ``in`` 都没有。"""
    seen = set()
    while isinstance(prm, dict) and "$ref" in prm:
        ref = prm["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        cur: object = spec
        for part in ref.lstrip("#/").split("/"):
            cur = (cur or {}).get(part, {})   # type: ignore[union-attr]
        prm = cur          # type: ignore[assignment]
    return prm if isinstance(prm, dict) else {}


def _query_params(spec: dict, path: str) -> dict[str, dict]:
    """这条路径上所有查询参数：名字 → schema。"""
    out: dict[str, dict] = {}
    for _meth, op in spec["paths"].get(path, {}).items():
        if not isinstance(op, dict):
            continue
        for prm in op.get("parameters") or []:
            prm = _resolve_param(spec, prm)
            if prm.get("in") == "query" and prm.get("name"):
                out[prm["name"]] = prm.get("schema") or {}
    return out


def _default_value(expr: str, view) -> int | None:
    """把代码里那个默认值表达式解成整数；解不出返回 None。

    不能只认字面量数字：``request.args.get("days", DEFAULT_STATS_DAYS)`` 写的是
    常量名，而**今天查出的那处漂移恰恰是它**（spec 写 30、常量是 7）。只比字面量
    的话，这个测试对那一处永远是绿的——那就等于没测到唯一一个真出问题的地方。
    """
    expr = expr.strip()
    if expr.lstrip("-").isdigit():
        return int(expr)
    if not expr.isidentifier():
        return None
    mod = inspect.getmodule(inspect.unwrap(view))
    val = getattr(mod, expr, None)
    return val if isinstance(val, int) and not isinstance(val, bool) else None


def _reads_query_args(test_app):
    """路由 → {参数名: (默认值表达式, view 函数)}。"""
    for rule in test_app.url_map.iter_rules():
        raw = str(rule.rule)
        if not raw.startswith(API_PREFIX + "/"):
            continue
        view = test_app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        try:
            src = inspect.getsource(inspect.unwrap(view))
        except (OSError, TypeError):
            continue
        found = {m.group(1): (m.group(2).strip(), view)
                 for m in _ARG_GET.finditer(src)}
        if found:
            yield _normalize(raw), found


def test_openapi_documents_every_query_parameter_the_code_reads(test_app) -> None:
    """代码 ``request.args.get`` 读的查询参数，spec 里都要有。"""
    spec = _load_openapi()
    drift: list[str] = []
    checked = 0
    seen: set[str] = set()

    for path, found in _reads_query_args(test_app):
        checked += 1
        seen |= set(found)
        missing = sorted(set(found) - set(_query_params(spec, path)))
        if missing:
            drift.append(f"{path} 读了 {missing}，spec 里没有")

    assert not drift, (
        "这些查询参数代码在读、spec 没写——客户端照契约写就用不上：\n  "
        + "\n  ".join(drift))
    # 挡住「正则或归一化坏掉导致一条都没扫到」，那会让上面的断言恒真。
    assert checked >= 5, f"只扫到 {checked} 条读查询参数的路由，正则多半坏了"
    assert len(seen) >= 12, f"只认出 {len(seen)} 个参数名，正则多半坏了"


def test_openapi_query_parameter_defaults_match_the_code(test_app) -> None:
    """spec 里写的默认值，要和代码里那个字面量一致。

    「参数在」不等于「参数对」。``DaysQuery`` 写着 default 30 而代码用 7，整整
    一年没人发现——路由 diff、字段 diff、存在性检查全都是绿的。
    """
    spec = _load_openapi()
    drift: list[str] = []
    compared = 0
    via_constant = 0

    for path, found in _reads_query_args(test_app):
        documented = _query_params(spec, path)
        for name, (default_expr, view) in sorted(found.items()):
            if name not in documented or not default_expr:
                continue
            code_default = _default_value(default_expr, view)
            if code_default is None:
                continue          # 默认值不是整数常量，比不了
            compared += 1
            if not default_expr.lstrip("-").isdigit():
                via_constant += 1
            spec_default = documented[name].get("default")
            if spec_default != code_default:
                drift.append(
                    f"{path} 的 {name}：代码默认 {code_default}"
                    f"（{default_expr}），spec 写 {spec_default!r}")

    assert not drift, "spec 的默认值和代码对不上：\n  " + "\n  ".join(drift)
    assert compared >= 5, f"只比到 {compared} 个默认值，正则多半坏了"
    # 至少有一个默认值是**常量名**而不是字面量（``DEFAULT_STATS_DAYS``）。
    # 去掉常量解析之后 compared 只少 1，光靠上面那个下限挡不住——而唯一真出过
    # 问题的那处恰恰是常量写的。
    assert via_constant >= 1, (
        "没有比到任何「默认值写成常量名」的参数——常量解析多半失效了，"
        "而 DaysQuery 那类漂移只有它能发现")


def test_every_success_response_wraps_the_standard_envelope() -> None:
    """所有返回 ``data`` 的 200 响应都要引 ``SuccessEnvelope``。

    2026-09-09 查出 ``MapLocateOk`` 写着 ``"allOf": []``——一个空数组，于是它既没
    继承 ``ok`` / ``error``，也没人报错：``$ref`` 全都能解、JSON 也合法，前面那些
    检查一条都不会红。而后端走的是同一个 ``_err.ok()``，客户端照这份 schema 生成
    模型就会少掉整个信封。

    空 allOf 单独判一次：它比「忘了引」更隐蔽，看起来像是写了。
    """
    spec = _load_openapi()
    empty, unwrapped = [], []
    for name, resp in (spec.get("components", {}).get("responses") or {}).items():
        schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
        if not isinstance(schema, dict):
            continue
        if "allOf" in schema and not schema["allOf"]:
            empty.append(name)
        blob = json.dumps(schema)
        if '"data"' in blob and "SuccessEnvelope" not in blob:
            unwrapped.append(name)
    assert not empty, f"这些响应写了空的 allOf（看着像写了，其实什么都没继承）：{empty}"
    assert not unwrapped, f"这些响应带 data 却没挂 SuccessEnvelope：{unwrapped}"


# ── 人读的那份文档 ──────────────────────────────────────────────────
#
# ``docs/openapi.json`` 是机器契约，``docs/API.md`` 是人读的那份，而**只有前者
# 有测试**。2026-09-09 的全量核对发现 API.md 少了三个端点：``GET /legal``、
# ``GET /map/locate``、``POST /auth/verify``——其中 ``/auth/verify`` 是 2026-09-03
# 加的，spec 后来补上了，API.md 一直没有。
#
# 少写一个端点不会让任何东西变红，只会让看文档的人以为它不存在。

_MD_ENDPOINT = re.compile(
    r"^#{2,4}\s+(GET|POST|PUT|PATCH|DELETE)\s+`([^`]+)`", re.MULTILINE)

#: API.md 里刻意收录、但不属于 ``/api/v1`` 移动端契约的端点。
#: webhook 走 Svix 签名而不是 Bearer，spec 的 server 是 ``…/api/v1``，装不下它。
_MD_OUTSIDE_V1 = {("POST", "/api/inbound/email")}


def _api_md_endpoints() -> set[tuple[str, str]]:
    text = (OPENAPI_PATH.parent / "API.md").read_text(encoding="utf-8")
    return {(m.group(1), _CONVERTER.sub(r"{\1}", m.group(2)))
            for m in _MD_ENDPOINT.finditer(text)}


def test_api_md_documents_every_endpoint_in_the_spec() -> None:
    spec = _load_openapi()
    in_spec = {(meth.upper(), path)
               for path, item in spec["paths"].items()
               for meth in item
               if meth.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}}
    in_md = _api_md_endpoints()

    assert len(in_md) >= 30, (
        f"只从 API.md 里认出 {len(in_md)} 个端点，标题正则多半坏了——"
        "那会让下面两条断言恒真")

    missing = sorted(in_spec - in_md)
    assert not missing, (
        "spec 里有、API.md 没写（看文档的人会以为它不存在）：\n  "
        + "\n  ".join(f"{m} {p}" for m, p in missing))

    extra = sorted(in_md - in_spec - _MD_OUTSIDE_V1)
    assert not extra, (
        "API.md 写了 spec 里没有的端点（写错了、已删除、或者 spec 漏了）：\n  "
        + "\n  ".join(f"{m} {p}" for m, p in extra))
