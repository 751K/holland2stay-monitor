"""
config.py 单元测试。
"""
import config


class TestGetImpersonate:
    def test_consecutive_never_returns_same(self, monkeypatch):
        """连续两次调用不应返回同一指纹（池大小 > 1 时）。"""
        # 固定随机种子，避免偶发失败
        import random
        monkeypatch.setattr(random, "choices", lambda pool, weights, k: [pool[0]])
        first = config.get_impersonate()
        # 第二次调用：上次选中的值应被排除，所以返回 pool 中下一个
        second = config.get_impersonate()
        assert first != second, f"连续两次返回了相同指纹: {first}"


class TestScrapeTasksV2:
    def test_default_sources_is_holland2stay(self, monkeypatch):
        monkeypatch.delenv("SOURCES", raising=False)
        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()
        assert cfg.sources == ["holland2stay"]
        assert tasks
        assert {t.source for t in tasks} == {"holland2stay"}

    def test_ourdomain_source_adds_ourdomain_tasks(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "holland2stay,ourdomain")
        monkeypatch.setenv("CITIES", "Eindhoven,29")
        monkeypatch.setenv("OURDOMAIN_CITIES", "Amsterdam Diemen,diemen")

        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()

        assert [t.source for t in tasks] == ["holland2stay", "ourdomain"]
        assert tasks[0].city_display == "Eindhoven"
        assert tasks[0].extra["availability_ids"] == ["179", "336"]
        assert tasks[1].city_key == "diemen"
        assert tasks[1].city_display == "Amsterdam Diemen"

    def test_ourdomain_only_uses_default_city(self, monkeypatch):
        monkeypatch.setenv("SOURCES", "ourdomain")
        monkeypatch.delenv("OURDOMAIN_CITIES", raising=False)

        cfg = config.load_config()
        tasks = cfg.scrape_tasks_v2()

        assert cfg.sources == ["ourdomain"]
        assert len(tasks) == 1
        assert tasks[0].source == "ourdomain"
        assert tasks[0].city_key == "diemen"
