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
