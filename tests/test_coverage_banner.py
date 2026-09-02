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
        assert "monitored_cities_by_source()" in src, "城市名没有从配置推导，多半是写死了"
        assert "coverage=coverage" in src

    def test_route_survives_a_config_error(self):
        """配置读不出来时不显示横幅，而不是让整个首页 500。"""
        import inspect

        from app.routes import dashboard

        src = inspect.getsource(dashboard.index)
        i = src.index("monitored_cities_by_source()")
        assert "except Exception" in src[i - 600:i + 600]

    def test_template_hides_the_block_when_empty(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "{% if coverage %}" in html

    def test_route_marks_shadow_sources(self):
        """影子 source 必须带标记传给模板。

        它们在抓、在入库、在房源列表里看得见，但**不发通知**。不标记等于在覆盖说明
        里承诺一个不会兑现的推送——2026-09-02 生产上就是这个状态：横幅列出的 17 个
        城市里有 9 个只由影子 source（Plaza / Student Experience）覆盖。
        """
        src = (ROOT / "app" / "routes" / "dashboard.py").read_text(encoding="utf-8")
        assert "shadow_sources" in src
        assert '"shadow"' in src

    def test_support_link_uses_url_for(self):
        """写死 /support 会在挂到子路径时断掉。"""
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "url_for('support_page')" in html


class TestTranslations:
    @pytest.mark.parametrize("key", ["dash_coverage_title", "dash_coverage_notice",
                                     "dash_coverage_shadow", "dash_coverage_contact"])
    def test_both_languages_present(self, key):
        from translations import TRANSLATIONS

        entry = TRANSLATIONS[key]
        assert entry.get("zh") and entry.get("en")

    def test_no_stale_city_placeholder(self):
        """城市名不再拼进译文——分组之后由模板逐行渲染。

        译文里留着 `{cities}` 而模板不再替换的话，用户会看见字面量花括号。
        """
        from translations import TRANSLATIONS

        for lang in ("zh", "en"):
            assert "{cities}" not in TRANSLATIONS["dash_coverage_notice"][lang]
            assert "{cities}" not in TRANSLATIONS["dash_coverage_title"][lang]

    def test_chinese_has_no_trailing_space(self):
        """中文的「请」和链接之间不该有空格；英文那一个空格是需要的。"""
        from translations import TRANSLATIONS

        entry = TRANSLATIONS["dash_coverage_notice"]
        assert not entry["zh"].endswith(" ")
        assert entry["en"].endswith(" ")


class TestSupportEmail:
    """横幅上的邮箱必须与支持页同源。

    2026-08-06 发现生产 .env 里写的是 `supprot@flatradar.app`（拼错），支持页照着
    显示，而那个页面正是 App Store 登记的 Support URL——写信过去是收不到的。
    仓库文档里还是第三种拼法 `surrport@`。三处各写各的，就一定会有一处是错的。

    所以横幅不自己写死地址，取 `support_text.CONTACT_EMAIL`（即 SUPPORT_EMAIL）。
    """

    def test_route_takes_it_from_config(self):
        src = (ROOT / "app" / "routes" / "dashboard.py").read_text(encoding="utf-8")
        assert "CONTACT_EMAIL" in src, "邮箱写死了，迟早和支持页对不上"
        assert "support_email=" in src

    def test_template_renders_a_mailto(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "mailto:{{ support_email }}" in html

    def test_template_hides_it_when_unset(self):
        """没配 SUPPORT_EMAIL 时不该渲染一个空的 mailto: 链接。"""
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "{% if support_email %}" in html

    def test_no_hardcoded_address_anywhere_in_the_banner(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "@flatradar.app" not in html

    @pytest.mark.parametrize("typo", ["supprot@", "surrport@"])
    def test_known_misspellings_are_gone_from_docs(self, typo):
        """两种错拼都出现过；留一个在文档里，下一次部署就会再抄一遍。

        只查面向部署者的 README——CHANGELOG 记录的是「当时错成什么样」，那是历史，
        不是让人照抄的地址。（这条测试第一版连 CHANGELOG 一起查，于是被记录该修复
        的那段文字判为不合格。）
        """
        for name in ("README.md", "README_cn.md"):
            text = (ROOT / "docs" / name).read_text(encoding="utf-8")
            assert typo not in text, f"{name} 仍有 {typo}"


class TestGroupedByPlatform:
    """并集在七个平台之后读不动了，横幅改成按平台分组。"""

    def test_grouping_keeps_each_platform_separate(self, env):
        cfg = env(SOURCES="holland2stay,magis", CITIES="Amsterdam,24")
        by_src = cfg.monitored_cities_by_source()
        assert by_src["holland2stay"] == ["Amsterdam"]
        assert "Eindhoven" in by_src["magis"]
        # 分组不该把别人的城市混进来
        assert "Eindhoven" not in by_src["holland2stay"]

    def test_union_still_agrees_with_the_groups(self, env):
        """``monitored_city_names`` 现在由分组推导，两者不能各说各的。"""
        cfg = env(SOURCES="holland2stay,magis,plaza", CITIES="Amsterdam,24")
        merged = set()
        for cities in cfg.monitored_cities_by_source().values():
            merged.update(cities)
        assert sorted(merged) == cfg.monitored_city_names()

    def test_disabled_source_is_absent(self, env):
        """平台没开就不能出现——城市列表填着但 source 没启用，一条都不会抓。"""
        cfg = env(SOURCES="holland2stay", CITIES="Amsterdam,24")
        assert set(cfg.monitored_cities_by_source()) == {"holland2stay"}

    def test_buildings_are_normalised_within_a_platform(self, env):
        """楼盘名归一到城市：Xior 的两栋 Eindhoven 楼是一个 Eindhoven。"""
        eind = [r for r in config.KNOWN_XIOR_CITIES if r["city"] == "Eindhoven"][:2]
        assert len(eind) == 2, "Eindhoven 应当有不止一栋楼，否则这条测试空过"
        ams = _xior("Amsterdam")
        picked = eind + [ams]
        cfg = env(SOURCES="xior", XIOR_CITIES="|".join(
            f"{r['city']} {r['bldg']},{r['key']}" for r in picked))
        assert cfg.monitored_cities_by_source()["xior"] == ["Amsterdam", "Eindhoven"]


class TestShadowIsVisible:
    """影子 source 既不能隐藏、也不能不标注。"""

    def test_shadow_sources_are_still_listed(self, env, monkeypatch):
        """隐藏会让用户在房源列表里看见 Plaza 的房子，却在覆盖说明里找不到那个城市。"""
        monkeypatch.setenv("SHADOW_SOURCES", "plaza")
        cfg = env(SOURCES="holland2stay,plaza", CITIES="Amsterdam,24")
        assert "plaza" in cfg.monitored_cities_by_source()
        assert cfg.shadow_sources == ["plaza"]

    def test_rendered_page_badges_only_the_shadow_platform(self, admin_client, monkeypatch):
        """**端到端**：真渲染一次，角标必须出现在影子平台那一行、且只在那一行。

        这条不能靠「路由源码里有 `shadow` 字样」来守——把 `"shadow": False` 写死
        同样含有那个字样，grep 抓不到。也不能靠测试自己重算一遍 rows：那样验的是
        测试自己的算法，不是路由的。
        """
        import re

        monkeypatch.setenv("SOURCES", "holland2stay,plaza")
        monkeypatch.setenv("CITIES", "Amsterdam,24")
        monkeypatch.setenv("SHADOW_SOURCES", "plaza")

        from translations import TRANSLATIONS
        badge = TRANSLATIONS["dash_coverage_shadow"]["en"]

        html = admin_client.get("/").get_data(as_text=True)
        i = html.index(TRANSLATIONS["dash_coverage_title"]["en"])
        block = html[i:i + 3000]

        assert badge in block, "影子平台没有角标——等于承诺了不会兑现的推送"
        # 角标只能有一个：Holland2Stay 不是影子，不该被标
        assert block.count(badge) == 1
        # 且必须紧跟在 Plaza 后面，不是挂在 Holland2Stay 上
        plaza_at, h2s_at = block.index("Plaza"), block.index("Holland2Stay")
        badge_at = block.index(badge)
        assert plaza_at < badge_at, "角标出现在 Plaza 之前，多半挂错了平台"
        assert not (h2s_at < badge_at < plaza_at)

    def test_template_renders_the_badge(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        assert "row.shadow" in html
        assert "dash_coverage_shadow" in html


class TestBannerLayout:
    def test_uses_a_dedicated_class_not_alert(self):
        """横幅**不能**用 .alert。

        那个类是 `display:flex; align-items:center`——标题、表格、联系方式三个子元素
        会被排成一行并垂直居中：标题贴最左、联系方式飘到最右、中间一大片空白。
        2026-09-02 就是这么渲染出来的。
        """
        import re

        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        # 先剥 Jinja 注释：模板里那段说明本身就写着「而不是 .alert」，
        # 不剥的话测试咬中的是自己的注释而不是标记。
        html = re.sub(r"\{#.*?#\}", " ", html, flags=re.S)
        i = html.index("coverage-banner")
        block = html[i - 200:i + 1400]
        assert "alert" not in block, "覆盖横幅又用回 .alert 了，布局会塌"

    def test_styles_exist(self):
        css = (ROOT / "static" / "design.css").read_text(encoding="utf-8")
        for cls in (".coverage-banner", ".coverage-grid", ".coverage-src",
                    ".coverage-cities", ".coverage-shadow", ".coverage-foot"):
            assert cls in css, f"{cls} 没有样式，会渲染成裸文本"

    def test_narrow_screens_stack(self):
        """窄屏必须把 max-content 那一列拆开，否则城市被挤成一条缝。

        断言按**内容**判而不是按距离：上一版写的是「从 .coverage-grid 往后 1200
        字符内出现 max-width:640px」，中间插一段注释就会失败——那验的是源码排版，
        不是行为。
        """
        import re

        css = (ROOT / "static" / "design.css").read_text(encoding="utf-8")
        blocks = re.findall(r"@media\s*\(max-width:\s*640px\)\s*\{(.*?)\n\}",
                            css, re.S)
        assert any(".coverage-grid" in b and "grid-template-columns:1fr" in b
                   for b in blocks), "窄屏没有把平台名与城市拆成上下两行"
