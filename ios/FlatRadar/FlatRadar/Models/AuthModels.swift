import Foundation

// MARK: - Request

struct LoginRequest: Encodable {
    let username: String
    let password: String
    let deviceName: String
    let ttlDays: Int

    enum CodingKeys: String, CodingKey {
        case username, password
        case deviceName = "device_name"
        case ttlDays = "ttl_days"
    }
}

// MARK: - Responses

struct LoginResponse: Decodable {
    let token: String
    let tokenID: Int
    let role: String
    let userID: String?
    let deviceName: String
    let ttlDays: Int

    enum CodingKeys: String, CodingKey {
        case token
        case tokenID = "token_id"
        case role
        case userID = "user_id"
        case deviceName = "device_name"
        case ttlDays = "ttl_days"
    }
}

struct MeResponse: Decodable {
    let role: String
    let userID: String?
    let user: UserInfo?

    enum CodingKeys: String, CodingKey {
        case role
        case userID = "user_id"
        case user
    }
}
