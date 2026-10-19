"""平台能力的单一注册表：SourceSpec。

以前同一份平台差异散在四个平行集合里（浏览器型、可无代理、可自动预订、
REST 唤醒），加平台时要记得逐个补。现在每个 source 只声明一次，集合由
注册表派生。

注意：``AUTO_BOOK_SOURCES`` 保持元组顺序，因为用户看到/日志里的顺序曾经
就是 ("holland2stay", "plaza")。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    name: str
    #: 需要 Playwright 浏览器传输（必须绑专属长存线程）。
    browser_transport: bool = False
    #: 代理全挂、降级直连时还能抓。
    proxyless_capable: bool = False
    #: 自动预订流程已端到端验证通过。
    auto_bookable: bool = False
    #: REST 触发器发现新房时，这个 source 值得单独叫醒一轮。
    rest_wake: bool = False


SPECS: dict[str, SourceSpec] = {
    # auto_bookable 的进组条件是「用真实账号端到端跑通过」：
    #   holland2stay  2026-05-22 真实下单成功（见 ARCHITECTURE §7）
    #   plaza         2026-09-10 真实应征成功（见 docs/PLAZA.md §6）
    #
    # Plaza 另有一道**用户侧开关** auto_book.plaza_enabled，默认关——进了这个
    # 元组不等于对谁都开。理由见 can_auto_book 里的注释。
    #
    # **Xior 先不放进来**：bookers/rentcafe.py 里第 3 步之后的多步表单是没走过
    # 流程硬猜出来的草稿，放开等于拿用户的真实账号去提交半懂不懂的表单。等流程
    # 侦察完并验证过再加。凭据判定（can_auto_book）已经就位，到时只改这里。
    "holland2stay": SourceSpec(
        "holland2stay",
        browser_transport=True,
        auto_bookable=True,
        rest_wake=True,
    ),
    "xior": SourceSpec("xior", browser_transport=True),
    "ourdomain": SourceSpec("ourdomain", proxyless_capable=True),
    "ourcampus": SourceSpec("ourcampus", proxyless_capable=True),
    "plaza": SourceSpec("plaza", auto_bookable=True),
    "magis": SourceSpec("magis"),
    "studentexperience": SourceSpec("studentexperience"),
}


def _names(field: str) -> frozenset[str]:
    return frozenset(name for name, spec in SPECS.items() if getattr(spec, field))


BROWSER_SOURCES = _names("browser_transport")
PROXYLESS_CAPABLE_SOURCES = _names("proxyless_capable")
REST_WAKE_SOURCES = _names("rest_wake")

#: 元组顺序保留历史顺序（H2S 在前）。
AUTO_BOOK_SOURCES: tuple[str, ...] = tuple(
    name for name, spec in SPECS.items() if spec.auto_bookable
)
