import Foundation
import SwiftUI
import UIKit
import UserNotifications

/// APNs 设备注册状态机。
///
/// 生命周期
/// --------
/// 1. App 启动：``FlatRadarApp.task`` 调 ``setup()``——挂 PushDelegate 钩子，
///    等待登录完成
/// 2. 登录成功：``AuthStore.login`` 触发 ``requestPermissionAndRegister()``
///    → 系统弹通知权限框 → 同意后向 APNs 注册 → 拿到 token
/// 3. PushDelegate 把 token 回传 ``handleDeviceToken(_:)``
/// 4. token 通过 ``APIClient.registerDevice`` 上报后端
/// 5. 登出：``logout()`` 删除后端绑定 + 解除 APNs 注册
///
/// 环境切换
/// --------
/// Xcode 直接 Run（DEBUG）拿到的 token 只对 sandbox 端点有效；
/// TestFlight / App Store（RELEASE）拿到的 token 对 production 有效。
/// ``Self.currentEnv`` 通过 ``#if DEBUG`` 自动切换。
@MainActor
@Observable
final class PushStore {

    enum PermissionStatus: Sendable {
        case notDetermined, denied, authorized, provisional, ephemeral
    }

    var permissionStatus: PermissionStatus = .notDetermined
    var lastToken: String?
    var lastError: String?
    var registeredDeviceId: Int?

    private let client = APIClient.shared
    private var hasInstalledDelegate = false

    /// DEBUG / RELEASE → "sandbox" / "production"。
    /// 与 ``FlatRadar.entitlements`` 的 ``aps-environment`` 互相对应；
    /// 也是 ``/api/v1/devices/register`` 的 ``env`` 字段值。
    static var currentEnv: String {
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }

    static var currentModel: String {
        var s = utsname()
        uname(&s)
        let mirror = Mirror(reflecting: s.machine)
        return mirror.children
            .compactMap { ($0.value as? Int8).flatMap { $0 == 0 ? nil : UInt8(bitPattern: $0) } }
            .reduce(into: "") { $0.append(Character(UnicodeScalar($1))) }
    }

    static var currentBundleId: String {
        Bundle.main.bundleIdentifier ?? ""
    }

    // MARK: - Setup

    /// App 启动时调一次：挂回调，让 PushDelegate 把 token 转给我们。
    func setup() {
        guard !hasInstalledDelegate else { return }
        hasInstalledDelegate = true
        PushDelegate.shared.onDeviceToken = { [weak self] data in
            Task { @MainActor in
                await self?.handleDeviceToken(data)
            }
        }
        PushDelegate.shared.onRegistrationError = { [weak self] err in
            Task { @MainActor in
                self?.lastError = err.localizedDescription
                print("[PushStore] registration error: \(err)")
            }
        }
        Task { await refreshPermissionStatus() }
    }

    // MARK: - Permission + register

    /// 登录成功后调：弹通知权限框 + APNs 注册。
    /// guest 角色不应调（没 token 调不通 ``/devices/register``）。
    func requestPermissionAndRegister() async {
        let center = UNUserNotificationCenter.current()
        do {
            let granted = try await center.requestAuthorization(
                options: [.alert, .badge, .sound])
            print("[PushStore] requestAuthorization granted=\(granted)")
        } catch {
            lastError = error.localizedDescription
            print("[PushStore] requestAuthorization error: \(error)")
            return
        }
        await refreshPermissionStatus()
        guard permissionStatus == .authorized
            || permissionStatus == .provisional
            || permissionStatus == .ephemeral else {
            print("[PushStore] permission not granted, skip APNs register")
            return
        }
        // 触发 APNs 注册；token 异步回到 PushDelegate.didRegister
        UIApplication.shared.registerForRemoteNotifications()
    }

    private func refreshPermissionStatus() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        permissionStatus = Self.map(settings.authorizationStatus)
    }

    private static func map(_ s: UNAuthorizationStatus) -> PermissionStatus {
        switch s {
        case .notDetermined: return .notDetermined
        case .denied:        return .denied
        case .authorized:    return .authorized
        case .provisional:   return .provisional
        case .ephemeral:     return .ephemeral
        @unknown default:    return .notDetermined
        }
    }

    // MARK: - Device token → backend

    /// PushDelegate 转发的 device token，写库 + 上报。
    func handleDeviceToken(_ data: Data) async {
        let hex = data.map { String(format: "%02x", $0) }.joined()
        print("[PushStore] APNs token hex \(hex.prefix(12))… (\(hex.count) chars)")
        lastToken = hex

        do {
            let resp = try await client.registerDevice(
                token: hex,
                env: Self.currentEnv,
                model: Self.currentModel,
                bundleId: Self.currentBundleId)
            registeredDeviceId = resp.deviceId
            lastError = nil
            print("[PushStore] backend registered device_id=\(resp.deviceId) env=\(resp.env)")
        } catch {
            lastError = error.localizedDescription
            print("[PushStore] backend registerDevice failed: \(error)")
        }
    }

    // MARK: - Logout

    /// 登出时删除当前会话的设备绑定；APNs token 本身保留（同设备重登可复用）。
    func logout() async {
        if let id = registeredDeviceId {
            _ = try? await client.deleteDevice(id: id)
        }
        registeredDeviceId = nil
        lastToken = nil
        lastError = nil
    }
}
