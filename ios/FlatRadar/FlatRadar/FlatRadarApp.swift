import SwiftUI

@main
struct FlatRadarApp: App {
    // 监听 App 前后台切换；用于 SSE 在后台主动关、回前台重连。
    @Environment(\.scenePhase) private var scenePhase

    // PushDelegate 桥接：SwiftUI 没有原生 APNs token 钩子，必须挂一个
    // UIApplicationDelegate。@UIApplicationDelegateAdaptor 把它注入到
    // App 生命周期；PushDelegate.init() 会把 self 写进 .shared 供 PushStore 拿。
    @UIApplicationDelegateAdaptor(PushDelegate.self) private var pushDelegate

    @State private var authStore = AuthStore()
    @State private var dashboardStore = DashboardStore()
    @State private var listingsStore = ListingsStore()
    @State private var notificationsStore = NotificationsStore()
    @State private var mapStore = MapStore()
    @State private var pushStore = PushStore()
    @State private var coordinator = NavigationCoordinator()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(authStore)
                .environment(dashboardStore)
                .environment(listingsStore)
                .environment(notificationsStore)
                .environment(mapStore)
                .environment(pushStore)
                .environment(coordinator)
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
                // Deep link 入口 1：用户点击 push 通知 →
                // PushDelegate 已 post 这个事件
                .onReceive(NotificationCenter.default.publisher(
                    for: .flatRadarOpenListing)) { note in
                    guard let id = note.userInfo?["listing_id"] as? String else { return }
                    coordinator.openListing(id: id)
                }
                // Deep link 入口 2：h2smonitor://listing/<id>（邮件/iMessage 点）
                .onOpenURL { url in
                    handleURL(url)
                }
                // SSE 实时通知：登录 + 前台时连，登出 / 后台时断
                .onChange(of: scenePhase) { _, newPhase in
                    syncStreamState(scenePhase: newPhase)
                }
                .onChange(of: authStore.isAuthenticated) { _, _ in
                    syncStreamState(scenePhase: scenePhase)
                }
        }
    }

    /// 决定 SSE 是否应保持连接：authenticated && non-guest && foreground active。
    /// 其它情况主动断开，避免后台时网络心跳浪费电。
    private func syncStreamState(scenePhase: ScenePhase) {
        let shouldConnect = authStore.isAuthenticated
            && !authStore.isGuest
            && scenePhase == .active
        if shouldConnect {
            notificationsStore.connectStream()
        } else {
            notificationsStore.disconnectStream()
        }
    }

    /// 解析 ``h2smonitor://listing/<id>``。
    /// 其它 host 暂时忽略（将来可扩展 /map、/notifications 等）。
    private func handleURL(_ url: URL) {
        guard url.scheme == "h2smonitor" else { return }
        switch url.host {
        case "listing":
            let id = url.lastPathComponent
            coordinator.openListing(id: id)
        default:
            print("[FlatRadarApp] unknown deep link host=\(url.host ?? "")")
        }
    }
}
