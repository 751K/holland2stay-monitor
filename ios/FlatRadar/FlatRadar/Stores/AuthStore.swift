import Foundation
import SwiftUI

enum Role: String, Sendable {
    case guest
    case user
    case admin
}

enum DeviceName {
    static var current: String {
#if os(iOS)
        UIDevice.current.name
#else
        Host.current().name ?? "Mac"
#endif
    }
}

@MainActor
@Observable
final class AuthStore {
    var isAuthenticated = false
    var role: Role = .guest
    var userInfo: UserInfo?
    var isLoading = false
    var errorMessage: String?
    var lastError: APIError?

    /// 登录成功后待保存的 Face ID 凭据——由 LoginView 设置，ContentView 弹出 alert。
    /// LoginView 会在登录成功后立即被 ContentView 替换掉，alert 放 LoginView
    /// 层级会来不及弹出。提到这里让 ContentView 处理。
    var pendingBiometricCredential: (username: String, password: String, role: String)?

    private let client = APIClient.shared
    private var server: String {
        UserDefaults.standard.string(forKey: "server_url") ?? APIClient.defaultServerHost
    }

    /// Listen for global auth failures from any API call and auto-logout.
    func observeAuthFailures() {
        NotificationCenter.default.addObserver(
            forName: APIClient.authFailedNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.isAuthenticated, !self.isGuest else { return }
                await self.logout()
            }
        }
    }

    // MARK: - Restore Session

    func restoreSession() async {
        // 截图测试：要求 LoginView 时跳过恢复，否则 keychain 残留的 token
        // 会让 ContentView 直接展示 Dashboard。生产 build 永远不会进这个分支。
        if CommandLine.arguments.contains("UI_TEST_SHOW_LOGIN") {
            return
        }

        let savedToken = KeychainManager.load(server: server)
            ?? UserDefaults.standard.string(forKey: "auth_token")
        guard let token = savedToken else { return }

        client.setToken(token)

        // Verify token is still valid
        do {
            let me = try await client.getMe()
            applyMe(me)
        } catch {
            // Token expired or revoked — clear and stay on login screen
            KeychainManager.delete(server: server)
            UserDefaults.standard.removeObject(forKey: "auth_token")
            client.setToken(nil)
        }
    }

    // MARK: - Login

    func loginAsAdmin(password: String, ttlDays: Int = 90) async {
        await login(username: "__admin__", password: password, ttlDays: ttlDays)
    }

    func loginAsUser(name: String, password: String, ttlDays: Int = 90) async {
        await login(username: name, password: password, ttlDays: ttlDays)
    }

    private func login(username: String, password: String, ttlDays: Int) async {
        isLoading = true
        errorMessage = nil
        do {
            let device = DeviceName.current
            let resp = try await client.login(
                username: username, password: password,
                deviceName: device, ttlDays: ttlDays)
            client.setToken(resp.token)
            do {
                try KeychainManager.save(token: resp.token, server: server)
            } catch {
                #if DEBUG
                print("[AuthStore] Keychain save failed, falling back to UserDefaults")
                #endif
                UserDefaults.standard.set(resp.token, forKey: "auth_token")
            }

            let me = try await client.getMe()
            applyMe(me)
        } catch {
            #if DEBUG
            print("[AuthStore] login error: \(error)")
            #endif
            recordError(error)
        }
        isLoading = false
    }

    // MARK: - Register

    func register(name: String, password: String, ttlDays: Int = 90) async {
        isLoading = true
        errorMessage = nil
        do {
            let device = DeviceName.current
            let resp = try await client.register(
                username: name, password: password,
                deviceName: device, ttlDays: ttlDays)
            client.setToken(resp.token)
            do {
                try KeychainManager.save(token: resp.token, server: server)
            } catch {
                #if DEBUG
                print("[AuthStore] Keychain save failed, falling back to UserDefaults")
                #endif
                UserDefaults.standard.set(resp.token, forKey: "auth_token")
            }
            let me = try await client.getMe()
            applyMe(me)
        } catch {
            #if DEBUG
            print("[AuthStore] register error: \(error)")
            #endif
            recordError(error)
        }
        isLoading = false
    }

    /// 统一错误收纳：errorMessage 优先取后端给的具体原因（failureReason），
    /// fallback 到 LocalizedError 的标题（errorDescription / localizedDescription）。
    ///
    /// 旧实现只取 localizedDescription，导致：
    /// - 后端返回 409 conflict "该用户名已被注册" → UI 显示 "Server Error"
    /// - 后端返回 401 "用户名或密码错误" → UI 显示 "Login Failed"
    ///
    /// 现在错误条 = 后端给的人话 message，登录/注册失败用户能立即知道为什么。
    private func recordError(_ error: Error) {
        let api = error as? APIError
        lastError = api
        errorMessage = api?.failureReason ?? error.localizedDescription
    }

    // MARK: - Guest

    func enterAsGuest() {
        role = .guest
        isAuthenticated = true
        userInfo = nil
        pendingBiometricCredential = nil
    }

    /// 编辑 filter 保存后调用——把 ``userInfo.listingFilter`` 同步成后端
    /// 规范化过的版本。Dashboard.meSummary 等读 userInfo 的视图会即时刷新。
    func updateLocalFilter(_ filter: ListingFilter) {
        guard var info = userInfo else { return }
        info.listingFilter = filter
        userInfo = info
    }

    // MARK: - Logout

    func logout() async {
        _ = try? await client.logout()
        pendingBiometricCredential = nil
        KeychainManager.delete(server: server)
        UserDefaults.standard.removeObject(forKey: "auth_token")
        client.setToken(nil)
        role = .guest
        isAuthenticated = false
        userInfo = nil
        errorMessage = nil
    }

    // MARK: - Delete Account

    // MARK: - Change Password

    /// 修改当前 user 密码。
    ///
    /// 成功 → 返回 true，并把 errorMessage 清空；调用方负责 UI dismiss。
    /// 失败 → 返回 false，errorMessage 含后端 message。
    ///
    /// 调用前应先在 UI 层校验：
    /// - 两次新密码一致
    /// - 新密码 ≥ 4 字符
    /// - 新密码 != 当前密码（也可放给后端，会返 validation 错误）
    ///
    /// 副作用：后端会撤销该 user 名下"除当前 token 外"的所有 session。
    /// 当前设备保持登录态。
    func changePassword(current: String, new: String) async -> Bool {
        guard role == .user else {
            errorMessage = String(localized: "Only user accounts can change password here.")
            return false
        }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            _ = try await client.changePassword(current: current, new: new)
            return true
        } catch {
            #if DEBUG
            print("[AuthStore] changePassword error: \(error)")
            #endif
            recordError(error)
            return false
        }
    }

    func deleteAccount() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            _ = try await client.deleteAccount()
            // Clear local state and return to login
            KeychainManager.delete(server: server)
            UserDefaults.standard.removeObject(forKey: "auth_token")
            client.setToken(nil)
            role = .guest
            isAuthenticated = false
            userInfo = nil
        } catch {
            #if DEBUG
            print("[AuthStore] deleteAccount error: \(error)")
            #endif
            recordError(error)
        }
    }

    // MARK: - Private

    private func applyMe(_ me: MeResponse) {
        isAuthenticated = true
        role = Role(rawValue: me.role) ?? .guest
        userInfo = me.user
    }

    var isAdmin: Bool { role == .admin }
    var isUser: Bool { role == .user }
    var isGuest: Bool { role == .guest }
}
