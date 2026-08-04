"""推测出来的状态必须能被看出来。

平台不会告诉我们「这个单元没了」——它只是把那一行从列表里拿掉。所以
``mark_stale_listings`` 在房源老化之后把它标成 ``Occupied`` 并置
``status_is_inferred=1``。

2026-08-04 走查发现：这个字段从建库起就只活在存储层，**从来没离开过**——没有
一个路由、模板或 API 读它。于是「平台报的 Occupied」和「我们猜的 Occupied」
在面板和 API 上长得一模一样，推断被当成事实端给了用户。

对 OurDomain 这类平台尤其要紧：它的 feed 只列当前可订的单元，房源没了就是
消失，所以它库里几乎所有 Occupied 都是推测的（线上 9 推测 / 1 真实）。
"""
from __future__ import annotations

import re

from app.services.listing_service import serialize_listing


def _row(**over) -> dict:
    row = {
        "id": "od_211053", "name": "Diemen #6031", "status": "Occupied",
        "price_raw": "€ 1.138", "available_from": "2026-09-14",
        "city": "Amsterdam Diemen", "source": "ourdomain",
        "url": "https://example.invalid/floorplans.aspx", "features": "[]",
        "first_seen": "2026-08-04T08:39:00+00:00",
        "last_seen": "2026-08-04T11:48:00+00:00",
        "status_is_inferred": 1,
    }
    row.update(over)
    return row


class TestApiExposesIt:
    def test_inferred_status_is_flagged(self):
        assert serialize_listing(_row())["status_is_inferred"] is True

    def test_platform_confirmed_status_is_not_flagged(self):
        assert serialize_listing(_row(status_is_inferred=0))["status_is_inferred"] is False

    def test_missing_column_defaults_to_not_inferred(self):
        """老数据/精简查询没有这一列时，默认当成平台确认的。

        默认值只能往这个方向倒：把真实状态误标成「推测」会让用户无谓怀疑，
        但那是可见的；反过来是把猜测当事实，用户看不出来。老库的
        status_is_inferred 默认就是 0，语义一致。
        """
        row = _row()
        del row["status_is_inferred"]
        assert serialize_listing(row)["status_is_inferred"] is False

    def test_it_is_a_bool_not_an_int(self):
        """SQLite 存的是 0/1，JSON 里要是 true/false——客户端不该去猜类型。"""
        assert serialize_listing(_row())["status_is_inferred"] is True
        assert serialize_listing(_row(status_is_inferred=0))["status_is_inferred"] is False


class TestListingsPageShowsIt:
    """模板层：状态胶囊旁边要有个标记。"""

    def _html(self) -> str:
        return open("templates/listings.html", encoding="utf-8").read()

    def test_table_row_renders_the_marker(self):
        html = self._html()
        assert html.count("l.status_is_inferred") >= 2, (
            "表格和移动端卡片两处都要标——只标一处等于换个设备就看不见"
        )

    def test_marker_has_an_explanation(self):
        assert "status_inferred_hint" in self._html(), (
            "光标一个「推测」没用，得说清楚推测的是什么"
        )

    def test_marker_is_translated_not_hardcoded(self):
        html = self._html()
        assert "推测" not in html and "Inferred" not in html, (
            "文案要走 translations，别写死在模板里"
        )


class TestRenderedPage:
    """真渲染一次 /listings——模板里断言字符串只能证明代码写了，证明不了它出得来。"""

    def _seed(self, isolated_data_dir):
        from mstorage import Storage

        st = Storage(isolated_data_dir / "listings.db")
        now = "2026-08-04T11:48:00+00:00"
        for lid, name, status, inferred in [
            ("od_1", "Diemen #6031", "Occupied", 1),
            ("od_2", "Diemen #6039", "Available to book", 0),
        ]:
            st.conn.execute(
                """INSERT INTO listings
                   (id,name,status,price_raw,available_from,features,url,city,
                    first_seen,last_seen,notified,last_status,source,status_is_inferred)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)""",
                (lid, name, status, "€ 1.138", "2026-09-14", "[]", "http://x/",
                 "Amsterdam Diemen", now, now, status, "ourdomain", inferred),
            )
        st.conn.commit()
        st.close()

    def test_marker_appears_exactly_on_the_inferred_row(
        self, admin_client, isolated_data_dir,
    ):
        self._seed(isolated_data_dir)
        resp = admin_client.get("/listings")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # 表格一次 + 移动端卡片一次 = 2；两条房源里只有一条是推测的
        assert html.count("badge-inferred") == 2, (
            "推测标记应当只出现在那一条推测状态的房源上（表格 + 卡片各一次）"
        )

    def test_no_marker_when_nothing_is_inferred(
        self, admin_client, isolated_data_dir,
    ):
        from mstorage import Storage

        st = Storage(isolated_data_dir / "listings.db")
        st.conn.execute(
            """INSERT INTO listings
               (id,name,status,price_raw,available_from,features,url,city,
                first_seen,last_seen,notified,last_status,source,status_is_inferred)
               VALUES ('od_2','x','Available to book','€1','','[]','http://x/','C',
                       '2026-08-04T00:00:00+00:00','2026-08-04T00:00:00+00:00',0,
                       'Available to book','ourdomain',0)"""
        )
        st.conn.commit()
        st.close()
        html = admin_client.get("/listings").get_data(as_text=True)
        assert "badge-inferred" not in html


class TestTranslations:
    def test_both_languages_present(self):
        from translations import TRANSLATIONS

        for key in ("status_inferred", "status_inferred_hint"):
            assert set(TRANSLATIONS[key]) >= {"zh", "en"}
            assert all(TRANSLATIONS[key][lang].strip() for lang in ("zh", "en"))


class TestOpenApiDocumented:
    def test_listing_schema_declares_the_field(self):
        import json

        schema = json.load(open("docs/openapi.json", encoding="utf-8"))
        props = schema["components"]["schemas"]["Listing"]["properties"]
        assert "status_is_inferred" in props, "API 加了字段，契约也要跟上"
        assert props["status_is_inferred"]["type"] == "boolean"
        assert props["status_is_inferred"].get("description", "").strip(), (
            "这个字段光有类型没用——不解释「推测」是什么意思，客户端只会忽略它"
        )
