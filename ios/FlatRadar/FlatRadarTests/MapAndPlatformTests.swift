import SwiftUI   // Color —— 断言平台调色板时要用
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

    func test_平台颜色与既有界面保持一致() {
        // 前三个是用户已经认熟的取值，收拢调色板时不能顺手改掉。
        XCTAssertEqual(String(describing: Platform.color("holland2stay")),
                       String(describing: Color.blue))
        XCTAssertEqual(String(describing: Platform.color("ourdomain")),
                       String(describing: Color.purple))
        XCTAssertEqual(String(describing: Platform.color("xior")),
                       String(describing: Color.teal))
    }

    func test_后四个平台不再全是蓝色() {
        // 收拢前它们都落在 `default: return .blue`，和 Holland2Stay 撞色，
        // 等于颜色不承载任何信息。
        let h2s = String(describing: Platform.color("holland2stay"))
        for key in ["ourcampus", "magis", "studentexperience", "plaza"] {
            XCTAssertNotEqual(String(describing: Platform.color(key)), h2s,
                              "\(key) 还是和 H2S 同色")
        }
    }

    func test_每个平台的颜色互不相同() {
        // 图表原先用 palette[idx % 3]：三个平台够用，七个之后颜色开始重复，
        // 堆叠条上相邻两段可能同色，图例和条形对不上号。
        let colors = Platform.knownKeys.map { String(describing: Platform.color($0)) }
        XCTAssertEqual(Set(colors).count, colors.count, "有平台共用了颜色：\(colors)")
    }

    func test_颜色按平台稳定_不随传入形式变化() {
        // 图表里传进来的常常已经是缩写（"OC"），必须和 key 取到同一个颜色，
        // 否则同一个平台在条形和图例上会是两种颜色。
        for key in Platform.knownKeys {
            let short = Platform.shortName(key)
            XCTAssertEqual(String(describing: Platform.color(key)),
                           String(describing: Platform.color(short)),
                           "\(key) 用 key 和用缩写取到的颜色不一样")
        }
    }

    func test_未知平台不借用别人的颜色() {
        let unknown = String(describing: Platform.color("brand-new-site"))
        for key in Platform.knownKeys {
            XCTAssertNotEqual(unknown, String(describing: Platform.color(key)))
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

    func test_every_bucket_is_on_by_default() {
        // 一度默认关掉 Reserved / Occupied（生产全量里那两档占七成）。但那是全量
        // 比例：用一个筛选很窄的账号打开地图，9 条里 0 条可订，默认筛完一套不剩，
        // 整张图是空的——读起来是「坏了」，不是「筛选生效了」。
        //
        // 空图的代价比噪音大：噪音看得见、能自己关；空图连该点哪里都不知道。
        for kind in ListingStatus.allCases {
            XCTAssertTrue(kind.isOnByDefault, "\(kind) 默认被关掉了，可能筛出一张空图")
        }
    }

    @MainActor
    func test_default_filter_never_empties_a_non_empty_map() throws {
        // 把上一条的**后果**直接钉住：只要地图上有房源，默认筛选之后就不该是 0。
        let s = MapStore()
        s.listings = try (0..<3).map {
            try JSONDecoder().decode(MapListing.self, from: Data("""
            {"id":"x\($0)","name":"n","status":"Occupied","source":"holland2stay",
             "price_raw":"€1","available_from":"","url":"","city":"c","neighborhood":"",
             "building":"","area":"","address":"a","lat":52.0,"lng":4.0}
            """.utf8))
        }
        s.resetFilters()
        XCTAssertEqual(s.visibleCount, 3, "默认筛选把非空地图筛成了空的")
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

    /// 点击聚合泡之后，**必须真的散得开**。
    ///
    /// 这是一条走完整回路的断言：散开坐标 → boundingRegion → 把那个 span 喂回
    /// cluster()。原先只断言 boundingRegion "不是 0"，而 0.008 和 0.00096 都不是
    /// 0——它放过了 minSpan 把圆环压掉这个 bug，表现是放大到底还是一个泡。
    func test_tapping_a_cluster_actually_splits_it() throws {
        // 九套同址，按服务端的圆环参数散开（半径 0.00012 * 9/6）
        let r = 0.00012 * (9.0 / 6.0)
        let items = try (0..<9).map { i -> MapListing in
            let a = 2.0 * Double.pi * Double(i) / 9.0
            return try listing("a\(i)", lat: 52.336693, lng: 4.926876,
                               dLat: 52.336693 + r * sin(a),
                               dLng: 4.926876 + r * cos(a) / cos(52.336693 * .pi / 180),
                               stack: 9)
        }
        // 远景：聚成一个泡（前提）
        let far = MapClustering.cluster(listings: items, latDelta: 0.5, lngDelta: 0.5)
        XCTAssertEqual(far.count, 1, "前提：远景下它们本来就该是一团")

        // 点击 → zoomIn(to:) 用的就是这个 region
        let region = far[0].boundingRegion()
        let near = MapClustering.cluster(listings: items,
                                         latDelta: region.span.latitudeDelta,
                                         lngDelta: region.span.longitudeDelta)
        XCTAssertGreaterThan(near.count, 1,
            "点了聚合泡还是一团——放大到底也认不出是哪套房")
    }

    func test_min_span_is_tighter_than_the_spread_ring() throws {
        // 直接钉住那个常量的量级：它一旦大于圆环，上面那条回路就断了。
        let items = try (0..<2).map { i in
            try listing("a\(i)", lat: 52.0, lng: 4.0,
                        dLat: 52.0 + Double(i) * 0.00024, dLng: 4.0, stack: 2)
        }
        let span = ListingCluster(id: "c", coordinate: items[0].displayCoordinate,
                                  listings: items).boundingRegion().span.latitudeDelta
        XCTAssertLessThan(span, 0.002, "视野太大，同址那几套挤在一个网格里散不开")
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
    func test_open_map_pops_the_navigation_stack() {
        // MapView 是 BrowseView 那个 NavigationStack 的**根**。用户点「在地图上
        // 查看」时正站在推上去的详情页上；不清 path 的话详情页还盖在上面，
        // 表现是「点了没反应」——真机实测就是这样。
        let c = NavigationCoordinator()
        c.listingsPath = [.byId("victoriapark-226", titleHint: nil)]
        c.openMap(focusing: "victoriapark-226")
        XCTAssertTrue(c.listingsPath.isEmpty, "详情页没弹出，地图被盖住了")
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


/// 空态说明卡的文案是**算出来**的，不是写死的。
///
/// 原先卡片上硬写着「多数已出租或预留」——那是猜的。空图若其实是城市或租金条件
/// 筛出来的，这句话就是错的，而界面上看不出错在哪。同理「Show all」原先只重置
/// 状态档，被别的条件挡空时点下去毫无反应，是个死按钮。
@MainActor
final class MapEmptyBreakdownTests: XCTestCase {

    private func listing(_ id: String, status: String, city: String = "Amsterdam",
                         price: String = "€800") throws -> MapListing {
        let json = """
        {"id":"\(id)","name":"n","status":"\(status)","source":"holland2stay",
         "price_raw":"\(price)","available_from":"","url":"","city":"\(city)",
         "neighborhood":"","building":"","area":"30 m²","address":"a",
         "lat":52.0,"lng":4.0}
        """
        return try JSONDecoder().decode(MapListing.self, from: Data(json.utf8))
    }

    private func store(_ items: [MapListing]) -> MapStore {
        let s = MapStore()
        s.listings = items
        return s
    }

    func test_counts_each_hidden_status_bucket() throws {
        let s = store(try [
            listing("a", status: "Occupied"), listing("b", status: "Occupied"),
            listing("c", status: "Reserved"),
        ])
        // 默认已改成全开，所以这条要**显式**关掉这两档才谈得上"被藏起来"。
        // 原先它靠的是"occupied/reserved 默认关"这个隐含前提——默认一改就红了，
        // 红得对：它测的是 breakdown 的算法，不该顺带依赖默认值。
        s.activeStatuses = [.book, .lottery, .other]
        let b = s.emptyBreakdown
        XCTAssertEqual(b.total, 3)
        XCTAssertEqual(b.byStatus.first?.status, .occupied)
        XCTAssertEqual(b.byStatus.first?.count, 2, "按数量降序，多的排前面")
        XCTAssertEqual(b.byOtherFilters, 0)
    }

    func test_attributes_other_filters_separately() throws {
        // 状态这一关过了，是城市把它挡下的——这时不能说「已出租或预留」。
        let s = store(try [listing("a", status: "Available to book", city: "Utrecht")])
        s.cityFilter = "Amsterdam"
        let b = s.emptyBreakdown
        XCTAssertTrue(b.byStatus.isEmpty, "不该赖到状态头上")
        XCTAssertEqual(b.byOtherFilters, 1)
    }

    func test_show_everything_clears_every_filter() throws {
        // 只开状态档不够：被城市或租金挡空时，那样点下去毫无反应。
        let s = store(try [listing("a", status: "Available to book", city: "Utrecht")])
        s.cityFilter = "Amsterdam"
        s.maxRentText = "100"
        s.minAreaText = "999"
        s.sourceFilter = "xior"
        s.activeStatuses = []
        XCTAssertEqual(s.visibleCount, 0)

        s.showEverything()
        XCTAssertEqual(s.visibleCount, 1, "「显示全部」之后必须真的全都看得见")
    }

    func test_reset_clears_the_non_status_filters() throws {
        // resetFilters 的职责是"回到默认"。默认现在是全开，所以状态那一半和
        // showEverything 暂时同效——但它另有一件独立的事：把城市/平台/租金/面积
        // 一并清掉。钉住这一件，不去钉两者是否恰好相等（那只是当下默认值的巧合）。
        let s = store(try [listing("a", status: "Available to book", city: "Utrecht")])
        s.cityFilter = "Amsterdam"
        s.maxRentText = "100"
        XCTAssertEqual(s.visibleCount, 0)
        s.resetFilters()
        XCTAssertEqual(s.visibleCount, 1)
        XCTAssertTrue(s.cityFilter.isEmpty && s.maxRentText.isEmpty)
    }
}


/// `source` 字段是权威，URL 嗅探只是兜底。
///
/// 真机上每一条 OurCampus 都显示成 "OD"：后端发的 `source` 是 `ourcampus`，
/// 但归一化函数只在第一段里认三个平台，`ourcampus` 落不进去，掉到 URL 那一段
/// 撞上 `securerc.co.uk → ourdomain`——而 OurCampus 和 OurDomain 共用 RentCafe
/// 这个域名。一条猜测盖掉了一条事实。
final class NormalizedSourceKeyTests: XCTestCase {

    private func listing(source: String?, url: String) throws -> Listing {
        let src = source.map { "\"source\": \"\($0)\"," } ?? ""
        let json = """
        {"id":"1","name":"n","status":"s",\(src)
         "features":[],"feature_map":{},"url":"\(url)","city":"c"}
        """
        return try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
    }

    private let rentCafeOurCampus =
        "https://new-ourcampus-amsterdam-diemen-rentcafewebsiteuk.securerc.co.uk/x"
    private let rentCafeOurDomain =
        "https://thisisourdomain.securerc.co.uk/onlineleasing/ourdomain-amsterdam-diemen/x"

    func test_ourcampus_is_not_relabelled_as_ourdomain() throws {
        let l = try listing(source: "ourcampus", url: rentCafeOurCampus)
        XCTAssertEqual(l.normalizedSourceKey, "ourcampus")
        XCTAssertEqual(l.sourceShortText, "OC", "真机上这里显示的是 OD")
    }

    func test_every_backend_platform_survives_the_round_trip() throws {
        // 后端登记的七个平台，一个都不该被 URL 改写。
        for key in Platform.knownKeys {
            let l = try listing(source: key, url: rentCafeOurDomain)
            XCTAssertEqual(l.normalizedSourceKey, key,
                           "\(key) 被 URL 嗅探覆盖了")
        }
    }

    func test_url_sniffing_still_works_when_source_is_missing() throws {
        // 兜底本身要留着：source 缺失时仍然靠 URL 猜。
        XCTAssertEqual(try listing(source: nil, url: rentCafeOurCampus).normalizedSourceKey,
                       "ourcampus")
        XCTAssertEqual(try listing(source: nil, url: rentCafeOurDomain).normalizedSourceKey,
                       "ourdomain")
        XCTAssertEqual(try listing(source: nil, url: "https://holland2stay.com/x")
                        .normalizedSourceKey, "holland2stay")
    }

    func test_shared_rentcafe_domain_is_split_by_subdomain() throws {
        // 光看 securerc.co.uk 分不出两家——必须看子域。
        let a = try listing(source: nil, url: rentCafeOurCampus).normalizedSourceKey
        let b = try listing(source: nil, url: rentCafeOurDomain).normalizedSourceKey
        XCTAssertNotEqual(a, b, "两家共用同一个域名，被混成了一家")
    }

    func test_legacy_short_codes_still_map() throws {
        XCTAssertEqual(try listing(source: "OC", url: "").normalizedSourceKey, "ourcampus")
        XCTAssertEqual(try listing(source: "h2s", url: "").normalizedSourceKey, "holland2stay")
    }
}


/// 地点行的去重。
///
/// OurCampus 的 city 和 building 都是 "OurCampus Amsterdam Diemen"，而房源名是
/// "OurCampus Diemen #3250"——照原样并排会把同一件事念三遍。真机截图上就是这样。
///
/// 这段逻辑一度在地图弹卡和日历行各写一份，地图那边修好了、日历那边没有。
/// 现在只有 PlaceSummary 一份。
final class PlaceSummaryTests: XCTestCase {

    func test_ourcampus_只留下真正新增的信息() {
        // 标题 "OurCampus Diemen #3250"，city 和 building 都是
        // "OurCampus Amsterdam Diemen"。整串比较放行（两串互不包含）——那是
        // 第一版的漏洞，测试当场抓到了。按词看：OurCampus、Diemen 标题里已有，
        // 真正新的只有 Amsterdam，那才是这一行值得占位置的内容。
        XCTAssertEqual(
            PlaceSummary.text(name: "OurCampus Diemen #3250",
                              parts: ["OurCampus Amsterdam Diemen",
                                      "OurCampus Amsterdam Diemen"]),
            "Amsterdam")
    }

    func test_门牌号不参与判重() {
        // 数字和单字符（#、1-639）不承载地点信息，拿它们判重只会误伤。
        XCTAssertEqual(
            PlaceSummary.text(name: "Kastanjelaan 1-639", parts: ["Eindhoven 639"]),
            "Eindhoven")
    }

    func test_去掉重复项() {
        XCTAssertEqual(PlaceSummary.text(name: "X", parts: ["Amsterdam", "Amsterdam"]),
                       "Amsterdam")
    }

    func test_去掉空白项() {
        XCTAssertEqual(PlaceSummary.text(name: "X", parts: ["", "  ", "Utrecht"]),
                       "Utrecht")
    }

    func test_保留标题里没有的部分() {
        XCTAssertEqual(
            PlaceSummary.text(name: "Kastanjelaan 1-639",
                              parts: ["Centrum", "Eindhoven"]),
            "Centrum · Eindhoven")
    }

    func test_大小写不影响判重() {
        XCTAssertNil(PlaceSummary.text(name: "AMSTERDAM Naritaweg 155C",
                                       parts: ["amsterdam naritaweg"]))
    }

    func test_只剩重复词时不显示() {
        XCTAssertNil(PlaceSummary.text(name: "Amsterdam Diemen", parts: ["Diemen Amsterdam"]))
    }

    func test_全部被过滤掉时返回_nil_而不是空串() {
        // 返回 "" 的话调用方会画一个空的 Text，留下一道莫名的空行。
        XCTAssertNil(PlaceSummary.text(name: "X", parts: ["", "x"]))
    }
}
