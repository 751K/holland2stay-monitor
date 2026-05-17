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

    private var tab: Binding<AppTab> {
        Binding(get: { coord.selectedTab }, set: { coord.selectedTab = $0 })
    }

    var body: some View {
        GeometryReader { proxy in
            let useCompactTabs = shouldUseCompactTabs(width: proxy.size.width)

            ZStack {
                if useCompactTabs {
                    compactTabView
                } else {
                    wideTabView
                }
                keyboardShortcuts(compact: useCompactTabs)
            }
            .toolbarBackground(.ultraThinMaterial, for: .tabBar)
            .toolbarBackground(.visible, for: .tabBar)
            .onChange(of: coord.selectedTab) { _, new in
                normalizeSelection(new, compact: useCompactTabs)
            }
            .onChange(of: useCompactTabs) { _, new in
                normalizeSelection(coord.selectedTab, compact: new)
            }
            .onAppear {
                normalizeSelection(coord.selectedTab, compact: useCompactTabs)
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
        NavigationStack {
            MapView()
        }
    }

    private var calendarTab: some View {
        NavigationStack {
            CalendarView()
        }
    }

    // MARK: - Keyboard shortcuts

    private func shouldUseCompactTabs(width: CGFloat) -> Bool {
        // iPad Stage Manager / Split View can keep a regular size class even
        // when the window is too narrow for six top tabs. Switch to Browse
        // once the actual content width gets tight.
        width < 920
    }

    private func keyboardShortcuts(compact: Bool) -> some View {
        HStack(spacing: 0) {
            shortcutButton("Switch to Dashboard", key: "1") { coord.selectedTab = .dashboard }
            if !compact {
                shortcutButton("Switch to Listings", key: "2") { coord.selectedTab = .listings }
                shortcutButton("Switch to Map", key: "3") { coord.selectedTab = .map }
                shortcutButton("Switch to Calendar", key: "4") { coord.selectedTab = .calendar }
                shortcutButton("Switch to Alerts", key: "5") { coord.selectedTab = .notifications }
                shortcutButton("Switch to Settings", key: "6") { coord.selectedTab = .settings }
            } else {
                shortcutButton("Switch to Browse", key: "2") { coord.selectedTab = .browse }
                shortcutButton("Switch to Alerts", key: "3") { coord.selectedTab = .notifications }
                shortcutButton("Switch to Settings", key: "4") { coord.selectedTab = .settings }
            }
        }
        .hidden()
        .frame(width: 0, height: 0)
        // .hidden() 已经把 HStack 视觉隐藏；同时对 VoiceOver 显式跳过，
        // 否则 VO 仍能聚焦到这些"空标签按钮"——既然有 accessibilityLabel
        // 防御性也好，再用 accessibilityHidden 把整组从 a11y 树移除最干净。
        // 这些 button 只是 keyboardShortcut 接收器，硬件键盘用户走快捷键，
        // VoiceOver 用户走真实的 tab bar，重复曝光反而干扰。
        .accessibilityHidden(true)
    }

    /// 把命令键 shortcut 包成有 accessibilityLabel 的按钮 —— 即便整体走
    /// accessibilityHidden 屏蔽，单元素仍带 label 是好习惯：
    /// 一是日后想曝光时只需删 .accessibilityHidden(true)；二是某些辅助工具
    /// （非 VoiceOver）会扫 label 内容。
    private func shortcutButton(
        _ label: String,
        key: KeyEquivalent,
        action: @escaping () -> Void
    ) -> some View {
        Button("", action: action)
            .keyboardShortcut(key, modifiers: .command)
            .accessibilityLabel(label)
    }

    private func normalizeSelection(_ tab: AppTab, compact: Bool) {
        if compact {
            switch tab {
            case .listings:
                coord.selectedTab = .browse
                coord.selectedBrowseMode = .list
            case .map:
                coord.selectedTab = .browse
                coord.selectedBrowseMode = .map
            case .calendar:
                coord.selectedTab = .browse
                coord.selectedBrowseMode = .calendar
            default:
                break
            }
        } else if tab == .browse {
            switch coord.selectedBrowseMode {
            case .list:
                coord.selectedTab = .listings
            case .map:
                coord.selectedTab = .map
            case .calendar:
                coord.selectedTab = .calendar
            }
        }
    }
}
