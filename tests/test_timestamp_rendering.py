"""用户能看见的时间戳一律走时区转换。

库里所有时间戳都存 UTC，而容器跑在 TZ=Europe/Amsterdam。把 UTC 原文渲染出来，
夏令时期间和 /logs 差两小时——而这两处本来就是对着看的。

项目为此专门写了 app/jinja_filters.py:local_time，docstring 里把理由写得很清楚。
但 app_accounts.html 绕开它，用 ``| replace('T',' ') | replace('Z','')`` 手工拼
字符串：格式看起来对，时区是错的，而且错得不显眼——只差两小时，不像坏值那样
一眼能看出来。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"

#: 存 UTC、且会渲染给用户看的字段
_UTC_FIELDS = (
    "created_at", "last_used_at", "expires_at", "last_seen",
    "first_seen", "changed_at", "round_at", "last_scrape_at",
)

#: 认可的渲染方式：转成本地时区，或转成相对时间
_SAFE_FILTERS = ("local_time", "time_ago")


def _jinja_exprs(text: str) -> list[str]:
    return re.findall(r"\{\{(.*?)\}\}", text, flags=re.S)


class TestNoRawUtcInTemplates:
    def test_no_manual_timestamp_formatting(self):
        """手工 replace 拼时间戳的写法一处都不许有。

        它绕开 local_time，产出一个格式正确、时区错误的字符串。
        """
        bad = []
        for f in sorted(_TEMPLATES.glob("*.html")):
            for expr in _jinja_exprs(f.read_text(encoding="utf-8")):
                if "replace('T'" in expr or 'replace("T"' in expr:
                    bad.append(f"{f.name}: {' '.join(expr.split())[:80]}")
        assert not bad, "手工格式化时间戳（应改用 | local_time）：\n" + "\n".join(bad)

    def test_utc_fields_go_through_a_timezone_aware_filter(self):
        bad = []
        for f in sorted(_TEMPLATES.glob("*.html")):
            for expr in _jinja_exprs(f.read_text(encoding="utf-8")):
                flat = " ".join(expr.split())
                if not any(re.search(rf"\b{fld}\b", flat) for fld in _UTC_FIELDS):
                    continue
                if any(filt in flat for filt in _SAFE_FILTERS):
                    continue
                bad.append(f"{f.name}: {flat[:80]}")
        assert not bad, (
            "以下表达式把 UTC 时间戳直接渲染给用户，未经 local_time / time_ago：\n"
            + "\n".join(bad)
        )


class TestLocalTimeFilter:
    def test_converts_utc_to_configured_zone(self, monkeypatch):
        from app.jinja_filters import local_time
        import config

        monkeypatch.setattr(config, "TIMEZONE", "Europe/Amsterdam", raising=False)
        # 2026-08-20 是夏令时，CEST = UTC+2
        assert local_time("2026-08-20T15:01:00Z") == "2026-08-20 17:01"
        assert local_time("2026-08-20T15:01:00+00:00") == "2026-08-20 17:01"

    def test_naive_timestamps_are_treated_as_utc(self, monkeypatch):
        """库里的时间戳大多不带时区后缀，不能当成本地时间读。"""
        from app.jinja_filters import local_time
        import config

        monkeypatch.setattr(config, "TIMEZONE", "Europe/Amsterdam", raising=False)
        assert local_time("2026-08-20T15:01:00") == "2026-08-20 17:01"

    def test_garbage_passes_through(self):
        """展示层不该因为一个脏时间戳把整页打崩。"""
        from app.jinja_filters import local_time
        assert local_time("not-a-time") == "not-a-time"
        assert local_time("") == "—"
        assert local_time("—") == "—"
