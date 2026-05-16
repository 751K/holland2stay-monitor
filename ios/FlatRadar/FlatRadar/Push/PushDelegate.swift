import Foundation
import UIKit
import UserNotifications

/// UIApplicationDelegate + UNUserNotificationCenterDelegate for APNs.
///
/// Why an AppDelegate in a SwiftUI app?
/// ------------------------------------
/// `application(_:didRegisterForRemoteNotificationsWithDeviceToken:)` is the
/// only documented way to receive APNs device tokens; SwiftUI has no native
/// hook for it. We bridge it via `@UIApplicationDelegateAdaptor` in
/// `FlatRadarApp.swift`, then forward the token to `PushStore` (an actor /
/// @Observable type that the rest of the app reads from).
///
/// Foreground presentation
/// -----------------------
/// `willPresent` returns `[.banner, .sound, .badge, .list]` so notifications
/// also appear when the app is foreground — useful for testing on simulator.
///
/// Tap handling
/// ------------
/// Backend payloads include `deep_link` like `h2smonitor://listing/<id>`.
/// We post a `Notification.Name.flatRadarOpenListing` so any view in the
/// SwiftUI tree can subscribe and navigate without coupling to this delegate.
@MainActor
final class PushDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    /// **关键**：iOS 通过 ``@UIApplicationDelegateAdaptor(PushDelegate.self)``
    /// 自己 ``init()`` 一份实例并持有；如果再 ``static let shared = PushDelegate()``
    /// 会产生 **两个独立实例**——iOS 给它的发 didRegister，PushStore 又配置
    /// shared，永远收不到 token。
    ///
    /// 解决：第一个被构造的实例（iOS 那个）通过 ``init()`` 把 self 写进
    /// ``shared``。之后 ``PushDelegate.shared`` 拿到的就是 iOS 在用的实例。
    nonisolated(unsafe) static var shared: PushDelegate!

    override init() {
        super.init()
        Self.shared = self
    }

    // Bridge to PushStore; set by FlatRadarApp on launch.
    var onDeviceToken: ((Data) -> Void)?
    var onRegistrationError: ((Error) -> Void)?

    /// iOS 可能在 ``onDeviceToken`` 还未挂上时（App 刚启动、cached token 重放）
    /// 就调 ``didRegister``。这里保留最新一次的 token，``PushStore.setup()``
    /// 完成回调挂载后会主动 ``flushPendingToken()`` 把缓存的 token 投递出去。
    private(set) var latestDeviceToken: Data?

    func flushPendingToken() {
        guard let data = latestDeviceToken else { return }
        #if DEBUG
        print("[PushDelegate] flushing pending token (\(data.count) bytes) to handler")
        #endif
        onDeviceToken?(data)
    }

    // MARK: - UIApplicationDelegate

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        latestDeviceToken = deviceToken
        #if DEBUG
        print("[PushDelegate] didRegister deviceToken (\(deviceToken.count) bytes), handler=\(onDeviceToken != nil)")
        #endif
        onDeviceToken?(deviceToken)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        #if DEBUG
        print("[PushDelegate] didFailToRegister: \(error)")
        #endif
        onRegistrationError?(error)
    }

    // MARK: - UNUserNotificationCenterDelegate

    /// App in foreground — still show banner + play sound. Without this iOS
    /// suppresses banners for foreground apps which makes test pushes look like
    /// silent failures.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound, .badge, .list])
    }

    /// User tapped a notification — forward listing_id via NotificationCenter.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        // 优先用结构化 listing_id；回退解析 deep_link
        if let listingId = userInfo["listing_id"] as? String, !listingId.isEmpty {
            NotificationCenter.default.post(
                name: .flatRadarOpenListing,
                object: nil,
                userInfo: ["listing_id": listingId])
        } else if let deepLink = userInfo["deep_link"] as? String,
                  let url = URL(string: deepLink),
                  url.scheme == "h2smonitor",
                  url.host == "listing" {
            let id = url.lastPathComponent
            NotificationCenter.default.post(
                name: .flatRadarOpenListing,
                object: nil,
                userInfo: ["listing_id": id])
        }
        completionHandler()
    }
}

// MARK: - Notification name for deep linking

extension Notification.Name {
    /// 推送通知点击后用：``userInfo["listing_id"]`` 是要打开的房源 id。
    static let flatRadarOpenListing = Notification.Name("FlatRadarOpenListing")
}
