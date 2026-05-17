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

    /// 由 deep link / 通知点击调用：切到 List 视图并 push 详情。
    /// 多次连点不重复 push 同一条；切换 tab 顺手清空已有 path。
    func openListing(id: String) {
        guard !id.isEmpty else { return }
        selectedTab = .listings
        selectedBrowseMode = .list
        listingsPath = [.byId(id)]
    }

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
    }
}

/// Listings NavigationStack 的路由对象。
///
/// 两种打开方式：
/// - ``known(Listing)``：列表里点行，已有完整 Listing 数据
/// - ``byId(String)``：从 deep link 来，只有 id，详情页自己 fetch
enum ListingRoute: Hashable, Sendable {
    case known(Listing)
    case byId(String)
}
