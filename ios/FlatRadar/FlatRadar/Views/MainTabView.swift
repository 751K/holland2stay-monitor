import SwiftUI

struct MainTabView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(NotificationsStore.self) private var notifStore
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
                    .tabItem { Label("Notifications", systemImage: "bell.fill") }
                    .badge(notifStore.unreadCount)
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
                    .tabItem { Label("Notifications", systemImage: "bell.fill") }
                    .badge(notifStore.unreadCount)
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
