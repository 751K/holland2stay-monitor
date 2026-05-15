import SwiftUI

@main
struct FlatRadarApp: App {
    // PushDelegate 桥接：SwiftUI 没有原生 APNs token 钩子，必须挂一个
    // UIApplicationDelegate。@UIApplicationDelegateAdaptor 把它注入到
    // App 生命周期；PushDelegate.shared 通过回调把 token 转给 PushStore。
    @UIApplicationDelegateAdaptor(PushDelegate.self) private var pushDelegate

    @State private var authStore = AuthStore()
    @State private var dashboardStore = DashboardStore()
    @State private var listingsStore = ListingsStore()
    @State private var notificationsStore = NotificationsStore()
    @State private var pushStore = PushStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(authStore)
                .environment(dashboardStore)
                .environment(listingsStore)
                .environment(notificationsStore)
                .environment(pushStore)
                .task {
                    // 1. 把 PushStore 与 PushDelegate 桥接好（一次性）
                    pushStore.setup()
                    // 2. 恢复 token 会话
                    await authStore.restoreSession()
                    // 3. 若已登录（非 guest），自动尝试注册 APNs
                    if authStore.isAuthenticated, !authStore.isGuest {
                        await pushStore.requestPermissionAndRegister()
                    }
                }
        }
    }
}
