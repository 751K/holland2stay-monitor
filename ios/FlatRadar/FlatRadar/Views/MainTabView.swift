import SwiftUI

struct MainTabView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(NotificationsStore.self) private var notifStore

    var body: some View {
        TabView {
            DashboardView()
                .tabItem {
                    Label("Dashboard", systemImage: "chart.bar.fill")
                }

            if auth.role == .user || auth.role == .admin {
                ListingsView()
                    .tabItem {
                        Label("Listings", systemImage: "list.bullet")
                    }

                NotificationsView()
                    .tabItem {
                        Label("Notifications", systemImage: "bell.fill")
                    }
                    .badge(notifStore.unreadCount)
            }

            SettingsView()
                .tabItem {
                    Label("Settings", systemImage: "gear")
                }
        }
    }
}
