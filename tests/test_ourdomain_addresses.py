"""OurDomain 两栋楼的街道地址。

这个字段只有一个用途：geocode。unit 名（"Diemen #6045"）是内部房号，不可
geocode，所以地图上的 pin 完全取决于 ``BUILDINGS[...]["street_address"]``。
它错了不会有任何报错——房源照常抓、照常通知，只是地图上钉在别的地方。

2026-08-04 发现两条**都是错的，而且互相串了**：

    diemen      →  Wenckebachweg 51, 1096 AN Amsterdam   （第三个地方）
    south-east  →  Dalsteindreef 20-40, 1112 XC Diemen   （Diemen 那栋的街道）

两个 pin 各偏 4–5 km。正确值取自 RentCafe 页脚（平台自己写的建筑地址）：

    OurDomain Amsterdam Diemen      Dalsteindreef, DIEMEN 1112 XJ
    OurDomain Amsterdam South East  Markelerbergpad 5, Amsterdam 1105 AW

这里不去断言具体门牌号——那会把测试变成「复读一遍常量」。断言的是几条**能
独立判断对错**的性质：楼名和地址所在的城市要自洽，两栋楼不能共用一个地址，
以及那个已知错误的街道不能再出现。
"""
from __future__ import annotations

from scrapers.ourdomain import OurDomainScraper

BUILDINGS = OurDomainScraper.BUILDINGS


def _addr(key: str) -> str:
    return BUILDINGS[key]["street_address"]


class TestAddressesAreSelfConsistent:
    def test_diemen_is_in_diemen(self):
        """楼名叫 Amsterdam Diemen，地址就得落在 Diemen。"""
        assert "diemen" in _addr("diemen").casefold()

    def test_south_east_is_in_amsterdam(self):
        """Amsterdam South East 在阿姆斯特丹东南，不在 Diemen。"""
        addr = _addr("south-east").casefold()
        assert "amsterdam" in addr
        assert "diemen" not in addr, "串成 Diemen 那栋的地址了"

    def test_the_two_buildings_do_not_share_an_address(self):
        """两栋楼是两个物理建筑，共用一个地址说明有一条抄错了。"""
        assert _addr("diemen") != _addr("south-east")

    def test_the_known_wrong_street_is_gone(self):
        """Wenckebachweg 既不是这两栋楼里的任何一栋。"""
        for key in BUILDINGS:
            assert "wenckebachweg" not in _addr(key).casefold()

    def test_every_building_has_a_geocodable_address(self):
        """至少要有街道 + 荷兰邮编，否则 geocode 出来是个城市级的粗略点。"""
        import re

        for key, meta in BUILDINGS.items():
            addr = meta.get("street_address", "")
            assert addr, f"{key} 没有街道地址，地图上会掉 pin"
            assert re.search(r"\b\d{4}\s?[A-Z]{2}\b", addr), (
                f"{key} 的地址里没有荷兰邮编：{addr!r}"
            )
