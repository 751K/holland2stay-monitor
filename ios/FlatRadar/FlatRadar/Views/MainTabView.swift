import SwiftUI

/// 给 NotificationsView 的 tab item 单独挂红点，**故意**抽成 ViewModifier：
/// 让对 `NotificationsStore.unreadCount` 的观察只发生在这个小 modifier 的 body 里。
///
/// 之前 MainTabView 自己 `@Environment(NotificationsStore.self)` + 在 body 里读
/// `notifStore.unreadCount`，每次 SSE 推一批通知导致 unreadCount 跳，整个 MainTabView.body
/// 就会重跑——TabView 也跟着重跑——DashboardView 内的 `.refreshable` / `.task` 就被
/// SwiftUI cancel，正在飞的 URLSession 请求统统抛 `NSURLErrorCancelled (-999)`，UI
/// 看到的就是"连接失败"。
///
/// 抽到这里后，badge 变化只让这个 modifier 的 wrapper view 重跑，MainTabView 不受影响。
private struct AlertsTabBadge: ViewModifier {
    @Environment(NotificationsStore.self) private var notifStore
    func body(content: Content) -> some View {
        content.badge(notifStore.unreadCount)
    }
}

struct MainTabView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(NavigationCoordinator.self) private var coord
    @Environment(\.horizontalSizeClass) private var hSizeClass

    private var tab: Binding<AppTab> {
        Binding(get: { coord.selectedTab }, set: { coord.selectedTab = $0 })
    }

    var body: some View {
        ZStack {
            if hSizeClass == .regular {
                wideTabView
            } else {
                compactTabView
            }
            keyboardShortcuts
        }
        .toolbarBackground(.ultraThinMaterial, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .onChange(of: coord.selectedTab) { _, new in
            if hSizeClass == .compact, new == .listings {
                coord.selectedTab = .browse
                coord.selectedBrowseMode = .list
            }
        }
    }

    // MARK: - iPhone: 4 tabs, Browse 内含 segmented picker

    private var compactTabView: some View {
        TabView(selection: tab) {
            DashboardView()
                .tabItem { Label("Dashboard", systemImage: "chart.bar.fill") }
                .tag(AppTab.dashboard)

            BrowseView()
                .tabItem { Label("Browse", systemImage: "square.grid.2x2.fill") }
                .tag(AppTab.browse)

            if auth.role == .user || auth.role == .admin {
                NotificationsView()
                    .tabItem { Label("Alerts", systemImage: "bell.fill") }
                    .modifier(AlertsTabBadge())
                    .tag(AppTab.notifications)
            }

            SettingsView()
                .tabItem { Label("Settings", systemImage: "gear") }
                .tag(AppTab.settings)
        }
    }

    // MARK: - iPad: 6 tabs，List/Map/Calendar 直接展开

    private var wideTabView: some View {
        TabView(selection: tab) {
            DashboardView()
                .tabItem { Label("Dashboard", systemImage: "chart.bar.fill") }
                .tag(AppTab.dashboard)

            listingsTab
                .tabItem { Label("Listings", systemImage: "list.bullet") }
                .tag(AppTab.listings)

            mapTab
                .tabItem { Label("Map", systemImage: "map.fill") }
                .tag(AppTab.map)

            calendarTab
                .tabItem { Label("Calendar", systemImage: "calendar") }
                .tag(AppTab.calendar)

            if auth.role == .user || auth.role == .admin {
                NotificationsView()
                    .tabItem { Label("Alerts", systemImage: "bell.fill") }
                    .modifier(AlertsTabBadge())
                    .tag(AppTab.notifications)
            }

            SettingsView()
                .tabItem { Label("Settings", systemImage: "gear") }
                .tag(AppTab.settings)
        }
    }

    // MARK: - iPad tab content

    private var listingsTab: some View {
        NavigationStack(path: Binding(
            get: { coord.listingsPath },
            set: { coord.listingsPath = $0 }
        )) {
            ListingsView()
                .navigationDestination(for: ListingRoute.self) { route in
                    ListingDetailView(route: route)
                }
        }
    }

    private var mapTab: some View {
        MapView()
    }

    private var calendarTab: some View {
        CalendarView()
    }

    // MARK: - Keyboard shortcuts

    private var keyboardShortcuts: some View {
        HStack(spacing: 0) {
            Button("") { coord.selectedTab = .dashboard }
                .keyboardShortcut("1", modifiers: .command)
            if hSizeClass == .regular {
                Button("") { coord.selectedTab = .listings }
                    .keyboardShortcut("2", modifiers: .command)
                Button("") { coord.selectedTab = .map }
                    .keyboardShortcut("3", modifiers: .command)
                Button("") { coord.selectedTab = .calendar }
                    .keyboardShortcut("4", modifiers: .command)
                Button("") { coord.selectedTab = .notifications }
                    .keyboardShortcut("5", modifiers: .command)
                Button("") { coord.selectedTab = .settings }
                    .keyboardShortcut("6", modifiers: .command)
            } else {
                Button("") { coord.selectedTab = .browse }
                    .keyboardShortcut("2", modifiers: .command)
                Button("") { coord.selectedTab = .notifications }
                    .keyboardShortcut("3", modifiers: .command)
                Button("") { coord.selectedTab = .settings }
                    .keyboardShortcut("4", modifiers: .command)
            }
        }
        .hidden()
        .frame(width: 0, height: 0)
    }
}
