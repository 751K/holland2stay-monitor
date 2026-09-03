import XCTest
@testable import FlatRadar

/// 通知筛选器在设置页上的呈现。
///
/// 守的是同一类错：**界面把"不知道"或"没覆盖到"说成了一个确定的答案。**
///
/// 1. ``ListingFilter.summary`` 原先只覆盖十三个维度里的六个。只勾了
///    "Finishing: Furnished" 的用户，设置页写着 "No filters"——而后端
///    ``config.py`` 的 ``matches()`` 正照着这条把推送过滤掉。
/// 2. ``PlatformScope`` 说的是另一件从没露过面的事：一个维度只对登记了它的
///    平台生效。合同期限七个平台里只对 Holland2Stay 生效，用户设了以为是全局
///    条件。后端不返回这张表时（老版本）必须**不作标注**，而不是断言"全都生效"。
final class FilterSummaryTests: XCTestCase {

    // MARK: - 每个维度都必须能被摘要看见

    /// 逐个维度单独设一条，摘要都不能是 "No filters"。
    ///
    /// 这条断言写成循环而不是十三个 `XCTAssertEqual`，是因为新增维度时
    /// `mutations` 少加一项不会有人注意——它至少会和 `isEmpty` 对不上，
    /// 由下面 `test_summary_agrees_with_isEmpty` 兜住。
    func test_every_dimension_appears_in_summary() {
        for (name, filter) in Self.singleDimensionFilters {
            XCTAssertFalse(filter.isEmpty, "\(name): 构造的样例本身就是空的")
            XCTAssertFalse(filter.summaryParts.isEmpty,
                           "\(name) 已设置，摘要却一段都没有")
            XCTAssertNotEqual(filter.summary, "No filters",
                              "\(name) 已设置，摘要仍写着 No filters")
        }
    }

    /// 摘要为空 ⟺ 过滤器为空。两边任何一侧漏掉一个维度都会在这里露馅。
    func test_summary_agrees_with_isEmpty() {
        XCTAssertTrue(ListingFilter.empty.summaryParts.isEmpty)
        XCTAssertEqual(ListingFilter.empty.summary, "No filters")
        for (name, filter) in Self.singleDimensionFilters {
            XCTAssertEqual(filter.isEmpty, filter.summaryParts.isEmpty,
                           "\(name): isEmpty 与摘要不一致")
        }
    }

    /// 十三个维度全设满时，摘要应当有十三段——一段都不许合并掉。
    func test_all_dimensions_at_once_yield_thirteen_parts() {
        var f = ListingFilter.empty
        f.maxRent = 900
        f.minArea = 25
        f.minFloor = 2
        f.allowedCities = ["Eindhoven"]
        f.allowedNeighborhoods = ["Strijp-S"]
        f.allowedSources = ["holland2stay"]
        f.allowedEnergy = "B"
        f.allowedTypes = ["Studio"]
        f.allowedOccupancy = ["Single"]
        f.allowedTenant = ["Students only"]
        f.allowedContract = ["Indefinite"]
        f.allowedFinishing = ["Furnished"]
        f.allowedOffer = ["First month free"]
        XCTAssertEqual(f.summaryParts.count, 13, f.summary)
    }

    // MARK: - 各段的写法

    func test_numeric_parts_read_as_bounds() {
        var f = ListingFilter.empty
        f.maxRent = 900
        f.minArea = 25
        f.minFloor = 0
        XCTAssertEqual(f.summaryParts, ["≤ €900/mo", "≥ 25 m²", "Floor ≥ 0"])
    }

    /// 楼层 0 是「一层」，不是「没设」—— `if let` 而不是 `if f > 0`。
    func test_min_floor_zero_is_a_real_condition() {
        var f = ListingFilter.empty
        f.minFloor = 0
        XCTAssertFalse(f.isEmpty)
        XCTAssertTrue(f.summaryParts.contains("Floor ≥ 0"))
    }

    func test_sources_use_platform_short_names() {
        var f = ListingFilter.empty
        f.allowedSources = ["holland2stay", "studentexperience"]
        XCTAssertEqual(f.summaryParts, ["H2S, SE"])
    }

    /// 值本身读不出维度的（"Two"、"Indefinite"）必须带维度名，
    /// 否则摘要成了一串没有主语的词。
    func test_ambiguous_values_are_labeled() {
        var f = ListingFilter.empty
        f.allowedOccupancy = ["Two"]
        f.allowedContract = ["Indefinite"]
        f.allowedTenant = ["Students only"]
        f.allowedOffer = ["First month free"]
        XCTAssertEqual(f.summaryParts, [
            "Occupancy: Two", "Tenant: Students only",
            "Contract: Indefinite", "Offer: First month free",
        ])
    }

    // MARK: - brief()

    func test_brief_lists_up_to_two_then_counts_the_rest() {
        XCTAssertEqual(ListingFilter.brief(["A"]), "A")
        XCTAssertEqual(ListingFilter.brief(["A", "B"]), "A, B")
        XCTAssertEqual(ListingFilter.brief(["A", "B", "C"]), "A, B +1")
        XCTAssertEqual(ListingFilter.brief(["A", "B", "C", "D"]), "A, B +2")
    }

    func test_brief_never_reports_plus_zero() {
        for n in 1...5 {
            let out = ListingFilter.brief((1...n).map { "v\($0)" })
            XCTAssertFalse(out.contains("+0"), out)
        }
    }

    func test_brief_label_prefixes_the_body() {
        XCTAssertEqual(ListingFilter.brief(["X", "Y", "Z"], label: "Tenant"),
                       "Tenant: X, Y +1")
    }

    /// 三个以上城市要看得出"还有别的"，否则用户以为自己只选了两个。
    func test_many_cities_signal_the_remainder() {
        var f = ListingFilter.empty
        f.allowedCities = ["Amsterdam", "Eindhoven", "Rotterdam", "Utrecht"]
        XCTAssertEqual(f.summaryParts, ["Amsterdam, Eindhoven +2"])
    }

    // MARK: - 样例

    /// 每个维度一条、其余留空的十三个过滤器。
    private static var singleDimensionFilters: [(String, ListingFilter)] {
        func make(_ mutate: (inout ListingFilter) -> Void) -> ListingFilter {
            var f = ListingFilter.empty
            mutate(&f)
            return f
        }
        return [
            ("maxRent",       make { $0.maxRent = 900 }),
            ("minArea",       make { $0.minArea = 25 }),
            ("minFloor",      make { $0.minFloor = 2 }),
            ("cities",        make { $0.allowedCities = ["Eindhoven"] }),
            ("neighborhoods", make { $0.allowedNeighborhoods = ["Strijp-S"] }),
            ("sources",       make { $0.allowedSources = ["holland2stay"] }),
            ("energy",        make { $0.allowedEnergy = "B" }),
            ("types",         make { $0.allowedTypes = ["Studio"] }),
            ("occupancy",     make { $0.allowedOccupancy = ["Single"] }),
            ("tenant",        make { $0.allowedTenant = ["Students only"] }),
            ("contract",      make { $0.allowedContract = ["Indefinite"] }),
            ("finishing",     make { $0.allowedFinishing = ["Furnished"] }),
            ("offer",         make { $0.allowedOffer = ["First month free"] }),
        ]
    }
}

/// ``PlatformScope`` —— 把后端的维度/平台能力表翻译成一句说明。
final class PlatformScopeTests: XCTestCase {

    private let h2sOnly = ["holland2stay"]
    private let all = Platform.knownKeys

    // MARK: - 什么时候闭嘴

    /// 老 backend 不返回 `dim_sources` —— 没有信息就别写。
    /// 退回"对所有平台生效"是把「不知道」说成了一个确定的答案。
    func test_no_note_when_table_is_missing() {
        XCTAssertNil(PlatformScope.note(appliesTo: [], selectedSources: []))
        XCTAssertNil(PlatformScope.note(appliesTo: [], selectedSources: h2sOnly))
    }

    /// 覆盖全部已知平台的维度（max_rent / city / …）没什么可提示的。
    func test_no_note_when_dimension_covers_every_platform() {
        XCTAssertNil(PlatformScope.note(appliesTo: all, selectedSources: []))
    }

    /// 用户选的平台**全部**支持该维度 —— 也没什么可提示的。
    func test_no_note_when_every_selected_platform_supports_it() {
        XCTAssertNil(PlatformScope.note(appliesTo: ["holland2stay", "magis"],
                                        selectedSources: ["magis"]))
    }

    // MARK: - 什么时候必须说话

    func test_names_the_supported_platforms_when_no_source_selected() {
        let note = PlatformScope.note(appliesTo: h2sOnly, selectedSources: [])
        XCTAssertNotNil(note)
        XCTAssertTrue(note!.text.contains("Holland2Stay"), note!.text)
        XCTAssertFalse(note!.isWarning)
    }

    /// 最要紧的一档：用户勾的平台一个都不支持这条维度 —— 这条过滤等于没设。
    func test_warns_when_selection_and_dimension_do_not_overlap() {
        let note = PlatformScope.note(appliesTo: h2sOnly, selectedSources: ["xior"])
        XCTAssertNotNil(note)
        XCTAssertTrue(note!.isWarning)
        XCTAssertTrue(note!.text.lowercased().contains("no effect"), note!.text)
    }

    /// 部分重叠：讲清楚"你选的这几个里只有哪几个会被影响"。
    func test_lists_the_effective_subset_on_partial_overlap() {
        let note = PlatformScope.note(appliesTo: ["holland2stay", "magis"],
                                      selectedSources: ["holland2stay", "xior"])
        XCTAssertNotNil(note)
        XCTAssertFalse(note!.isWarning)
        XCTAssertTrue(note!.text.contains("Holland2Stay"), note!.text)
        XCTAssertFalse(note!.text.contains("Xior"), "Xior 不受影响，不该被列进生效名单")
    }

    /// source key 大小写不该改变判定 —— 后端存的是小写，但用户数据来路不一。
    func test_selected_sources_are_matched_case_insensitively() {
        let note = PlatformScope.note(appliesTo: h2sOnly,
                                      selectedSources: ["Holland2Stay"])
        XCTAssertNil(note, "大小写不同就当成不支持，会误报「这条过滤没有效果」")
    }

    /// 用平台**显示名**而不是 source key —— 界面上从没出现过 "holland2stay"。
    func test_note_uses_display_names_not_source_keys() {
        let note = PlatformScope.note(appliesTo: ["studentexperience"],
                                      selectedSources: [])
        XCTAssertTrue(note!.text.contains("Student Experience"), note!.text)
        XCTAssertFalse(note!.text.contains("studentexperience"), note!.text)
    }

    // MARK: - sentenceList

    func test_sentence_list_reads_as_english() {
        XCTAssertEqual(PlatformScope.sentenceList([]), "")
        XCTAssertEqual(PlatformScope.sentenceList(["A"]), "A")
        XCTAssertEqual(PlatformScope.sentenceList(["A", "B"]), "A and B")
        XCTAssertEqual(PlatformScope.sentenceList(["A", "B", "C"]), "A, B, and C")
        XCTAssertEqual(PlatformScope.sentenceList(["A", "B", "C", "D"]), "A, B, C, and D")
    }

    /// 两项时用 "and" 而不是逗号 —— "A, B" 读起来像后面还漏了词。
    func test_two_items_are_joined_with_and_not_a_comma() {
        XCTAssertFalse(PlatformScope.sentenceList(["A", "B"]).contains(","))
    }
}
