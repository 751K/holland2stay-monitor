import Foundation

/// 地图视图状态：缓存房源 + 当前选中 + 加载/错误标志。
///
/// 数据加载策略
/// ------------
/// - 进入 Map tab 时 `.task` 调一次 ``fetch()``
/// - 下拉触发 ``refresh()``
/// - 不做无限滚动（地图视图天然适合一次性加载，后端已 LIMIT 2000）
@MainActor
@Observable
final class MapStore {
    var listings: [MapListing] = []
    var uncached: Int = 0
    var isLoading: Bool = false
    var errorMessage: String?
    var lastError: APIError?

    /// 当前选中的房源——MapView 用 `Map(selection:)` 双向绑定，
    /// 选中时弹底部 sheet 卡片。
    var selectedID: String?

    // MARK: - 筛选
    //
    // 地图此前**没有任何筛选**。生产实测：235 条里 Occupied 117、Reserved 48，
    // 七成是租不到的，而列表页有十一个筛选控件。默认关掉那两档，能租的那三成
    // 才看得见。各档默认值见 ``ListingStatus.isOnByDefault``。

    var activeStatuses: Set<ListingStatus> =
        Set(ListingStatus.allCases.filter(\.isOnByDefault))
    var cityFilter: String = ""
    var sourceFilter: String = ""
    /// 空串表示不限。用 String 而不是 Double? 是为了直接绑 TextField，
    /// 免得每次输入中间态（"1", "12", "12."）都要来回转换。
    var maxRentText: String = ""
    var minAreaText: String = ""

    // MARK: - 深链
    //
    // 房源详情 →「在地图上查看」。那一套可能不在这批数据里（超出 14 天窗口、
    // 或被用户自己的 listing_filter 排除），此时走 /map/locate 兜底。

    /// 要聚焦的房源 id；聚焦完成后由 MapView 清空。
    var focusID: String?
    /// 兜底拿到的那一条——不在 ``listings`` 里，单独画一枚标记。
    var focusExtra: MapListing?
    /// 说明为什么它不在图上。nil 表示没有要说的。
    var focusNotice: FocusNotice?

    enum FocusNotice: Equatable, Sendable {
        /// 有坐标，但被新鲜度窗口或筛选排除——位置照样标出来。
        case outOfView
        /// 房源在，但地址还没解析出坐标——地图上确实没有这个点。
        case noCoords
        /// 库里没这个 id，链接多半过期了。
        case notFound

        var text: String {
            switch self {
            case .outOfView:
                return String(localized: "This listing is outside the current map view (past the freshness window or excluded by filters). Its location is marked below.")
            case .noCoords:
                return String(localized: "This listing's address has not been geocoded yet, so it has no position on the map.")
            case .notFound:
                return String(localized: "This listing could not be found; the link may have expired.")
            }
        }

        var systemImage: String {
            switch self {
            case .outOfView, .noCoords: return "exclamationmark.triangle"
            case .notFound:             return "slash.circle"
            }
        }
    }

    private let client = APIClient.shared

    var selected: MapListing? {
        guard let id = selectedID else { return nil }
        if let extra = focusExtra, extra.id == id { return extra }
        return listings.first(where: { $0.id == id })
    }

    // MARK: - 筛选结果

    /// 各档的房源数——筛选栏的 chip 上直接显示，让「七成是租不到的」这件事
    /// 不用点开就看得见。
    var statusCounts: [ListingStatus: Int] {
        var counts: [ListingStatus: Int] = [:]
        for l in listings { counts[l.statusKind, default: 0] += 1 }
        return counts
    }

    var cityOptions: [String] {
        Array(Set(listings.map(\.city).filter { !$0.isEmpty })).sorted()
    }

    var sourceOptions: [String] {
        Array(Set(listings.compactMap { $0.source }.filter { !$0.isEmpty })).sorted()
    }

    /// 通过筛选的房源，外加深链兜底那一条。
    var visibleListings: [MapListing] {
        var out = listings.filter(passes)
        if let extra = focusExtra { out.append(extra) }
        return out
    }

    /// 通过筛选的条数（不含兜底那条——它不属于「当前视图里有几套」）。
    var visibleCount: Int { listings.filter(passes).count }

    private func passes(_ l: MapListing) -> Bool {
        // 深链指定的那一套无条件保留：用户是点着它过来的，被默认筛选
        // （比如「已租出」默认关）挡掉会变成「点了没反应」。
        if let f = focusID, l.id == f { return true }
        guard activeStatuses.contains(l.statusKind) else { return false }
        if !cityFilter.isEmpty && l.city != cityFilter { return false }
        if !sourceFilter.isEmpty && (l.source ?? "") != sourceFilter { return false }
        if let cap = Self.number(from: maxRentText),
           let rent = Self.price(from: l.priceRaw), rent > cap {
            // 价格解析不出来时**保留**：读不出 ≠ 超预算。丢掉才是替上游做判断。
            return false
        }
        if let floor = Self.number(from: minAreaText),
           let area = Self.number(from: l.area), area < floor {
            return false
        }
        return true
    }

    /// 一套都不剩时，说明是被**哪一层**挡下的。
    ///
    /// 只报核查过的事实。此前那张卡片写死一句「多数已出租或预留」——那是猜的：
    /// 若空图其实是城市或租金条件筛出来的，这句话就是错的，而界面上看不出来。
    struct EmptyBreakdown: Equatable, Sendable {
        /// 被关掉的状态档 → 各有几套，按数量降序。
        var byStatus: [(status: ListingStatus, count: Int)]
        /// 状态这一关过了，却被城市 / 平台 / 租金 / 面积挡下的条数。
        var byOtherFilters: Int
        var total: Int

        static func == (a: Self, b: Self) -> Bool {
            a.total == b.total && a.byOtherFilters == b.byOtherFilters
                && a.byStatus.map(\.count) == b.byStatus.map(\.count)
        }
    }

    var emptyBreakdown: EmptyBreakdown {
        var byStatus: [ListingStatus: Int] = [:]
        var other = 0
        for l in listings {
            if !activeStatuses.contains(l.statusKind) {
                byStatus[l.statusKind, default: 0] += 1
            } else {
                other += 1          // 状态放行了，那就是别的条件挡的
            }
        }
        return EmptyBreakdown(
            byStatus: byStatus.sorted { $0.value > $1.value }
                .map { (status: $0.key, count: $0.value) },
            byOtherFilters: other,
            total: listings.count)
    }

    /// 「显示全部」：打开所有状态档**并且**清掉城市/平台/租金/面积。
    ///
    /// 只开状态档是不够的——空图若是被城市或租金筛出来的，那样点下去毫无反应，
    /// 成了一个死按钮。这个名字承诺的是「全部」，就得真的是全部。
    func showEverything() {
        activeStatuses = Set(ListingStatus.allCases)
        cityFilter = ""
        sourceFilter = ""
        maxRentText = ""
        minAreaText = ""
    }

    func resetFilters() {
        activeStatuses = Set(ListingStatus.allCases.filter(\.isOnByDefault))
        cityFilter = ""
        sourceFilter = ""
        maxRentText = ""
        minAreaText = ""
    }

    /// `"€ 1.647"` / `"€1,647"` → 1647。荷兰站点用点做千分位，分隔符一律去掉。
    nonisolated static func price(from raw: String) -> Double? {
        let digits = raw.filter { $0.isNumber }
        return digits.isEmpty ? nil : Double(digits)
    }

    /// `"26.5 m²"` → 26.5。取第一段数字（含小数点）。
    nonisolated static func number(from raw: String) -> Double? {
        var seen = false
        var out = ""
        for ch in raw {
            if ch.isNumber || (ch == "." && !out.isEmpty && !out.hasSuffix(".")) {
                out.append(ch); seen = true
            } else if seen {
                break
            }
        }
        return Double(out)
    }

    func fetch() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let resp = try await client.getMap()
            listings = resp.listings
            uncached = resp.uncached
        } catch {
            lastError = error as? APIError
            errorMessage = error.localizedDescription
            #if DEBUG
            print("[MapStore] fetch error: \(error)")
            #endif
        }
    }

    func refresh() async {
        await fetch()
    }

    /// 深链要求聚焦某一套。先看这批数据里有没有；没有再问服务端为什么。
    func focus(on id: String) async {
        focusID = id
        focusExtra = nil
        focusNotice = nil
        if listings.isEmpty { await fetch() }
        if listings.contains(where: { $0.id == id }) { return }

        do {
            let r = try await client.locateMapListing(id: id)
            if r.ok, let l = r.listing {
                focusExtra = l
                focusNotice = .outOfView
            } else {
                // 三种原因分开报。定位不到就把 focusID 放掉，让地图回到正常视野
                // ——「没找到」不该顺带把地图也卡在一个不会到来的目标上。
                focusNotice = r.parsedReason == .noCoords ? .noCoords : .notFound
                focusID = nil
            }
        } catch {
            focusNotice = .notFound
            focusID = nil
            #if DEBUG
            print("[MapStore] locate error: \(error)")
            #endif
        }
    }

    func clearFocus() {
        focusID = nil
        focusExtra = nil
        focusNotice = nil
    }

    /// 登出时清空——下个用户登入预热的是他自己的 map listings。
    func clear() {
        listings = []
        uncached = 0
        isLoading = false
        errorMessage = nil
        lastError = nil
        selectedID = nil
        resetFilters()
        clearFocus()
    }
}
