"""落地页的 <title> 与 meta description。

``/`` 对匿名访客 302 到 ``/login``，所以 login.html 的这两行**就是搜索结果里
显示的标题和描述**。原先标题写的是「登录 · FlatRadar」——一个 26.9KB 的落地页
只说「登录」，而 description 完全没有，描述由 Google 自己从正文里截。

这两条只给搜索引擎和分享卡片看，界面上一个字都不显示，所以很容易被当成无关紧要
而改坏。用例把三件事钉住：长度、双语、不点名被监控的平台。
"""
from __future__ import annotations

import pytest

from translations import TRANSLATIONS

#: 搜索结果的截断点。中英上限不同是因为按像素宽度截，中文字更宽。
_LIMITS = {
    ("login_title", "zh"): 30,
    ("login_title", "en"): 60,
    ("login_meta_description", "zh"): 80,
    ("login_meta_description", "en"): 155,
}


class TestLengths:
    @pytest.mark.parametrize("key,lang,limit", [(k, l, v) for (k, l), v in _LIMITS.items()])
    def test_within_the_truncation_point(self, key, lang, limit):
        v = TRANSLATIONS[key][lang]
        assert len(v) <= limit, (
            f"{key}/{lang} 有 {len(v)} 字符，超过 {limit}——超出的部分会被搜索"
            f"结果截掉，写了等于没写"
        )

    @pytest.mark.parametrize("key", ["login_title", "login_meta_description"])
    def test_not_empty(self, key):
        for lang in ("zh", "en"):
            assert TRANSLATIONS[key][lang].strip()


class TestContent:
    def test_title_is_not_just_login(self):
        """原来的毛病：26.9KB 的落地页，标题只说「登录」。"""
        for lang in ("zh", "en"):
            t = TRANSLATIONS["login_title"][lang]
            assert t.strip() not in ("登录 · FlatRadar", "Login · FlatRadar"), (
                "标题退回成了纯「登录」，搜索结果里没人会点"
            )
            assert "FlatRadar" in t, "品牌名要在标题里"

    def test_does_not_name_monitored_platforms(self):
        """刻意的取舍，不是疏漏。

        写上 Holland2Stay / Xior 会让 SEO 更强，但一来提高对被监控平台的曝光，
        二来在自己的标题里用别家商标另有一层风险。哪天想改，应该是有意识地推翻
        这个决定，而不是顺手加两个词。
        """
        for key in ("login_title", "login_meta_description"):
            for lang in ("zh", "en"):
                v = TRANSLATIONS[key][lang].lower()
                for brand in ("holland2stay", "ourdomain", "ourcampus", "xior"):
                    assert brand not in v, f"{key}/{lang} 点名了 {brand}"

    def test_description_says_what_it_does(self):
        """描述要落到具体能力上，不能只是一句口号。"""
        zh = TRANSLATIONS["login_meta_description"]["zh"]
        assert "荷兰" in zh
        en = TRANSLATIONS["login_meta_description"]["en"].lower()
        assert "dutch" in en or "netherlands" in en


class TestRendered:
    def test_login_page_carries_both(self, client):
        # 显式要中文：2026-08-27 起，**不带 Accept-Language 的客户端回落到 en**
        # （Googlebot 就是这么爬的），默认渲染的已经不是中文了。
        html = client.get(
            "/login", headers={"Accept-Language": "zh-CN"}).get_data(as_text=True)
        assert f"<title>{TRANSLATIONS['login_title']['zh']}</title>" in html
        assert 'name="description"' in html

    def test_english_switches_both(self, client):
        html = client.get("/login?lang=en").get_data(as_text=True)
        assert TRANSLATIONS["login_title"]["en"] in html
        assert TRANSLATIONS["login_meta_description"]["en"] in html

    def test_title_key_is_only_used_for_the_title(self):
        """login_title 只该出现在 <title> 和 og:title 里。

        它如果哪天被当成**界面文案**复用，改 SEO 标题就会顺带改坏页面上的字。
        守的是这个，不是「只能出现一次」——og:title 复用它恰恰是对的：分享卡片
        的标题和搜索结果的标题写成两份，只会各自漂移。它们相等这件事由
        tests/test_social_meta.py::TestShareCard::test_title_matches_the_page_title
        钉住。
        """
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        hits = []
        for f in (root / "templates").glob("*.html"):
            txt = f.read_text(encoding="utf-8")
            if "login_title" in txt:
                for line in txt.splitlines():
                    if "login_title" not in line:
                        continue
                    if "<title>" in line or "set social_title" in line:
                        continue
                    hits.append(f"{f.name}: {line.strip()[:60]}")
        assert not hits, "login_title 被用在了 <title> / og:title 之外：\n" + "\n".join(hits)
