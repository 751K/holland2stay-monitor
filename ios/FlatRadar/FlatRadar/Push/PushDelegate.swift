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

    static let shared = PushDelegate()

    // Bridge to PushStore; set by FlatRadarApp on launch.
    var onDeviceToken: ((Data) -> Void)?
    var onRegistrationError: ((Error) -> Void)?

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
        print("[PushDelegate] didRegister deviceToken (\(deviceToken.count) bytes)")
        onDeviceToken?(deviceToken)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("[PushDelegate] didFailToRegister: \(error)")
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
