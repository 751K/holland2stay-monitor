"""
i18n 工具：语言检测 + 过滤选项的本地化标签表
================================================

依赖
----
- Flask request（仅 get_lang 需要请求上下文）
- 无外部业务模块依赖

translations.tr() 不在此处暴露，由 web.py 的 _inject_translations
context_processor 直接 import 使用，避免重复封装。
"""
from __future__ import annotations

from flask import request

# 默认可选值（DB 为空时回退）
DEFAULT_OCCUPANCY = ["One", "Two (only couples)", "Family (parents with children)"]
DEFAULT_TYPES     = ["Studio", "1", "2", "3", "Loft (open bedroom area)"]
DEFAULT_CONTRACT  = ["Indefinite", "6 months max"]
DEFAULT_TENANT    = ["student only", "student and employed", "employed only"]
DEFAULT_OFFER     = ["Short-stay", "Parking included"]

# 类别 → 回退默认值
DEFAULTS: dict[str, list[str]] = {
    "Occupancy": DEFAULT_OCCUPANCY,
    "Type":      DEFAULT_TYPES,
    "Contract":  DEFAULT_CONTRACT,
    "Offer":     DEFAULT_OFFER,
    "Tenant":    DEFAULT_TENANT,
}

# 过滤选项显示名称 (zh, en)
LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "Type": {
        "1":                              ("1 居室",      "1 bedroom"),
        "2":                              ("2 居室",      "2 bedrooms"),
        "3":                              ("3 居室",      "3 bedrooms"),
        "4":                              ("4 居室",      "4 bedrooms"),
        "Loft (open bedroom area)":       ("Loft",        "Loft"),
    },
    "Occupancy": {
        "One":                            ("单人",         "Single"),
        "Two":                            ("双人",         "Two persons"),
        "Two (only couples)":             ("双人/情侣",     "Two (couples)"),
        "Family (parents with children)": ("家庭",         "Family"),
    },
    "Contract": {
        "Indefinite":                     ("长期",         "Indefinite"),
        "6 months max":                   ("短租(≤6月)",    "6 months max"),
    },
    "Tenant": {
        "student only":                   ("仅学生",       "Student only"),
        "employed only":                  ("仅上班族",     "Employed only"),
        "student and employed":           ("学生/上班族",  "Student & employed"),
        "custom":                         ("自定义",       "Custom"),
    },
}


def get_lang() -> str:
    """从 cookie 或 query 参数读取语言；默认 zh。"""
    lang = request.args.get("lang", "") or request.cookies.get("h2s-lang", "zh")
    return lang if lang in ("zh", "en") else "zh"


def localize_options(category: str, options: list[str]) -> list[tuple[str, str]]:
    """
    给一组选项附加本地化显示标签，返回 [(value, label), ...]，保持原始顺序。

    无标签映射的值（自定义片区、城市等）回退为 (value, value)。
    """
    labels = LABELS.get(category, {})
    zh = get_lang() == "zh"
    result: list[tuple[str, str]] = []
    for v in options:
        if v in labels:
            result.append((v, labels[v][0 if zh else 1]))
        else:
            result.append((v, v))
    return result
