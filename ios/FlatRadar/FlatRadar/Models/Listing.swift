import Foundation

struct Listing: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let status: String
    let priceRaw: String?
    let priceValue: Double?
    let availableFrom: String?
    let features: [String]
    let featureMap: [String: String]
    let url: String
    let city: String
    let firstSeen: String?
    let lastSeen: String?

    enum CodingKeys: String, CodingKey {
        case id, name, status, features, url, city
        case priceRaw = "price_raw"
        case priceValue = "price_value"
        case availableFrom = "available_from"
        case featureMap = "feature_map"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }
}

extension Listing {
    /// Server-side timezone — Holland2Stay 后端发的日期字符串都按 Europe/Amsterdam
    /// 解读，避免在做 "now() - first_seen" 计算时因为本地时区抖动出现 25h / -1h。
    fileprivate static let amsterdamTZ: TimeZone = TimeZone(identifier: "Europe/Amsterdam") ?? .current

    private static let dateParser: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = amsterdamTZ
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static let shortDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = amsterdamTZ
        f.dateFormat = "MMM d"
        return f
    }()

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let fallbackParsers: [DateFormatter] = {
        let formats = [
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
        ]
        return formats.map { fmt in
            let f = DateFormatter()
            f.calendar = Calendar(identifier: .gregorian)
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = amsterdamTZ
            f.dateFormat = fmt
            return f
        }
    }()

    /// Holland2Stay 三态业务语义 — 设计稿用这个区分 ●Book / ●Lottery / ●Reserved。
    enum StatusKind {
        case book      // 先到先得
        case lottery   // 抽签
        case reserved  // 已订 / Rented / Not available
        case other     // 未识别的状态原样兜底
    }

    var isBookable: Bool {
        status.localizedCaseInsensitiveContains("available to book")
    }

    var isLottery: Bool {
        status.localizedCaseInsensitiveContains("lottery")
    }

    /// 归一化后的状态枚举。原始后端可能返回 "Available to book"/"available_to_book"/
    /// "Available in lottery"/"Reserved"/"Rented"/"Not available" 等多种写法。
    var statusKind: StatusKind {
        let s = status.lowercased().replacingOccurrences(of: "_", with: " ")
        if s.contains("lottery") { return .lottery }
        if s.contains("available to book") || s == "book" { return .book }
        if s.contains("reserved") || s.contains("rented") || s.contains("not available") {
            return .reserved
        }
        return .other
    }

    var areaText: String? {
        featureValue(matching: ["area", "surface", "living area", "m2", "m²"])
    }

    var floorText: String? {
        featureValue(matching: ["floor", "level"])
    }

    var energyText: String? {
        featureValue(matching: ["energy", "energy label"])
    }

    var contractText: String? {
        featureValue(matching: ["contract", "rental agreement", "agreement"])
    }

    var typeText: String? {
        featureValue(matching: ["type", "property type", "apartment type"])
    }

    var buildingText: String? {
        featureValue(matching: ["building", "building name", "building_name", "complex"])
    }

    var availableDayKey: String? {
        guard let availableFrom, !availableFrom.isEmpty else { return nil }
        return String(availableFrom.prefix(10))
    }

    /// 后端用 "2050-01-01" / "1900-01-01" 这种远端日期当 "未知" 占位 —
    /// 列表里不应直接展示给用户（设计稿 ⑨ "干掉 1 Jan 2050"）。
    var hasRealAvailableDate: Bool {
        guard let day = availableDayKey else { return false }
        if day.hasPrefix("2049") || day.hasPrefix("2050") || day.hasPrefix("1900") { return false }
        return true
    }

    /// "Jun 22" 形态的短日期，仅当不是占位时返回。
    var availableShortText: String? {
        guard hasRealAvailableDate, let day = availableDayKey else { return nil }
        guard let date = Self.dateParser.date(from: day) else { return nil }
        return Self.shortDateFormatter.string(from: date)
    }

    /// Parse `first_seen` —— 复用 ServerTime 的多格式兼容。
    var firstSeenDate: Date? {
        guard let firstSeen, !firstSeen.isEmpty else { return nil }
        if let d = Self.isoFormatter.date(from: firstSeen) { return d }
        for f in Self.fallbackParsers {
            if let d = f.date(from: firstSeen) { return d }
        }
        return nil
    }

    /// 24h 内首次出现的房源 — 用于 "NEW TODAY" 分组和 NEW 徽章。
    var isNew: Bool {
        guard let d = firstSeenDate else { return false }
        return Date().timeIntervalSince(d) < 24 * 3600
    }

    /// 相对年龄串："now" / "38m" / "5h" / "2d"。
    var ageText: String? {
        guard let d = firstSeenDate else { return nil }
        let interval = Date().timeIntervalSince(d)
        if interval < 60 { return "now" }
        if interval < 3600 { return "\(Int(interval / 60))m" }
        if interval < 86400 { return "\(Int(interval / 3600))h" }
        return "\(Int(interval / 86400))d"
    }

    func featureValue(matching aliases: [String]) -> String? {
        let normalizedAliases = aliases.map(normalizeFeatureKey)
        for (key, value) in featureMap {
            let normalizedKey = normalizeFeatureKey(key)
            if normalizedAliases.contains(where: { normalizedKey.contains($0) || $0.contains(normalizedKey) }) {
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                return trimmed.isEmpty ? nil : trimmed
            }
        }
        return nil
    }

    private func normalizeFeatureKey(_ key: String) -> String {
        key
            .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
    }
}
