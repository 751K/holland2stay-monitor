"""仪表盘「支持平台」那格必须与 config.KNOWN_SOURCES 一致。

2026-09-01 反馈：线上启用了 holland2stay / ourcampus / xior / magis 四个平台，
那格显示的却是「2 · 共 3」。

成因是 dashboard_service.py 里另立了一份手写清单
``SUPPORTED_SOURCES = ("holland2stay", "ourdomain", "xior")``。接入 OurCampus 时
没跟上，接入 Magis 时又没跟上。**两个数都错**：分母是这份清单的长度，分子是「已
启用」与它的交集——被漏掉的平台连分子都进不去。

它不会报错，只会一直少报，而且少报的幅度随接入的平台数增长。
"""
from __future__ import annotations

import inspect

from config import KNOWN_SOURCES
from app.services import dashboard_service as ds


def test_uses_the_single_source_of_truth():
    assert tuple(ds.SUPPORTED_SOURCES) == tuple(KNOWN_SOURCES)


def test_not_a_hand_written_list():
    """判据是「它由 KNOWN_SOURCES 推出」，不是「它此刻碰巧相等」。

    手抄一份当前取值也能让上一条变绿，然后在下一次接入平台时重新分叉——那正是
    这次的成因。
    """
    src = inspect.getsource(ds)
    line = next(l for l in src.splitlines() if l.startswith("SUPPORTED_SOURCES"))
    assert "KNOWN_SOURCES" in line, f"又变回手写清单了: {line}"


def test_no_second_platform_list_in_the_module():
    """模块里不该再出现第二份平台名清单。"""
    import re

    # 注释里引用得着旧的那份手写清单（说明成因），看的是真正的代码
    src = "\n".join(
        l for l in inspect.getsource(ds).splitlines()
        if not re.match(r"\s*#", l)
    )
    for name in KNOWN_SOURCES:
        assert f'"{name}"' not in src, (
            f"{name} 被写进 dashboard_service 的字面量里，"
            "平台清单只该有 config.KNOWN_SOURCES 一份"
        )


class TestCounts:
    def _scope(self, monkeypatch, sources):
        class _Cfg:
            def __init__(self, srcs):
                self.sources = list(srcs)

            def scrape_tasks_v2(self):
                return []

        monkeypatch.setattr(ds, "load_config", lambda: _Cfg(sources))
        return ds._configured_scope()

    def test_every_known_source_counts(self, monkeypatch):
        """启用几个就报几个。漏登记的平台此前连分子都进不去。"""
        got = self._scope(monkeypatch, KNOWN_SOURCES)
        assert got["enabled_platforms"] == len(KNOWN_SOURCES)
        assert got["supported_platforms"] == len(KNOWN_SOURCES)

    def test_the_reported_production_case(self, monkeypatch):
        """线上那四个：显示「4 · 共 5」，而不是「2 · 共 3」。"""
        got = self._scope(
            monkeypatch, ["holland2stay", "ourcampus", "xior", "magis"])
        assert got["enabled_platforms"] == 4
        assert got["supported_platforms"] == len(KNOWN_SOURCES)

    def test_unknown_source_is_not_counted(self, monkeypatch):
        """SOURCES 里写了个不存在的平台不该把分子撑大。"""
        got = self._scope(monkeypatch, ["holland2stay", "not-a-platform"])
        assert got["enabled_platforms"] == 1

    def test_broken_config_degrades_to_zero(self, monkeypatch):
        """配置读不出来时报 0，而不是把整个仪表盘打崩。"""
        def _boom():
            raise RuntimeError("bad config")

        monkeypatch.setattr(ds, "load_config", _boom)
        got = ds._configured_scope()
        assert got["enabled_platforms"] == 0
        assert got["supported_platforms"] == len(KNOWN_SOURCES)
