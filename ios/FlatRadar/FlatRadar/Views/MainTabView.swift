import SwiftUI

struct MainTabView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(NotificationsStore.self) private var notifStore
    @Environment(NavigationCoordinator.self) private var coord

    var body: some View {
        // @Bindable 把 @Observable 拆成可绑定的属性，便于 TabView selection 双向同步。
        @Bindable var coord = coord

        TabView(selection: $coord.selectedTab) {
            DashboardView()
                .tabItem {
                    Label("Dashboard", systemImage: "chart.bar.fill")
                }
                .tag(AppTab.dashboard)

            if auth.role == .user || auth.role == .admin {
                ListingsView()
                    .tabItem {
                        Label("Listings", systemImage: "list.bullet")
                    }
                    .tag(AppTab.listings)

                MapView()
                    .tabItem {
                        Label("Map", systemImage: "map.fill")
                    }
                    .tag(AppTab.map)

                NotificationsView()
                    .tabItem {
                        Label("Notifications", systemImage: "bell.fill")
                    }
                    .badge(notifStore.unreadCount)
                    .tag(AppTab.notifications)
            }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gear")
                }
                .tag(AppTab.settings)
        }
    }
}
