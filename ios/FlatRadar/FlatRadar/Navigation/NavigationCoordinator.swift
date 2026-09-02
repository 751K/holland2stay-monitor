import Foundation
import SwiftUI

/// Tab 标识——MainTabView 用 ``selection`` 绑定。
///
/// iPhone compact: 4 tabs（Dashboard / Browse / Notifications / Settings），
/// Browse 内用 ``BrowseMode`` segmented picker 切换 List/Map/Calendar。
///
/// iPad regular: 6 tabs（Dashboard / Listings / Map / Calendar / Notifications / Settings），
/// 空间足够，不需要二级 picker。
enum AppTab: String, Hashable, Sendable {
    case dashboard
    case browse       // iPhone only
    case listings     // iPad only
    case map          // iPad only
    case calendar     // iPad only
    case notifications
    case settings
}

/// Browse tab 内的视图模式。
enum BrowseMode: String, Hashable, Sendable, CaseIterable, Identifiable {
    case list
    case map
    case calendar

    var id: String { rawValue }

    var label: String {
        switch self {
        case .list:     return String(localized: "List")
        case .map:      return String(localized: "Map")
        case .calendar: return String(localized: "Calendar")
        }
    }

    var systemImage: String {
        switch self {
        case .list:     return "list.bullet"
        case .map:      return "map.fill"
        case .calendar: return "calendar"
        }
    }
}

/// 程序内导航协调器。
///
/// 为什么需要
/// ----------
/// 1. **推送 deep link**：``PushDelegate`` 收到通知后只能 ``NotificationCenter.post``，
///    没法直接动 SwiftUI 视图。Coordinator 把 listing_id 接收下来，转成 tab
///    切换 + NavigationStack push。
/// 2. **URL Scheme**：``h2smonitor://listing/<id>`` 链接（邮件/iMessage 里点）
///    经 ``onOpenURL`` 也走同一个出口。
///
/// 用法
/// ----
/// - ``MainTabView`` ``$coordinator.selectedTab`` 绑定到 TabView selection
/// - ``ListingsView`` 用 ``$coordinator.listingsPath`` 作为 NavigationStack 的 path
/// - ``ListingsView.navigationDestination(for: ListingRoute.self)`` 负责实际绘制
///
/// 路由 enum (``ListingRoute``) 而不是直接塞 Listing：
/// push 通知只有 id，没有完整 Listing 对象；Detail 视图自己异步加载。
@MainActor
@Observable
final class NavigationCoordinator {
    var selectedTab: AppTab = .dashboard
    var selectedBrowseMode: BrowseMode = .list
    var listingsPath: [ListingRoute] = []

    /// deep link 里的 listing id 是否可信。
    ///
    /// - 非空
    /// - ≤ 128 字符，防止超长 URL 撑爆后端 path
    /// - 只允许字母数字 / `-` / `_` —— 各平台的 listing id 都在这个集合里，
    ///   挡掉路径穿越 / 控制字符 / URL 编码注入
    ///
    /// 抽成一处：``openListing`` 与 ``openMap`` 都要验，两份写法迟早分叉，
    /// 而分叉的那一半就是没人看守的那个入口。
    /// `nonisolated`：纯函数，不碰任何状态。不标的话它会继承 @MainActor，
    /// 想在非主线程（比如同步的单元测试）校验一个 id 都得 await。
    nonisolated static func isValidListingID(_ id: String) -> Bool {
        guard !id.isEmpty, id.count <= 128 else { return false }
        return id.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
    }

    /// 由 deep link / 通知点击调用：切到 List 视图并 push 详情。
    /// 多次连点不重复 push 同一条；切换 tab 顺手清空已有 path。
    func openListing(id: String, titleHint: String? = nil) {
        guard Self.isValidListingID(id) else { return }

        selectedTab = .listings
        selectedBrowseMode = .list
        listingsPath = [.byId(id, titleHint: titleHint)]
    }

    /// 切到地图并聚焦某一套房源。
    ///
    /// 房源详情页的「在地图上查看」和 `h2smonitor://map/<id>` 都走这里。
    /// `.map` 这个 tab 只在 iPad 存在；iPhone 上 ``MainTabView.normalizeSelection``
    /// 会把它翻译成 `.browse` + `.map` 模式，所以两种设备都设同一个值就行。
    ///
    /// 实际的定位由 ``MapStore.focus(on:)`` 完成——那套房可能超出 14 天新鲜度
    /// 窗口、或被用户自己的 listing_filter 排除，此时要走 `/map/locate` 兜底
    /// 并说明是哪一种「看不到」。
    func openMap(focusing id: String) {
        guard Self.isValidListingID(id) else { return }
        pendingMapFocusID = id
        // **必须清空导航栈**。MapView 是 BrowseView 那个 NavigationStack 的
        // 根视图，而用户此刻正站在推上去的房源详情页上——只换根视图的话，详情页
        // 还盖在上面，点「在地图上查看」看起来毫无反应。
        //
        // 而且在 path 非空时换根视图，SwiftUI 的行为是未定义的。
        listingsPath = []
        selectedTab = .map
        selectedBrowseMode = .map
    }

    /// 待聚焦的房源 id。``MapView`` 出现时取走并清空——放在 coordinator 而不是
    /// 直接调 MapStore，是因为地图视图此刻可能还没挂载。
    var pendingMapFocusID: String?

    /// Logout / 401 auto-logout / 删号时清空全部导航状态。
    ///
    /// 为什么必须显式调：NavigationCoordinator 是 @Observable 单例，
    /// 跨 login/logout 一直存活在内存里。如果不重置，下个用户登入时
    /// 会看到上个用户最后停留的 tab + listings 详情页（残留 listingsPath
    /// 里的 ListingRoute），既诡异又可能泄露上一会话的房源 id。
    ///
    /// 由 ``FlatRadarApp`` 监听 ``AuthStore.isAuthenticated`` 切到 false
    /// 时统一调用，覆盖手动 logout、401 自动 logout、deleteAccount 三种路径。
    func reset() {
        selectedTab = .dashboard
        selectedBrowseMode = .list
        listingsPath = []
        pendingMapFocusID = nil
    }
}

/// Listings NavigationStack 的路由对象。
///
/// 两种打开方式：
/// - ``known(Listing)``：列表里点行，已有完整 Listing 数据
/// - ``byId(String)``：从 deep link 来，只有 id，详情页自己 fetch
enum ListingRoute: Hashable, Sendable {
    case known(Listing)
    case byId(String, titleHint: String?)
}
