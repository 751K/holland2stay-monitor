import Foundation
import LocalAuthentication

// MARK: - Biometric authentication service

/// Face ID / Touch ID 封装：检测可用性 + 触发认证 + 读取 Keychain 中受生物特征保护的凭据。
enum BiometricAuthService {
    /// 本地存储的生物凭据：登录凭据（不含其他加密数据）。
    struct StoredCredential: Codable {
        let username: String
        let password: String
    }

    // MARK: - Availability

    static var isAvailable: Bool {
        var error: NSError?
        let available = LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error)
        return available
    }

    static var biometryName: String {
        let ctx = LAContext()
        _ = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        switch ctx.biometryType {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        default: return "Biometrics"
        }
    }

    static var hasStoredCredentials: Bool {
        let query: [String: Any] = [
            kSecClass as String:       kSecClassGenericPassword,
            kSecAttrAccount as String: "flatradar_biometric",
            kSecAttrService as String: "com.flatradar.biometric",
            kSecReturnData as String:  true,
            kSecMatchLimit as String:  kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        return status == errSecSuccess
    }

    // MARK: - Save / Delete

    static func saveCredentials(_ cred: StoredCredential) throws {
        deleteCredentials()

        let data = try JSONEncoder().encode(cred)
        let access = SecAccessControlCreateWithFlags(
            kCFAllocatorDefault,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            .biometryCurrentSet,
            nil
        )!
        let query: [String: Any] = [
            kSecClass as String:       kSecClassGenericPassword,
            kSecAttrAccount as String: "flatradar_biometric",
            kSecAttrService as String: "com.flatradar.biometric",
            kSecValueData as String:   data,
            kSecAttrAccessControl as String: access,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw NSError(domain: "BiometricAuth", code: Int(status),
                         userInfo: [NSLocalizedDescriptionKey: "Keychain save failed (OSStatus \(status))"])
        }
    }

    static func deleteCredentials() {
        let query: [String: Any] = [
            kSecClass as String:       kSecClassGenericPassword,
            kSecAttrAccount as String: "flatradar_biometric",
            kSecAttrService as String: "com.flatradar.biometric",
        ]
        SecItemDelete(query as CFDictionary)
    }

    // MARK: - Authenticate + load

    /// 触发生物认证，成功后从 Keychain 读取凭据。
    /// - Parameter reason: Face ID 提示文字
    /// - Returns: 解密后的凭据；认证失败 / 凭据不存在时返回 nil
    static func authenticateAndLoad(reason: String) async -> StoredCredential? {
        guard isAvailable else { return nil }

        let ctx = LAContext()
        ctx.localizedFallbackTitle = "Enter Password"
        ctx.localizedReason = reason

        do {
            let success = try await ctx.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: reason
            )
            guard success else { return nil }
        } catch {
            return nil
        }

        // 复用已认证的 LAContext 读取 Keychain —— 避免二次弹出系统面容提示，
        // 同时解决 kSecUseOperationPrompt 在 iOS 14 已废弃的问题。
        let query: [String: Any] = [
            kSecClass as String:              kSecClassGenericPassword,
            kSecAttrAccount as String:        "flatradar_biometric",
            kSecAttrService as String:        "com.flatradar.biometric",
            kSecReturnData as String:         true,
            kSecMatchLimit as String:         kSecMatchLimitOne,
            kSecUseAuthenticationContext as String: ctx,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let cred = try? JSONDecoder().decode(StoredCredential.self, from: data) else {
            return nil
        }
        return cred
    }
}
