import Foundation

/// Generic envelope matching backend {ok, data} / {ok, error} shape.
/// Every /api/v1/* response decodes through this type.
struct APIResponse<T: Decodable>: Decodable {
    let ok: Bool
    let data: T?
    let error: APIErrorPayload?
}

struct APIErrorPayload: Decodable {
    let code: String
    let message: String
}

// MARK: - Paginated responses

struct ListingsResponse: Decodable {
    let items: [Listing]
    let total: Int
    let limit: Int
    let offset: Int
    let filtered: Bool?
}

struct NotificationsResponse: Decodable {
    let items: [NotificationItem]
    let total: Int
    let unread: Int
    let limit: Int
    let offset: Int
}

// MARK: - Me endpoints

struct MeSummary: Decodable {
    let role: String
    let totalInDb: Int
    let new24hTotal: Int
    let matchedTotal: Int
    let matchedAvailable: Int?
    let lastScrape: String
    let filterActive: Bool

    enum CodingKeys: String, CodingKey {
        case role
        case totalInDb = "total_in_db"
        case new24hTotal = "new_24h_total"
        case matchedTotal = "matched_total"
        case matchedAvailable = "matched_available"
        case lastScrape = "last_scrape"
        case filterActive = "filter_active"
    }
}

struct MeFilterResponse: Decodable {
    let role: String
    let filter: ListingFilter
    let isEmpty: Bool

    enum CodingKeys: String, CodingKey {
        case role, filter
        case isEmpty = "is_empty"
    }
}

// MARK: - Mark read

struct MarkReadResponse: Decodable {
    let marked: Bool
}

// MARK: - Devices / APNs (Phase 3)

/// `POST /api/v1/devices/register` 请求体。
struct DeviceRegisterRequest: Encodable {
    let deviceToken: String
    let env: String       // "sandbox" | "production"
    let platform: String  // "ios"
    let model: String
    let bundleId: String

    enum CodingKeys: String, CodingKey {
        case deviceToken = "device_token"
        case env, platform, model
        case bundleId = "bundle_id"
    }
}

struct DeviceRegisterResponse: Decodable {
    let deviceId: Int
    let env: String
    let platform: String

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case env, platform
    }
}

/// `/api/v1/devices` 列表返回；device_token 只回脱敏 hint，不会回明文。
struct DeviceListResponse: Decodable {
    let items: [DeviceInfo]
}

struct DeviceInfo: Decodable, Identifiable {
    let id: Int
    let deviceTokenHint: String
    let env: String
    let platform: String
    let model: String
    let createdAt: String
    let lastSeen: String
    let disabled: Bool
    let disabledReason: String

    enum CodingKeys: String, CodingKey {
        case id
        case deviceTokenHint = "device_token_hint"
        case env, platform, model
        case createdAt = "created_at"
        case lastSeen = "last_seen"
        case disabled
        case disabledReason = "disabled_reason"
    }
}

struct DeviceDeleteResponse: Decodable {
    let deleted: Bool
}

/// `POST /api/v1/devices/test` 响应。
struct DeviceTestPushResponse: Decodable {
    let sent: Int
    let total: Int
    let results: [DeviceTestPushResult]
}

struct DeviceTestPushResult: Decodable, Identifiable {
    var id: String { deviceTokenHint }
    let deviceTokenHint: String
    let env: String
    let status: Int
    let reason: String
    let ok: Bool

    enum CodingKeys: String, CodingKey {
        case deviceTokenHint = "device_token_hint"
        case env, status, reason, ok
    }
}
