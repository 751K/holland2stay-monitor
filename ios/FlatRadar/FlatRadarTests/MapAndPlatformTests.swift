import XCTest
@testable import FlatRadar

/// 2026-09-02 那一轮地图改造要守住的东西。
///
/// 三件事，都属于「不会崩、只会悄悄错」的那一类：
///
/// 1. **平台显示名只有一份。** 收拢之前它在七个文件里各写了一遍，最全的认得 3 个
///    平台而后端有 7 个——OurCampus / Magis / Student Experience / Plaza 在界面上
///    一路显示成大写的 source key。
/// 2. **同址的房源必须能分别点到。** 一栋楼的每个单元共用街道地址，坐标完全相同；
///    网格聚类对重合点在任何 cell 大小下都归同一格，于是它们在**任何缩放**下都
///    碰不到。服务端把它们摆成一圈，客户端认 display 坐标。
/// 3. **认不出的状态要有自己的一档。** 归进默认隐藏的 occupied 的话，新平台冒出
///    的新状态会从地图上静默消失。
final class PlatformTests: XCTestCase {

    func test_all_seven_backend_platforms_have_names() {
        // 与 static/app.js 的 SOURCE_LABELS 一一对应
        let expected = [
            "holland2stay": "Holland2Stay",
            "ourdomain": "OurDomain",
            "ourcampus": "OurCampus",
            "xior": "Xior",
            "magis": "Magis",
            "studentexperience": "Student Experience",
            "plaza": "Plaza",
        ]
        for (key, name) in expected {
            XCTAssertEqual(Platform.displayName(key), name, "\(key) 的全名不对")
        }
    }

    func test_all_seven_have_short_names() {
        let expected = ["holland2stay": "H2S", "ourdomain": "OD", "ourcampus": "OC",
                        "xior": "XR", "magis": "MG", "studentexperience": "SE",
                        "plaza": "PZ"]
        for (key, short) in expected {
            XCTAssertEqual(Platform.shortName(key), short, "\(key) 的缩写不对")
        }
    }

    func test_short_names_are_unique() {
        // 两个平台共用一个缩写的话，图表坐标轴上就分不出来了。
        let shorts = Platform.knownKeys.map(Platform.shortName)
        XCTAssertEqual(Set(shorts).count, shorts.count, "缩写有重复：\(shorts)")
    }

    func test_case_and_whitespace_are_tolerated() {
        XCTAssertEqual(Platform.displayName("  OurCampus "), "OurCampus")
        XCTAssertEqual(Platform.shortName("PLAZA"), "PZ")
    }

    func test_unknown_platform_does_not_borrow_another_name() {
        // 把未知 source 显示成 "Holland2Stay" 会让人以为数据是那边来的。
        XCTAssertEqual(Platform.displayName("some_new_site"), "Some New Site")
        XCTAssertNotEqual(Platform.displayName("some_new_site"), "Holland2Stay")
    }

    func test_unknown_short_name_does_not_blow_up_the_badge() {
        // 整段大写会把徽章撑变形——STUDENTEXPERIENCE 就是这么露出来的。
        XCTAssertEqual(Platform.shortName("verylongplatformname"), "VER")
        XCTAssertLessThanOrEqual(Platform.shortName("verylongplatformname").count, 3)
    }

    func test_empty_source_has_a_neutral_fallback() {
        XCTAssertEqual(Platform.displayName(nil), "Platform")
        XCTAssertEqual(Platform.shortName(""), "PLT")
    }

    func test_models_delegate_to_platform() throws {
        // 收拢的意义在于各 model 都真的走这一份。
        let json = """
        {"id":"x","name":"n","status":"Occupied","source":"ourcampus",
         "price_raw":"€1","available_from":"","url":"","city":"c","neighborhood":"",
         "building":"","area":"","address":"a","lat":1.0,"lng":2.0}
        """
        let m = try JSONDecoder().decode(MapListing.self, from: Data(json.utf8))
        XCTAssertEqual(m.sourceShortText, "OC")
        XCTAssertEqual(m.sourceDisplayText, "OurCampus")
    }
}


final class ListingStatusTests: XCTestCase {

    func test_buckets_match_the_backend_judgement() {
        // 判据抄自 app/jinja_filters.py 的 status_capsule
        XCTAssertEqual(ListingStatus.from("Available to book"), .book)
        XCTAssertEqual(ListingStatus.from("Available in lottery"), .lottery)
        XCTAssertEqual(ListingStatus.from("Reserved"), .reserved)
        XCTAssertEqual(ListingStatus.from("In Process"), .reserved)
        XCTAssertEqual(ListingStatus.from("Occupied"), .occupied)
        XCTAssertEqual(ListingStatus.from("Rented out"), .occupied)
        XCTAssertEqual(ListingStatus.from("Not available"), .occupied)
    }

    func test_underscored_form_is_normalized() {
        XCTAssertEqual(ListingStatus.from("available_to_book"), .book)
    }

    func test_reserved_and_occupied_are_different() {
        // 一个可能回来，一个永远不会。地图此前把两者一起丢进 .blue 兜底。
        XCTAssertNotEqual(ListingStatus.reserved, ListingStatus.occupied)
        XCTAssertNotEqual(ListingStatus.reserved.color, ListingStatus.occupied.color)
    }

    func test_unknown_status_is_not_occupied() {
        // 核心断言。归进 occupied 的话，它会跟着 occupied 一起被默认隐藏——
        // 新平台冒出的新状态就从地图上静默消失了。
        XCTAssertEqual(ListingStatus.from("Te huur"), .other)
        XCTAssertEqual(ListingStatus.from(""), .other)
        XCTAssertEqual(ListingStatus.from(nil), .other)
    }

    func test_unknown_status_is_visible_by_default() {
        XCTAssertTrue(ListingStatus.other.isOnByDefault)
    }

    func test_the_two_unrentable_buckets_are_hidden_by_default() {
        // 生产实测：235 条里 Occupied 117、Reserved 48。默认全开等于把能租的
        // 那三成淹掉。
        XCTAssertFalse(ListingStatus.occupied.isOnByDefault)
        XCTAssertFalse(ListingStatus.reserved.isOnByDefault)
        XCTAssertTrue(ListingStatus.book.isOnByDefault)
        XCTAssertTrue(ListingStatus.lottery.isOnByDefault)
    }

    func test_cluster_priority_puts_bookable_first() {
        XCTAssertLessThan(ListingStatus.book.priority, ListingStatus.lottery.priority)
        XCTAssertLessThan(ListingStatus.lottery.priority, ListingStatus.reserved.priority)
        // 认不出的比「已经租出去了」更值得看一眼
        XCTAssertLessThan(ListingStatus.other.priority, ListingStatus.occupied.priority)
    }

    func test_every_bucket_has_a_distinct_color() {
        let colors = ListingStatus.allCases.map(\.color)
        XCTAssertEqual(Set(colors.map(String.init(describing:))).count, colors.count)
    }
}


final class MapListingCoordinateTests: XCTestCase {

    private func decode(_ json: String) throws -> MapListing {
        try JSONDecoder().decode(MapListing.self, from: Data(json.utf8))
    }

    private let base = """
    "id":"x","name":"n","status":"Available to book","source":"holland2stay",
    "price_raw":"€1.647","available_from":"2026-06-06","url":"","city":"Amsterdam",
    "neighborhood":"","building":"","area":"27 m²","address":"a"
    """

    func test_display_coordinate_uses_server_values() throws {
        let m = try decode("{\(base),\"lat\":52.0,\"lng\":4.0," +
                           "\"display_lat\":52.001,\"display_lng\":4.002,\"stack_n\":9}")
        XCTAssertEqual(m.displayCoordinate.latitude, 52.001, accuracy: 1e-9)
        XCTAssertEqual(m.displayCoordinate.longitude, 4.002, accuracy: 1e-9)
        XCTAssertEqual(m.stackCount, 9)
        // 真实坐标仍然保留——「这套房到底在哪」和「图钉画在哪」是两件事。
        XCTAssertEqual(m.coordinate.latitude, 52.0, accuracy: 1e-9)
    }

    func test_missing_display_fields_fall_back_to_real_coords() throws {
        // 服务端还没更新时不能丢掉这个点。
        let m = try decode("{\(base),\"lat\":52.0,\"lng\":4.0}")
        XCTAssertEqual(m.displayCoordinate.latitude, 52.0, accuracy: 1e-9)
        XCTAssertEqual(m.stackCount, 1)
    }

    func test_stack_count_is_never_below_one() throws {
        let m = try decode("{\(base),\"lat\":1.0,\"lng\":2.0,\"stack_n\":0}")
        XCTAssertEqual(m.stackCount, 1)
    }
}


final class MapClusteringTests: XCTestCase {

    private func listing(_ id: String, lat: Double, lng: Double,
                         dLat: Double? = nil, dLng: Double? = nil,
                         stack: Int = 1, status: String = "Available to book") throws -> MapListing {
        var extra = ""
        if let dLat, let dLng {
            extra = ",\"display_lat\":\(dLat),\"display_lng\":\(dLng),\"stack_n\":\(stack)"
        }
        let json = """
        {"id":"\(id)","name":"n","status":"\(status)","source":"holland2stay",
         "price_raw":"€1","available_from":"","url":"","city":"c","neighborhood":"",
         "building":"","area":"","address":"a","lat":\(lat),"lng":\(lng)\(extra)}
        """
        return try JSONDecoder().decode(MapListing.self, from: Data(json.utf8))
    }

    func test_identical_coordinates_stay_one_cluster_without_spreading() throws {
        // 钉住这个**前提**：不散开的话它们在再细的网格里也是一格。
        // 这正是那十套在任何缩放下都点不到的原因。
        let items = try (0..<9).map { try listing("a\($0)", lat: 52.336693, lng: 4.926876) }
        let clusters = MapClustering.cluster(listings: items,
                                             latDelta: 0.0005, lngDelta: 0.0005)
        XCTAssertEqual(clusters.count, 1)
        XCTAssertEqual(clusters.first?.count, 9)
    }

    func test_spread_coordinates_split_when_zoomed_in() throws {
        // 服务端散开之后，放大到楼宇尺度就各是各的点了。
        let items = try (0..<9).map { i -> MapListing in
            let angle = 2.0 * Double.pi * Double(i) / 9.0
            return try listing("a\(i)", lat: 52.336693, lng: 4.926876,
                               dLat: 52.336693 + 0.00018 * sin(angle),
                               dLng: 4.926876 + 0.00029 * cos(angle),
                               stack: 9)
        }
        let clusters = MapClustering.cluster(listings: items,
                                             latDelta: 0.0008, lngDelta: 0.0008)
        XCTAssertGreaterThan(clusters.count, 1, "散开之后仍然聚成一团，等于没散")
    }

    func test_bounding_region_of_identical_points_is_not_zero() throws {
        // 点击簇 → zoomIn(to:) 用的就是这个 region。真实坐标全等时它是 0，
        // 被 minSpan 兜成固定视野 → 点了没反应，簇也永远散不开。
        let items = try (0..<3).map { try listing("a\($0)", lat: 52.0, lng: 4.0,
                                                  dLat: 52.0 + Double($0) * 0.0002,
                                                  dLng: 4.0, stack: 3) }
        let region = ListingCluster(id: "c", coordinate: items[0].displayCoordinate,
                                    listings: items).boundingRegion()
        XCTAssertGreaterThan(region.span.latitudeDelta, 0.0005)
    }

    func test_cluster_ids_are_stable_across_calls() throws {
        let items = try [listing("a", lat: 52.0, lng: 4.0),
                         listing("b", lat: 52.1, lng: 4.1)]
        let first = MapClustering.cluster(listings: items, latDelta: 0.5, lngDelta: 0.5)
        let second = MapClustering.cluster(listings: items.reversed(),
                                           latDelta: 0.5, lngDelta: 0.5)
        XCTAssertEqual(first.map(\.id), second.map(\.id))
    }
}


final class MapFilterTests: XCTestCase {

    func test_price_parses_dutch_thousands_separator() {
        XCTAssertEqual(MapStore.price(from: "€ 1.647"), 1647)
        XCTAssertEqual(MapStore.price(from: "€1,647"), 1647)
        XCTAssertEqual(MapStore.price(from: "€707"), 707)
        XCTAssertNil(MapStore.price(from: "n.v.t."))
    }

    func test_area_parses_decimals() {
        XCTAssertEqual(MapStore.number(from: "26.5 m²"), 26.5)
        XCTAssertEqual(MapStore.number(from: "27 m²"), 27)
        XCTAssertNil(MapStore.number(from: ""))
        XCTAssertNil(MapStore.number(from: "—"))
    }
}


final class SentinelDateTests: XCTestCase {

    func test_sentinel_is_recognised() {
        // H2S 在「入住日未定」时发的是 2050-01-01。
        XCTAssertTrue(ServerTime.isSentinelDate("2050-01-01"))
        XCTAssertTrue(ServerTime.isSentinelDate("2099-12-31"))
    }

    func test_real_dates_are_not_sentinels() {
        XCTAssertFalse(ServerTime.isSentinelDate("2026-07-01"))
        XCTAssertFalse(ServerTime.isSentinelDate("2049-12-31"))
        XCTAssertFalse(ServerTime.isSentinelDate(""))
        XCTAssertFalse(ServerTime.isSentinelDate(nil))
    }

    func test_sentinel_is_not_displayed_as_a_date() {
        // 显示成「2050 年 1 月 1 日」读起来像一个（荒唐的）事实，
        // 而它的意思其实是「不知道」。
        XCTAssertEqual(ServerTime.displayDate("2050-01-01"), "—")
    }

    func test_real_date_still_formats() {
        XCTAssertNotEqual(ServerTime.displayDate("2026-07-01"), "—")
        XCTAssertFalse(ServerTime.displayDate("2026-07-01").isEmpty)
    }

    func test_judgement_matches_the_backend_constant() {
        // models.SENTINEL_AVAILABLE_FROM_YEAR / app.js 同一个数。
        XCTAssertEqual(ServerTime.sentinelAvailableFromYear, 2050)
    }
}


final class NavigationCoordinatorMapTests: XCTestCase {

    func test_valid_ids_are_accepted() {
        XCTAssertTrue(NavigationCoordinator.isValidListingID("victoriapark-226"))
        XCTAssertTrue(NavigationCoordinator.isValidListingID("od_307170"))
    }

    func test_hostile_ids_are_rejected() {
        // deep link 来源不可信。
        XCTAssertFalse(NavigationCoordinator.isValidListingID(""))
        XCTAssertFalse(NavigationCoordinator.isValidListingID("../../etc/passwd"))
        XCTAssertFalse(NavigationCoordinator.isValidListingID("a b"))
        XCTAssertFalse(NavigationCoordinator.isValidListingID(String(repeating: "a", count: 129)))
    }

    @MainActor
    func test_open_map_sets_both_ipad_tab_and_iphone_mode() {
        // .map 这个 tab 只在 iPad 存在；iPhone 上 MainTabView.normalizeSelection
        // 会把它翻译成 .browse + .map 模式。两个都设，两种设备都对。
        let c = NavigationCoordinator()
        c.openMap(focusing: "victoriapark-226")
        XCTAssertEqual(c.selectedTab, .map)
        XCTAssertEqual(c.selectedBrowseMode, .map)
        XCTAssertEqual(c.pendingMapFocusID, "victoriapark-226")
    }

    @MainActor
    func test_open_map_ignores_a_hostile_id() {
        let c = NavigationCoordinator()
        c.openMap(focusing: "../../secret")
        XCTAssertNil(c.pendingMapFocusID)
        XCTAssertEqual(c.selectedTab, .dashboard, "非法 id 不该顺带切走 tab")
    }

    @MainActor
    func test_reset_clears_pending_focus() {
        // 不清的话，下个用户登入时地图会莫名其妙飞到上一个人看过的房源上。
        let c = NavigationCoordinator()
        c.openMap(focusing: "abc")
        c.reset()
        XCTAssertNil(c.pendingMapFocusID)
    }
}
