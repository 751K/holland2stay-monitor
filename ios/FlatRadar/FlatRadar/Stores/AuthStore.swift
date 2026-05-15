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

    private let client = APIClient.shared
    private var server: String {
        UserDefaults.standard.string(forKey: "server_url") ?? APIClient.defaultServerHost
    }

    // MARK: - Restore Session

    func restoreSession() async {
        let savedToken = KeychainManager.load(server: server)
            ?? UserDefaults.standard.string(forKey: "auth_token")
        guard let token = savedToken else { return }

        await client.setToken(token)

        // Verify token is still valid
        do {
            let me = try await client.getMe()
            applyMe(me)
        } catch {
            // Token expired or revoked — clear and stay on login screen
            KeychainManager.delete(server: server)
            UserDefaults.standard.removeObject(forKey: "auth_token")
            await client.setToken(nil)
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
            await client.setToken(resp.token)
            do {
                try KeychainManager.save(token: resp.token, server: server)
            } catch {
                print("[AuthStore] Keychain save failed, falling back to UserDefaults")
                UserDefaults.standard.set(resp.token, forKey: "auth_token")
            }

            let me = try await client.getMe()
            applyMe(me)
        } catch {
            print("[AuthStore] login error: \(error)")
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    // MARK: - Guest

    func enterAsGuest() {
        role = .guest
        isAuthenticated = true
        userInfo = nil
    }

    // MARK: - Logout

    func logout() async {
        // Best-effort server-side revocation
        _ = try? await client.logout()
        KeychainManager.delete(server: server)
        UserDefaults.standard.removeObject(forKey: "auth_token")
        await client.setToken(nil)
        role = .guest
        isAuthenticated = false
        userInfo = nil
        errorMessage = nil
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
