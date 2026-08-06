"""仪表盘的覆盖范围横幅：城市名必须来自当前配置。

写死城市名是这里最容易犯的错——横幅会一直显示部署那天的样子，而监控范围现在
存在 app_settings 里、随时能从「设置」页改，改完不会有任何地方报错。一条自信而
过时的说明，比没有说明更糟：用户据此以为自己的城市没被监控，或反之。

因此本文件守的是**推导**，不是文案。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(monkeypatch):
    """给一组干净的 source/城市环境变量，避免读到开发机的真实配置。"""
    for k in ("SOURCES", "CITIES", "OURDOMAIN_CITIES", "OURCAMPUS_CITIES", "XIOR_CITIES"):
        monkeypatch.delenv(k, raising=False)

    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
        return config.load_config()
    return _set


def _xior(city: str) -> dict:
    return next(r for r in config.KNOWN_XIOR_CITIES if r["city"] == city)


class TestDerivedFromLiveConfig:
    def test_h2s_cities(self, env):
        cfg = env(SOURCES="holland2stay", CITIES="Amsterdam,24|Eindhoven,29")
        assert cfg.monitored_city_names() == ["Amsterdam", "Eindhoven"]

    def test_deduplicates_across_sources(self, env):
        """OurDomain 的「Amsterdam Diemen」和 H2S 的「Amsterdam」是同一座城市。

        分开列会让人以为监控了七八个城市。
        """
        cfg = env(
            SOURCES="holland2stay,ourdomain",
            CITIES="Amsterdam,24",
            OURDOMAIN_CITIES="Amsterdam Diemen,diemen",
        )
        assert cfg.monitored_city_names() == ["Amsterdam"]

    def test_building_names_resolve_to_their_city(self, env):
        m, v = _xior("Maastricht"), _xior("Venlo")
        cfg = env(
            SOURCES="holland2stay,xior",
            CITIES="Eindhoven,29",
            XIOR_CITIES=f"{m['city']} {m['bldg']},{m['key']}|{v['city']} {v['bldg']},{v['key']}",
        )
        assert cfg.monitored_city_names() == ["Eindhoven", "Maastricht", "Venlo"]

    def test_key_wins_over_the_display_name(self, env):
        """归属按 key 查注册表，不看显示名。

        对现有 30 栋楼，``canonical_city`` 的结果与注册表**完全一致**（实测 0 分歧），
        所以正常数据分不出两种实现。差别只在显示名不可信时才显现——手工改过、
        或将来命名格式变了。那时查表仍然对，猜名字会把楼盘名原样显示在横幅上。
        """
        a = _xior("Amsterdam")
        cfg = env(SOURCES="xior", CITIES="",
                  XIOR_CITIES=f"Some Renamed Building,{a['key']}")
        assert cfg.monitored_city_names() == ["Amsterdam"]

    def test_unknown_key_falls_back_to_normalisation(self, env):
        """注册表里没有的 key（新楼盘尚未同步）退回按名字归一化，而不是丢掉。"""
        cfg = env(SOURCES="xior", CITIES="",
                  XIOR_CITIES="Amsterdam Karspeldreef,p-not-in-registry")
        assert cfg.monitored_city_names() == ["Amsterdam"]

    def test_two_word_city_survives(self, env):
        """「Aachen Vaals Katzensprung」按前缀猜会切成「Aachen」，查表则是对的。"""
        a = _xior("Aachen Vaals")
        cfg = env(
            SOURCES="xior",
            CITIES="",
            XIOR_CITIES=f"{a['city']} {a['bldg']},{a['key']}",
        )
        assert cfg.monitored_city_names() == ["Aachen Vaals"]

    def test_disabled_source_is_not_counted(self, env):
        """城市列表填着但平台没开，一条都不会抓——列出来就是虚报。

        直接构造 Config 而不走 load_config()：后者本来就按 sources 过滤，
        经由它构造的实例里那些列表已经是空的，测不到本方法自己的判断。
        """
        cfg = config.Config(
            check_interval=300, availability_filters=[],
            db_path="x.db", log_level="INFO",
            sources=["holland2stay"],
            cities=[config.CityFilter(name="Eindhoven", id=29)],
            ourdomain_cities=[config.OurDomainCityFilter(name="Amsterdam Diemen", key="diemen")],
            xior_cities=[config.XiorCityFilter(name="Amsterdam Karspeldreef", key="p0196062")],
        )
        assert cfg.monitored_city_names() == ["Eindhoven"]

    def test_sorted_and_unique(self, env):
        cfg = env(SOURCES="holland2stay", CITIES="Eindhoven,29|Amsterdam,24|Eindhoven,29")
        assert cfg.monitored_city_names() == ["Amsterdam", "Eindhoven"]

    def test_empty_when_nothing_configured(self, env):
        """没配城市时返回空——模板据此整块不渲染，而不是显示一句空荡荡的说明。"""
        assert env(SOURCES="holland2stay", CITIES="").monitored_city_names() == []


class TestBannerWiring:
    def test_route_passes_derived_cities(self):
        src = (ROOT / "app" / "routes" / "dashboard.py").read_text(encoding="utf-8")
        assert "monitored_city_names()" in src, "城市名没有从配置推导，多半是写死了"
        assert "monitored_cities_text=" in src

    def test_route_survives_a_config_error(self):
        """配置读不出来时不显示横幅，而不是让整个首页 500。"""
        import inspect

        from app.routes import dashboard

        src = inspect.getsource(dashboard.index)
        i = src.index("monitored_city_names()")
        assert "except Exception" in src[i - 400:i + 400]

    def test_template_hides_the_block_when_empty(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "{% if monitored_cities_text %}" in html

    def test_support_link_uses_url_for(self):
        """写死 /support 会在挂到子路径时断掉。"""
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "url_for('support_page')" in html


class TestTranslations:
    @pytest.mark.parametrize("key", ["dash_coverage_notice", "dash_coverage_contact"])
    def test_both_languages_present(self, key):
        from translations import TRANSLATIONS

        entry = TRANSLATIONS[key]
        assert entry.get("zh") and entry.get("en")

    def test_placeholder_present_in_both(self):
        from translations import TRANSLATIONS

        entry = TRANSLATIONS["dash_coverage_notice"]
        for lang in ("zh", "en"):
            assert "{cities}" in entry[lang], f"{lang} 少了占位符，城市名会显示不出来"

    def test_chinese_has_no_trailing_space(self):
        """中文的「请」和链接之间不该有空格；英文那一个空格是需要的。"""
        from translations import TRANSLATIONS

        entry = TRANSLATIONS["dash_coverage_notice"]
        assert not entry["zh"].endswith(" ")
        assert entry["en"].endswith(" ")
