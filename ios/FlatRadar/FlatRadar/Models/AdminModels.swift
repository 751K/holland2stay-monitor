import Foundation

/// 管理端用户摘要 —— ``GET /api/v1/admin/users`` 返回的 items 元素。
/// iOS Admin UI 只读展示 + 切 enabled / 删除。完整字段编辑（通知渠道凭证 /
/// 自动预订配置）仍走 Web 后台。
struct AdminUserSummary: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let enabled: Bool
    let notificationsEnabled: Bool
    let channelCount: Int
    let channels: [String]
    let appLoginEnabled: Bool
    let hasAppPassword: Bool
    let activeDevices: Int
    let autoBookEnabled: Bool
    let filterSummary: AdminFilterSummary

    enum CodingKeys: String, CodingKey {
        case id, name, enabled
        case notificationsEnabled = "notifications_enabled"
        case channelCount = "channel_count"
        case channels
        case appLoginEnabled = "app_login_enabled"
        case hasAppPassword = "has_app_password"
        case activeDevices = "active_devices"
        case autoBookEnabled = "auto_book_enabled"
        case filterSummary = "filter_summary"
    }
}

struct AdminFilterSummary: Decodable, Hashable, Sendable {
    let maxRent: Double?
    let minArea: Double?
    let minFloor: Int?
    let cities: [String]
    let energy: String
    let filterActive: Bool

    enum CodingKeys: String, CodingKey {
        case maxRent = "max_rent"
        case minArea = "min_area"
        case minFloor = "min_floor"
        case cities, energy
        case filterActive = "filter_active"
    }

    var compactDescription: String {
        var parts: [String] = []
        if let r = maxRent { parts.append("≤€\(Int(r))") }
        if let a = minArea { parts.append("≥\(Int(a))m²") }
        if let f = minFloor { parts.append("F≥\(f)") }
        if !cities.isEmpty {
            parts.append(cities.prefix(2).joined(separator: ",")
                + (cities.count > 2 ? "…" : ""))
        }
        if !energy.isEmpty { parts.append("⚡\(energy)") }
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
    }
}

/// `GET /api/v1/admin/users` 响应。
struct AdminUsersResponse: Decodable, Sendable {
    let items: [AdminUserSummary]
    let total: Int
}

/// `POST /api/v1/admin/users/<id>/toggle` 响应。
struct AdminUserToggleResponse: Decodable, Sendable {
    let id: String
    let enabled: Bool
}

/// `DELETE /api/v1/admin/users/<id>` 响应。
struct AdminUserDeleteResponse: Decodable, Sendable {
    let deleted: Bool
    let name: String
    let revokedSessions: Int

    enum CodingKeys: String, CodingKey {
        case deleted, name
        case revokedSessions = "revoked_sessions"
    }
}

/// `GET /api/v1/admin/monitor/status` 响应。
struct AdminMonitorStatus: Decodable, Sendable {
    let running: Bool
    let pid: Int?
    let lastScrape: String
    let lastCount: String

    enum CodingKeys: String, CodingKey {
        case running, pid
        case lastScrape = "last_scrape"
        case lastCount = "last_count"
    }
}

/// `POST /api/v1/admin/monitor/{start|stop|reload}` 响应。
struct AdminMonitorActionResponse: Decodable, Sendable {
    let started: Bool?
    let stopped: Bool?
    let pid: Int?
    let reload: Bool?
    let method: String?     // "signal" / "file"
}
