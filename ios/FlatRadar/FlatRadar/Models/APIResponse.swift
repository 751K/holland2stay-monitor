import Foundation

enum ServerTime {
    nonisolated static let timeZone = TimeZone(identifier: "Europe/Amsterdam") ?? .current

    // MARK: - Static formatters (DateFormatter creation is expensive)
    //
    // Swift 6 strict concurrency 下，static let 默认走 MainActor 隔离，但
    // display(_:) / parse(_:) 等是 nonisolated，跨不过去 → 编译错。
    // 加 `nonisolated` 关键字解除隔离；iOS 18 SDK 的 DateFormatter /
    // ISO8601DateFormatter 已声明为 Sendable，编译器不需要额外 (unsafe) 兜底。

    // ISO8601DateFormatter 当前 SDK 尚未标 Sendable，单 nonisolated 通不过严格
    // 并发检查；只读使用（仅 .date(from:)）实际线程安全，用 (unsafe) 关掉警告。
    nonisolated(unsafe) private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    nonisolated(unsafe) private static let isoNoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    nonisolated private static let dateParser: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = timeZone
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    nonisolated private static let displayFormatterTZ: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = timeZone
        f.dateFormat = "MMM d, HH:mm zzz"
        return f
    }()

    nonisolated private static let displayFormatterNoTZ: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = timeZone
        f.dateFormat = "MMM d, HH:mm"
        return f
    }()

    nonisolated private static let mediumDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = timeZone
        f.dateStyle = .medium
        f.timeStyle = .none
        return f
    }()

    nonisolated private static let fallbackParsers: [DateFormatter] = {
        let formats = [
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
            "yyyy/MM/dd HH:mm:ss",
            "yyyy/MM/dd HH:mm",
        ]
        return formats.map { fmt in
            let f = DateFormatter()
            f.calendar = Calendar(identifier: .gregorian)
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = timeZone
            f.dateFormat = fmt
            return f
        }
    }()

    // MARK: - Public API

    nonisolated static func display(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != "--", trimmed != "—" else { return raw }
        if isDateOnly(trimmed) {
            return displayDate(trimmed)
        }
        guard let date = parse(trimmed) else { return raw }

        let fmt = shouldShowTimeZone(for: date) ? displayFormatterTZ : displayFormatterNoTZ
        return fmt.string(from: date)
    }

    /// 「入住日未定」的哨兵年份。
    ///
    /// H2S 在入住日未定时发的是 `2050-01-01`。scraper、存储层、booker 都认得它，
    /// 唯独界面把它当成一个日期显示——地图弹卡和房源详情都写着「2050 年 1 月 1 日
    /// 可入住」，读起来像一个（荒唐的）事实，而它的意思其实是「不知道」。
    ///
    /// 按**年份**判而不是精确匹配那一天，哨兵改成 2099 时不至于漏。判据与
    /// `models.SENTINEL_AVAILABLE_FROM_YEAR`、`app.js` 的
    /// `SENTINEL_AVAILABLE_FROM_YEAR` 保持一致。
    nonisolated static let sentinelAvailableFromYear = 2050

    /// 这个 `available_from` 是不是哨兵（而不是真日期）。
    nonisolated static func isSentinelDate(_ raw: String?) -> Bool {
        let trimmed = (raw ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 4, let year = Int(trimmed.prefix(4)) else { return false }
        return year >= sentinelAvailableFromYear
    }

    /// 显示用日期。哨兵返回 "—"，不冒充成一个日期。
    ///
    /// **不加 `dash:` 之类的默认参数**：带默认值的函数引用没法当
    /// `(String) -> String` 传，而 `availableFrom.map(ServerTime.displayDate)`
    /// 这种写法在详情页有两处。
    nonisolated static func displayDate(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return raw }
        if isSentinelDate(trimmed) { return "—" }
        let source = String(trimmed.prefix(10))

        guard let date = dateParser.date(from: source) else { return raw }
        return mediumDateFormatter.string(from: date)
    }

    nonisolated private static func shouldShowTimeZone(for date: Date) -> Bool {
        TimeZone.current.secondsFromGMT(for: date) != timeZone.secondsFromGMT(for: date)
    }

    nonisolated private static func isDateOnly(_ raw: String) -> Bool {
        raw.count == 10 && raw.dropFirst(4).first == "-" && raw.dropFirst(7).first == "-"
    }

    nonisolated private static func parse(_ raw: String) -> Date? {
        if let date = isoFrac.date(from: raw) { return date }
        if let date = isoNoFrac.date(from: raw) { return date }

        for f in fallbackParsers {
            if let date = f.date(from: raw) { return date }
        }
        return nil
    }

    /// "2m ago" / "1h ago" / "3d ago" style relative time from ISO 8601.
    nonisolated static func relativeTime(_ iso: String) -> String {
        guard !iso.isEmpty, iso != "--" else { return "--" }
        guard let date = parse(iso) else { return iso }
        let secs = max(0, Int(Date().timeIntervalSince(date)))
        switch secs {
        case 0..<60: return "\(secs)s ago"
        case 60..<3600: return "\(secs / 60)m ago"
        case 3600..<86400: return "\(secs / 3600)h ago"
        default: return "\(secs / 86400)d ago"
        }
    }
}

/// Generic envelope matching backend {ok, data} / {ok, error} shape.
/// Every /api/v1/* response decodes through this type.
nonisolated struct APIResponse<T: Decodable>: Decodable {
    let ok: Bool
    let data: T?
    let error: APIErrorPayload?

    enum CodingKeys: String, CodingKey {
        case ok, data, error
    }

    nonisolated init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ok = try container.decode(Bool.self, forKey: .ok)
        data = try container.decodeIfPresent(T.self, forKey: .data)
        error = try container.decodeIfPresent(APIErrorPayload.self, forKey: .error)
    }
}

nonisolated struct APIErrorPayload: Decodable {
    let code: String
    let message: String

    enum CodingKeys: String, CodingKey {
        case code, message
    }

    nonisolated init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        code = try container.decode(String.self, forKey: .code)
        message = try container.decode(String.self, forKey: .message)
    }
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
    let language: String  // "en" | "zh" | ...

    enum CodingKeys: String, CodingKey {
        case deviceToken = "device_token"
        case env, platform, model
        case bundleId = "bundle_id"
        case language
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

/// `GET /api/v1/filter/options` 响应——FilterEditView 用来渲染所有多选项的候选。
///
/// **跨版本兼容**：自定义 `init(from:)` 让任一字段缺失都回退 `[]`。
/// 老 backend 没有 `sources` 字段（P1 多源新加）时 iOS 不会 data error。
struct FilterOptions: Decodable, Sendable {
    let cities: [String]
    let sources: [String]
    let occupancy: [String]
    let types: [String]
    let neighborhoods: [String]
    let contract: [String]
    let tenant: [String]
    let offer: [String]
    let finishing: [String]
    let energy: [String]

    /// 每个过滤维度实际生效于哪些平台，key 是**后端的维度名**
    /// （`contract` / `neighborhood` / `offer` / …），值是 source key 列表。
    ///
    /// 后端 `_SOURCE_FILTER_DIMS` 决定的是一件沉默的事：勾了 Contract 只会
    /// 影响 Holland2Stay，其余六个平台整条跳过。老 backend 不返回这个 key，
    /// 此时为空字典——界面退回不作标注，而不是标成"对所有平台生效"。
    let dimSources: [String: [String]]

    enum CodingKeys: String, CodingKey {
        case cities, sources, occupancy, types, neighborhoods
        case contract, tenant, offer, finishing, energy
        case dimSources = "dim_sources"
    }

    init(
        cities: [String], sources: [String], occupancy: [String],
        types: [String], neighborhoods: [String], contract: [String],
        tenant: [String], offer: [String], finishing: [String], energy: [String],
        dimSources: [String: [String]] = [:]
    ) {
        self.cities = cities; self.sources = sources; self.occupancy = occupancy
        self.types = types; self.neighborhoods = neighborhoods; self.contract = contract
        self.tenant = tenant; self.offer = offer; self.finishing = finishing; self.energy = energy
        self.dimSources = dimSources
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.cities        = try c.decodeIfPresent([String].self, forKey: .cities)        ?? []
        self.sources       = try c.decodeIfPresent([String].self, forKey: .sources)       ?? []
        self.occupancy     = try c.decodeIfPresent([String].self, forKey: .occupancy)     ?? []
        self.types         = try c.decodeIfPresent([String].self, forKey: .types)         ?? []
        self.neighborhoods = try c.decodeIfPresent([String].self, forKey: .neighborhoods) ?? []
        self.contract      = try c.decodeIfPresent([String].self, forKey: .contract)      ?? []
        self.tenant        = try c.decodeIfPresent([String].self, forKey: .tenant)        ?? []
        self.offer         = try c.decodeIfPresent([String].self, forKey: .offer)         ?? []
        self.finishing     = try c.decodeIfPresent([String].self, forKey: .finishing)     ?? []
        self.energy        = try c.decodeIfPresent([String].self, forKey: .energy)        ?? []
        self.dimSources    = try c.decodeIfPresent([String: [String]].self, forKey: .dimSources) ?? [:]
    }

    static let empty = FilterOptions(
        cities: [], sources: [], occupancy: [], types: [], neighborhoods: [],
        contract: [], tenant: [], offer: [], finishing: [], energy: [])
}

/// `POST /api/v1/auth/verify` 响应。密码正确才会返回，错误走 401 抛错路径。
struct VerifyPasswordResponse: Decodable {
    let ok: Bool
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

/// `DELETE /api/v1/me` 响应
struct AccountDeleteResponse: Decodable {
    let deleted: Bool
    let userId: String

    enum CodingKeys: String, CodingKey {
        case deleted
        case userId = "user_id"
    }
}

/// `GET /api/v1/legal` 响应
struct LegalResponse: Decodable {
    let terms: String
    let privacy: String
    let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case terms, privacy
        case updatedAt = "updated_at"
    }
}

/// `POST /api/v1/auth/password` 响应
struct ChangePasswordResponse: Decodable {
    /// 改密码同时被撤销的"其他设备会话"数量；当前 token 不在内
    let revokedOtherSessions: Int

    enum CodingKeys: String, CodingKey {
        case revokedOtherSessions = "revoked_other_sessions"
    }
}
