import Foundation
import Security

/// Thin wrapper around Keychain Services for Bearer token persistence.
/// Keyed by server hostname so switching servers stores separate tokens.
enum KeychainManager {
    private static let service = "com.flatradar.token"

    static func save(token: String, server: String) throws {
        // Remove any existing item first
        delete(server: server)

        let data = Data(token.utf8)
        let query: [String: Any] = [
            kSecClass as String:       kSecClassInternetPassword,
            kSecAttrService as String: service,
            kSecAttrServer as String:  server,
            kSecValueData as String:   data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw APIError.serverError("Keychain save failed (OSStatus \(status))")
        }
    }

    static func load(server: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String:       kSecClassInternetPassword,
            kSecAttrService as String: service,
            kSecAttrServer as String:  server,
            kSecReturnData as String:  true,
            kSecMatchLimit as String:  kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess,
              let data = result as? Data,
              let token = String(data: data, encoding: .utf8) else {
            return nil
        }
        return token
    }

    static func delete(server: String) {
        let query: [String: Any] = [
            kSecClass as String:       kSecClassInternetPassword,
            kSecAttrService as String: service,
            kSecAttrServer as String:  server,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
