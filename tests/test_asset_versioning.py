"""静态资源的版本号必须自动算，不能再靠人手动 +1。

踩过两次：

1. 改了 static/app.js 忘了把 base.html 里写死的 ``?v=`` 加一 → 浏览器继续用
   缓存里的旧脚本，新函数不存在，统计页整页空白。
2. login.html 有自己独立的一份 ``?v=28``，跟着 base.html 漏了 6 次 →
   线上登录页（新访客看到的第一个页面）一直在发过期样式表。

第二个是走查部署完之后才发现的：base.html 已经是 v=34，线上 /login 返回的
还是 v=28。两个地方各写一份版本号，就一定有一份会忘。

所以这里盯两件事：模板里不许再出现写死的 ``?v=``；以及文件一变，
``asset()`` 给出的版本号必须跟着变。
"""
from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

#: 写死的 /static/xxx?v=123
_HARDCODED = re.compile(r"/static/[^\"'\s]+\?v=", re.I)


class TestNoHardcodedVersions:
    def test_templates_use_the_asset_helper(self):
        offenders = []
        for path in sorted(TEMPLATES.glob("*.html")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _HARDCODED.search(line):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()[:90]}")
        assert not offenders, (
            "模板里还有写死的静态资源版本号。改用 {{ asset('xxx.css') }}，"
            "否则改了文件忘了改版本号，用户会拿到缓存里的旧文件：\n  "
            + "\n  ".join(offenders)
        )

    def test_every_template_referencing_core_assets_uses_asset(self):
        """反过来确认 asset() 真的被用上了，别把断言写成永远为真。"""
        users = [p.name for p in TEMPLATES.glob("*.html")
                 if "asset('design.css')" in p.read_text(encoding="utf-8")]
        assert set(users) >= {"base.html", "login.html"}, users


class TestAssetHelper:
    def test_url_carries_a_version(self, test_app):
        with test_app.test_request_context():
            url = test_app.jinja_env.globals["asset"]("design.css")
        assert url.startswith("/static/design.css?v=")

    def test_version_changes_when_the_file_changes(self, test_app, tmp_path, monkeypatch):
        """核心保证：内容一变版本号就得变，否则等于没有版本号。"""
        static = tmp_path / "static"
        static.mkdir()
        target = static / "probe.css"
        target.write_text("a{}", encoding="utf-8")
        monkeypatch.setattr(test_app, "static_folder", str(static))
        monkeypatch.setattr(test_app, "debug", True)  # debug 下不走缓存

        asset = test_app.jinja_env.globals["asset"]
        with test_app.test_request_context():
            first = asset("probe.css")
            target.write_text("a{color:red}", encoding="utf-8")
            import os
            os.utime(target, (0, 0))  # 强制改 mtime，避免同秒写入取到相同摘要
            second = asset("probe.css")

        assert first != second, "文件改了但版本号没变——缓存永远刷不掉"

    def test_missing_file_does_not_raise(self, test_app, tmp_path, monkeypatch):
        """资源缺失时退化成无版本号，不能连页面一起 500。"""
        monkeypatch.setattr(test_app, "static_folder", str(tmp_path))
        with test_app.test_request_context():
            assert test_app.jinja_env.globals["asset"]("nope.css") == "/static/nope.css"


class TestRenderedPages:
    def test_login_and_dashboard_agree_on_the_css_version(self, admin_client, client):
        """login.html 和 base.html 各写各的版本号，正是线上那次漂移的成因。"""
        login = client.get("/login").get_data(as_text=True)
        dash = admin_client.get("/").get_data(as_text=True)
        pat = re.compile(r"/static/design\.css\?v=([0-9a-f]+)")
        assert pat.search(login), "登录页没带版本号"
        assert pat.search(dash), "仪表盘没带版本号"
        assert pat.search(login).group(1) == pat.search(dash).group(1)
