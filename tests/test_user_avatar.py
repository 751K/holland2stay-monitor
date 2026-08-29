"""
用户卡上的头像与序号徽章。

头像原本是饱和渐变加白字，而色相取自 ``loop.index0``——用户卡是可拖拽排序的，
拖一下所有人的颜色跟着换一遍，认的是行号不是人。更根本的是：全站的颜色只有一个
accent、四个语义色和四个渠道品牌色，「每人一个色相」是一套别处都不用的调色板，
本身就是不协调的来源。识别靠的是紧挨着的用户名，颜色从来没承担过这件事。

序号徽章是这一簇里最后一个正圆，而头像是 10px 方圆——两个同色小块、一圆一方
并排，正是「形状不成套」最直接的样子。
"""
from __future__ import annotations

import inspect
import re

import pytest


# ── 头像 ────────────────────────────────────────────────────────

class TestAvatar:
    """头像不再按人分色。

    全站的颜色只有一个 accent、四个语义色和四个渠道品牌色。「每人一个色相」是
    一套别处都不用的调色板，本身就是不协调的来源；而识别靠的是紧挨着的用户名，
    颜色从来没承担过这件事。

    此前色相还取自 ``loop.index0``——用户卡是可拖拽排序的，拖一下所有人的颜色
    跟着换一遍，认的是行号不是人。
    """

    def _css(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "static" / "design.css").read_text(encoding="utf-8")

    def _tpl(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / "templates" / "users.html").read_text(encoding="utf-8")

    def test_no_per_user_colour_left(self):
        tpl = self._tpl()
        assert "linear-gradient" not in tpl
        line = next(l for l in tpl.splitlines() if 'class="user-avatar"' in l)
        for gone in ("loop.index0", "avatar_hue", "--avatar-h", "hsl("):
            assert gone not in line, f"头像那一行还留着 {gone}"

    def test_avatar_hue_filter_is_gone(self):
        """过滤器随判据一起删掉，不留一个没有调用方的函数。"""
        import app.jinja_filters as jf

        assert not hasattr(jf, "avatar_hue")
        assert "avatar_hue" not in inspect.getsource(jf)

    def test_uses_theme_variables(self):
        """两个值都是主题变量，深色下自动跟随。

        写死 hsl 的话就得再补一段 [data-theme="dark"]，而漏补的表现是深色主题里
        一块 93% 亮度的方块在深色卡片上发光。
        """
        css = self._css()
        block = css[css.index(".user-avatar{"):]
        block = block[:block.index("}")]
        assert "var(--accent-soft)" in block
        assert "var(--accent)" in block
        assert "hsl(" not in block, "又写死了颜色"
        assert "#fff" not in block

    def test_no_dark_override_needed(self):
        """变量方案的好处就是不需要覆盖——留着一段空覆盖只会让人以为要两处同步。"""
        assert '[data-theme="dark"] .user-avatar{' not in self._css()

    def test_radius_follows_the_shape_language(self):
        """6px 是操作，10px 是状态，头像属于后者。"""
        css = self._css()
        block = css[css.index(".user-avatar{"):]
        block = block[:block.index("}")]
        assert "border-radius:var(--radius-lg)" in block


class TestRankBadge:
    """序号徽章紧挨着头像，两者要成套。

    它此前是 ``border-radius:50%`` 的正圆、accent 色的字，而头像是 10px 方圆的
    accent 块——两个同色小块、一圆一方并排，正是「形状不成套」最直接的样子。
    """

    def _block(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent
               / "static" / "design.css").read_text(encoding="utf-8")
        i = css.index(".rank-badge{")
        return css[i:css.index("}", i)]

    def test_not_a_circle_anymore(self):
        assert "border-radius:50%" not in self._block()

    def test_shares_the_avatar_radius_token(self):
        """同一个 token，而不是碰巧差不多的两个数。"""
        from pathlib import Path
        css = (Path(__file__).resolve().parent.parent
               / "static" / "design.css").read_text(encoding="utf-8")
        av = css[css.index(".user-avatar{"):]
        av = av[:av.index("}")]
        assert "border-radius:var(--radius-lg)" in self._block()
        assert "border-radius:var(--radius-lg)" in av

    def test_does_not_compete_with_the_avatar_colour(self):
        """序号是排序元数据，不该和头像抢同一个强调色。"""
        blk = self._block()
        assert "color:var(--text3)" in blk
        assert "color:var(--accent)" not in blk

    def test_two_digit_rank_still_fits(self):
        """第 10 名之后是两位数，固定 width 会把它挤出去。"""
        blk = self._block()
        assert "min-width:24px" in blk, "还是写死的 width，两位数会溢出"
        assert "padding:0 5px" in blk


class TestAvatarInPage:
    def _mk_user(self, client, name):
        from app.auth import _REGISTER_RECORDS
        _REGISTER_RECORDS.clear()
        with client.session_transaction() as sess:
            sess.clear()
            sess["csrf_token"] = "test_csrf"
        r = client.post("/register", data={
            "csrf_token": "test_csrf", "register_username": name,
            "register_password": "pw1234", "terms_accepted": "1",
        }, follow_redirects=False)
        assert r.status_code in (302, 303), r.status_code
        from users import load_users
        match = [u for u in load_users() if u.name == name]
        assert match, f"{name} 没有建出来"
        return match[0].id

    def test_every_avatar_is_identical_markup(self, client, admin_client):
        """两个用户的头像标签必须一模一样——差异全部交给 CSS。

        内联样式一旦回来，主题切换就管不到它了。
        """
        import re

        self._mk_user(client, "AvatarOne")
        self._mk_user(client, "AvatarTwo")

        html = admin_client.get("/users").get_data(as_text=True)
        tags = re.findall(r'<div class="user-avatar"[^>]*>', html)
        assert len(tags) >= 2
        assert len(set(tags)) == 1, f"头像标签不一致: {set(tags)}"
        assert "style=" not in tags[0]

    def test_initial_is_still_shown(self, client, admin_client):
        self._mk_user(client, "Zebra")
        html = admin_client.get("/users").get_data(as_text=True)
        i = html.index('class="user-avatar"')
        assert "Z" in html[i:i + 120]
