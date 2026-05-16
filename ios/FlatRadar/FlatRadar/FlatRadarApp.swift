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
    @State private var calendarStore = CalendarStore()
    @State private var meFilterStore = MeFilterStore()
    @State private var adminStore = AdminStore()
    @State private var pushStore = PushStore()
    @State private var coffeeStore = CoffeeStore()
    @State private var coordinator = NavigationCoordinator()

    /// User-overridden color scheme. "system" = follow OS.
    @AppStorage("color_scheme") private var colorScheme: String = "system"

    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(resolvedColorScheme)
                .environment(authStore)
                .environment(dashboardStore)
                .environment(listingsStore)
                .environment(notificationsStore)
                .environment(mapStore)
                .environment(calendarStore)
                .environment(meFilterStore)
                .environment(adminStore)
                .environment(pushStore)
                .environment(coffeeStore)
                .environment(coordinator)
                .task {
                    // 1. 全局 401/403 监听 → 自动登出
                    authStore.observeAuthFailures()
                    // 2. 把 PushStore 与 PushDelegate 桥接好（一次性）
                    pushStore.setup()
                    // 3. 恢复 token 会话
                    await authStore.restoreSession()
                    // 4. 若已登录（非 guest），自动尝试注册 APNs
                    if authStore.isAuthenticated, !authStore.isGuest {
                        await pushStore.requestPermissionAndRegister()
                    }
                    // 5. StoreKit 2 交易监听 + 加载咖啡产品
                    coffeeStore.listenForTransactions()
                    await coffeeStore.loadProducts()
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

    /// 把 UserDefaults 的字符串映射到 SwiftUI ColorScheme?。
    /// "system" → nil（跟随系统），"light"/"dark" → 对应值。
    private var resolvedColorScheme: ColorScheme? {
        switch colorScheme {
        case "light": return .light
        case "dark":  return .dark
        default:      return nil
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
            #if DEBUG
            print("[FlatRadarApp] unknown deep link host=\(url.host ?? "")")
            #endif
        }
    }
}
