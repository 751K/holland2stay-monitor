"""target_config.py — 塞在字符串里的结构化配置，严格解析

问题
----
监控范围本质是表格数据，却被压成了带分隔符的字符串：

    CITIES="Eindhoven,29|Amsterdam,24"
    AVAILABILITY_FILTERS="Available to book,179|Reserved,6203"
    SHARD_SIZES="xior:4"

2026-08-06 实测，同一类输入错误的后果毫无一致性：

    CITIES=Eindhoven          漏 ID     → 静默丢弃，**0 个城市，monitor 照常跑**
    CITIES=Eindhoven;29       分隔符错   → 同上，静默 0 个城市
    CITIES=Eindhoven,abc      ID 非数字  → ValueError，monitor 起不来
    AVAILABILITY_FILTERS=…,999999       → 照单全收，抓一个不存在的状态
    SOURCES=holland2stay,xiorr          → 照单全收，一个不存在的平台

最糟的是第一种：解析不出来就得到空列表，而空列表是**合法配置**（「不监控任何
城市」），于是监控正常启动、正常跑轮次、一条房源都不抓，直到几小时后 watchdog
才可能发现。

本模块的立场
------------
**解析失败必须说话。** 每个函数返回 ``(结果, 问题清单)``，问题清单里是可以直接
给人看的句子。调用方自己决定是拒绝启动、还是带着问题继续跑——但没有「安静地
少了一项」这个选项。

校验分两层，分开报：

- **格式**：分隔符对不对、字段数够不够、该是数字的是不是数字
- **实体**：平台名 / 城市 ID / 状态 ID 是否真的存在

后者单独分开是因为它可以是**警告而非错误**：官方注册表会变，写死拒绝会让一个
新上线的城市变成启动失败。
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    KNOWN_CITIES,
    KNOWN_OURCAMPUS_CITIES,
    KNOWN_OURDOMAIN_CITIES,
    KNOWN_SOURCES,
    KNOWN_MAGIS_CITIES,
    KNOWN_PLAZA_CITIES,
    KNOWN_STUDENTEXPERIENCE_CITIES,
    KNOWN_XIOR_CITIES,
)

#: 已知的 Holland2Stay 可用状态 ID → 荷兰语标签。取自 AVAILABILITY_FILTERS 的
#: 六个取值（见 ARCHITECTURE §5.13）。用于校验，不用于产生默认值。
KNOWN_AVAILABILITY_IDS: dict[int, str] = {
    179: "Direct te boeken",
    336: "Beschikbaar in loterij",
    6203: "Reserved",
    6204: "To be in lottery",
    180: "Niet beschikbaar",
    6253: "Coming soon",
}


@dataclass(frozen=True)
class Problem:
    """一条解析问题。

    ``fatal`` 区分「格式坏了」和「实体不认识」：前者一定是错的，后者可能只是
    官方注册表更新了而本地表还没跟上。
    """

    key: str
    entry: str
    message: str
    fatal: bool = True

    def __str__(self) -> str:
        where = f"{self.key}" + (f" 里的 {self.entry!r}" if self.entry else "")
        return f"{where}：{self.message}"


def _entries(raw: str) -> list[str]:
    """按 ``|`` 切开并去掉空项。尾部多一个分隔符是手写常见笔误，不算错。"""
    return [e.strip() for e in (raw or "").split("|") if e.strip()]


def parse_sources(raw: str) -> tuple[list[str], list[Problem]]:
    """``holland2stay,ourdomain`` → 平台名列表。逗号或 ``|`` 都接受。"""
    problems: list[Problem] = []
    out: list[str] = []
    for name in (raw or "").replace("|", ",").split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in KNOWN_SOURCES:
            problems.append(Problem(
                "SOURCES", name,
                f"不是已知平台（可选：{', '.join(KNOWN_SOURCES)}）",
                fatal=False,
            ))
            continue
        if name not in out:
            out.append(name)
    return out, problems


def parse_cities(raw: str) -> tuple[list[tuple[str, int]], list[Problem]]:
    """``名称,ID|名称,ID`` → ``[(名称, ID)]``，用于 Holland2Stay。

    ID 必须是整数：H2S 的 GraphQL 按数字 city id 过滤，写错了不会报错，只会
    返回空结果——那正是「抓到 0 条」和「真的没房」分不开的来源。
    """
    known_ids = {int(c["id"]) for c in KNOWN_CITIES}
    known_by_id = {int(c["id"]): c["name"] for c in KNOWN_CITIES}
    problems: list[Problem] = []
    out: list[tuple[str, int]] = []

    for entry in _entries(raw):
        parts = entry.rsplit(",", 1)
        if len(parts) != 2:
            problems.append(Problem(
                "CITIES", entry, "格式应为「城市名,ID」，缺少逗号或 ID",
            ))
            continue
        name, raw_id = parts[0].strip(), parts[1].strip()
        if not name:
            problems.append(Problem("CITIES", entry, "城市名为空"))
            continue
        try:
            city_id = int(raw_id)
        except ValueError:
            problems.append(Problem("CITIES", entry, f"ID {raw_id!r} 不是整数"))
            continue
        if city_id not in known_ids:
            problems.append(Problem(
                "CITIES", entry,
                f"ID {city_id} 不在已知城市表里（config.KNOWN_CITIES）",
                fatal=False,
            ))
        elif known_by_id[city_id] != name:
            problems.append(Problem(
                "CITIES", entry,
                f"ID {city_id} 对应的是 {known_by_id[city_id]!r}，写的却是 {name!r}",
                fatal=False,
            ))
        out.append((name, city_id))
    return out, problems


#: 各平台的 target 注册表：key → 显示名。
_KNOWN_TARGETS: dict[str, dict[str, str]] = {
    "ourdomain": {c["key"]: c["name"] for c in KNOWN_OURDOMAIN_CITIES},
    "ourcampus": {c["key"]: c["name"] for c in KNOWN_OURCAMPUS_CITIES},
    "xior": {c["key"]: f"{c['city']} {c['bldg']}" for c in KNOWN_XIOR_CITIES},
    "magis": {c["key"]: c["name"] for c in KNOWN_MAGIS_CITIES},
    "studentexperience": {c["key"]: c["name"]
                          for c in KNOWN_STUDENTEXPERIENCE_CITIES},
    "plaza": {c["key"]: c["name"] for c in KNOWN_PLAZA_CITIES},
}

#: 环境变量名 → 它描述的是哪个平台。
TARGET_KEYS: dict[str, str] = {
    "OURDOMAIN_CITIES": "ourdomain",
    "OURCAMPUS_CITIES": "ourcampus",
    "XIOR_CITIES": "xior",
    "MAGIS_CITIES": "magis",
    "STUDENTEXPERIENCE_CITIES": "studentexperience",
    "PLAZA_CITIES": "plaza",
}


def parse_targets(env_key: str, raw: str) -> tuple[list[tuple[str, str]], list[Problem]]:
    """``显示名,key|显示名,key`` → ``[(显示名, key)]``，用于非 H2S 的三个平台。

    与 ``parse_cities`` 的区别只有 key 不是整数；单独一个函数是为了让错误信息
    说得出「这是哪个平台的哪个楼盘」。
    """
    source = TARGET_KEYS.get(env_key, "")
    known = _KNOWN_TARGETS.get(source, {})
    problems: list[Problem] = []
    out: list[tuple[str, str]] = []

    for entry in _entries(raw):
        parts = entry.rsplit(",", 1)
        if len(parts) != 2:
            problems.append(Problem(
                env_key, entry, "格式应为「显示名,key」，缺少逗号或 key",
            ))
            continue
        name, key = parts[0].strip(), parts[1].strip()
        if not name or not key:
            problems.append(Problem(env_key, entry, "显示名或 key 为空"))
            continue
        if known and key not in known:
            problems.append(Problem(
                env_key, entry, f"key {key!r} 不在 {source} 的已知楼盘表里",
                fatal=False,
            ))
        out.append((name, key))
    return out, problems


def parse_availability(raw: str) -> tuple[list[tuple[str, int]], list[Problem]]:
    """``标签,ID|标签,ID`` → ``[(标签, ID)]``。

    ID 会被原样发给 H2S 的 GraphQL。写一个不存在的值不会报错，只会让该过滤器
    什么都匹配不到——又是一个「抓到 0 条」说不清来源的坑。
    """
    problems: list[Problem] = []
    out: list[tuple[str, int]] = []

    for entry in _entries(raw):
        parts = entry.rsplit(",", 1)
        if len(parts) != 2:
            problems.append(Problem(
                "AVAILABILITY_FILTERS", entry, "格式应为「标签,ID」，缺少逗号或 ID",
            ))
            continue
        label, raw_id = parts[0].strip(), parts[1].strip()
        try:
            fid = int(raw_id)
        except ValueError:
            problems.append(Problem(
                "AVAILABILITY_FILTERS", entry, f"ID {raw_id!r} 不是整数",
            ))
            continue
        if fid not in KNOWN_AVAILABILITY_IDS:
            problems.append(Problem(
                "AVAILABILITY_FILTERS", entry,
                f"ID {fid} 不是 H2S 的已知状态"
                f"（可选：{', '.join(str(i) for i in sorted(KNOWN_AVAILABILITY_IDS))}）",
                fatal=False,
            ))
        out.append((label or KNOWN_AVAILABILITY_IDS.get(fid, str(fid)), fid))
    return out, problems


def parse_source_map(env_key: str, raw: str) -> tuple[dict[str, int], list[Problem]]:
    """``source:数字,source:数字`` → dict。用于 SHARD_SIZES 与 SOURCE_MIN_INTERVALS。

    这两个键的分隔符和上面那批**不一样**（冒号 + 逗号，而非逗号 + 竖线）。
    统一格式会破坏现有配置，所以只在这里把差异集中掉，别让调用方各写一份解析。
    """
    problems: list[Problem] = []
    out: dict[str, int] = {}

    for entry in (raw or "").replace("|", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            problems.append(Problem(env_key, entry, "格式应为「平台:数字」，缺少冒号"))
            continue
        source, raw_val = entry.split(":", 1)
        source = source.strip().lower()
        if source not in KNOWN_SOURCES:
            problems.append(Problem(
                env_key, entry, f"{source!r} 不是已知平台", fatal=False,
            ))
        try:
            value = int(raw_val.strip())
        except ValueError:
            problems.append(Problem(env_key, entry, f"{raw_val.strip()!r} 不是整数"))
            continue
        if value < 0:
            problems.append(Problem(env_key, entry, f"不能是负数（{value}）"))
            continue
        out[source] = value
    return out, problems


# ── 反向：结构 → 字符串 ─────────────────────────────────────────────
#
# 面板和迁移都要往回写。放在这里而不是各自拼接，是为了让「怎么解析」和
# 「怎么生成」永远对得上——tests/test_target_config.py 有一条往返测试钉住它。


def format_pairs(pairs) -> str:
    """``[(名称, 值)]`` → ``名称,值|名称,值``。"""
    return "|".join(f"{name},{value}" for name, value in pairs)


def format_source_map(mapping: dict[str, int]) -> str:
    return ",".join(f"{k}:{v}" for k, v in mapping.items())


# ── 汇总校验 ────────────────────────────────────────────────────────

#: 环境变量名 → 解析函数。startup 自检和面板保存都走这张表，避免两边各漏一个。
_PARSERS = {
    "SOURCES": lambda v: parse_sources(v),
    "SHADOW_SOURCES": lambda v: parse_sources(v),
    "CITIES": lambda v: parse_cities(v),
    "OURDOMAIN_CITIES": lambda v: parse_targets("OURDOMAIN_CITIES", v),
    "OURCAMPUS_CITIES": lambda v: parse_targets("OURCAMPUS_CITIES", v),
    "XIOR_CITIES": lambda v: parse_targets("XIOR_CITIES", v),
    "AVAILABILITY_FILTERS": lambda v: parse_availability(v),
    "SHARD_SIZES": lambda v: parse_source_map("SHARD_SIZES", v),
    "SOURCE_MIN_INTERVALS": lambda v: parse_source_map("SOURCE_MIN_INTERVALS", v),
}

STRUCTURED_KEYS = frozenset(_PARSERS)


def validate(values: dict[str, str]) -> list[Problem]:
    """校验一批结构化配置，返回全部问题（无问题则空列表）。

    只校验传进来的键，不去读 ``os.environ``——面板保存时要在**写入之前**校验，
    那时新值还没生效。
    """
    problems: list[Problem] = []
    for key, raw in values.items():
        parser = _PARSERS.get(key)
        if parser is None:
            continue
        _, found = parser(raw)
        problems.extend(found)
    return problems


def validate_effective(env: dict[str, str] | None = None) -> list[Problem]:
    """校验当前**生效中**的配置。启动自检用。

    额外补一条只有在这里才判得出的：``CITIES`` 解析后为空。空列表是合法配置
    （「不监控任何城市」），但如果原始字符串非空却解析出 0 项，那就是解析全军
    覆没——监控会正常启动、正常跑轮次、一条都不抓。这种情况必须吼出来。
    """
    import os

    src = os.environ if env is None else env
    values = {k: src.get(k, "") for k in STRUCTURED_KEYS if src.get(k, "")}
    problems = validate(values)

    raw_cities = src.get("CITIES", "")
    if raw_cities.strip():
        parsed, _ = parse_cities(raw_cities)
        if not parsed:
            problems.append(Problem(
                "CITIES", raw_cities,
                "整条都没解析出城市——Holland2Stay 本轮什么都不会抓，"
                "而空列表本身是合法配置，不会有别的地方报错",
            ))
    return problems
