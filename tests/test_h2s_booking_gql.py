"""h2s_booking_gql 是照抄品，不是设计品——守住「别手改字段」。

与 tests/test_h2s_query_fields.py 同一个道理：H2S 按 operationName + 归一化字段集
放行 GraphQL，自写或裁剪的 operation 一律 403 operation_not_allowed。这些 operation
的原文从站点 JS chunk 逐字抄来（docs/H2S_BOOKING_OPS.md §2），改动只能重新照抄。
"""
from __future__ import annotations

import re

import pytest

import h2s_booking_gql as m


class TestDocumentsWellFormed:
    def test_every_document_declares_its_operation_name(self):
        """每个文档的 operation 名必须和登记的 OP_* 常量一致。

        白名单缺 operationName 或名字不符都 403，所以调用点传的 OP_* 必须真的是
        文档里那个名字。"""
        for op_name, doc in m.DOCUMENTS.items():
            decl = re.search(r"\b(?:query|mutation)\s+([A-Za-z_][A-Za-z0-9_]*)", doc)
            assert decl, f"{op_name} 文档里找不到 operation 声明"
            assert decl.group(1) == op_name, (
                f"文档声明的是 {decl.group(1)}，登记的 OP 是 {op_name}——"
                f"白名单按名字放行，对不上就是 403"
            )

    def test_no_leftover_template_placeholder(self):
        """image_manager 那个 ${...} 片段必须已展开——留着占位符 = 语法错的查询。"""
        for op_name, doc in m.DOCUMENTS.items():
            assert "${" not in doc, f"{op_name} 里还留着未展开的模板占位符"

    def test_braces_balanced(self):
        for op_name, doc in m.DOCUMENTS.items():
            assert doc.count("{") == doc.count("}"), f"{op_name} 花括号不配对"


class TestGetProductDetailFieldSet:
    """兜底取 sku 用的查询。字段集是照抄的既成事实，多一个少一个都会 403。"""

    #: 2026-08-19 从线上 chunk 抄下来时，items 选择集的完整字段。
    #: 这不是需求清单，是上游的既成事实。变了只能重抄。
    EXPECTED_ITEM_FIELDS = {
        "name", "sku", "city", "neighborhood", "living_area", "building_name",
        "resident_type", "no_of_rooms", "min_income", "floor", "finishing",
        "flooring", "curtains", "lighting", "price_range", "private_outside_area",
        "next_contract_startdate", "current_lottery_subscribers", "allin_excl_text",
        "maximum_day_selection", "basic_rent", "location_in_building",
        "lumpsum_service_charge", "inventory", "caretaker_costs", "start_unit_date",
        "service_costs_website", "supplies_website", "income_requirements",
        "tenant_profile", "cleaning_common_areas", "energy_label",
        "energy_common_areas", "residence_video", "residence_google_maps",
        "maximum_number_of_persons", "type_of_contract", "allowance_price",
        "pets_allowed", "parking_status", "storage_available", "minimum_stay",
        "meta_description", "meta_title", "meta_keyword", "overview",
        "book_now_text", "short_description", "description", "location", "url_key",
        "offer_text", "offer_text_two", "available_to_book", "view_from_residence",
        "deposit", "small_image", "image_manager",
    }

    def _item_fields(self) -> set[str]:
        src = m.GETPRODUCTDETAIL
        start = src.index("items {") + len("items {")
        depth, i = 1, start
        while depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        body = src[start:i - 1]
        fields, depth = set(), 0
        for line in body.splitlines():
            tok = line.strip()
            if not tok:
                continue
            if depth == 0:
                mt = re.match(r"^([a-z_][a-z0-9_]*)", tok)
                if mt:
                    fields.add(mt.group(1))
            depth += tok.count("{") - tok.count("}")
        return fields

    def test_field_set_is_verbatim(self):
        got = self._item_fields()
        added = sorted(got - self.EXPECTED_ITEM_FIELDS)
        removed = sorted(self.EXPECTED_ITEM_FIELDS - got)
        assert not added, f"给照抄的查询加了字段 {added}——加一个就全量 403"
        assert not removed, f"从照抄的查询删了字段 {removed}——删一个同样 403"

    def test_the_three_fields_booker_reads_are_present(self):
        """booker 只用其中三个，但它们必须在（且不能因此就删掉其余的）。"""
        f = self._item_fields()
        assert {"sku", "type_of_contract", "next_contract_startdate"} <= f


class TestBookerImportsMatch:
    """booker 引用的常量必须真的存在于本模块，别改名漏一处。"""

    def test_booker_uses_these_operations(self):
        import booker  # noqa: F401
        # 引用即导入成功；这里再核一遍名字对得上
        assert booker._OP_PRODUCT_DETAIL == "GetProductDetail"
        assert booker._OP_ADD_BOOKING == "AddNewBooking"
        assert booker._OP_PLACE_ORDER == "PlaceOrder"
        assert booker._GQL_PRODUCT_DETAIL is m.GETPRODUCTDETAIL
        assert booker._GQL_ADD_BOOKING is m.ADDNEWBOOKING
