import Foundation

/// User's listing filter config, from /auth/me -> user.listing_filter.
/// Mirrors backend config.ListingFilter fields exactly.
///
/// ``Encodable`` 用于 ``PUT /me/filter`` 提交 —— 后端 ``_coerce_filter_payload``
/// 会做白名单过滤 + 边界校验，多/少字段都安全。
struct ListingFilter: Codable, Equatable, Sendable {
    var maxRent: Double?
    var minArea: Double?
    var minFloor: Int?
    var allowedOccupancy: [String]
    var allowedTypes: [String]
    var allowedNeighborhoods: [String]
    var allowedCities: [String]
    var allowedSources: [String]
    var allowedContract: [String]
    var allowedTenant: [String]
    var allowedOffer: [String]
    var allowedFinishing: [String]
    var allowedEnergy: String

    enum CodingKeys: String, CodingKey {
        case maxRent = "max_rent"
        case minArea = "min_area"
        case minFloor = "min_floor"
        case allowedOccupancy = "allowed_occupancy"
        case allowedTypes = "allowed_types"
        case allowedNeighborhoods = "allowed_neighborhoods"
        case allowedCities = "allowed_cities"
        case allowedSources = "allowed_sources"
        case allowedContract = "allowed_contract"
        case allowedTenant = "allowed_tenant"
        case allowedOffer = "allowed_offer"
        case allowedFinishing = "allowed_finishing"
        case allowedEnergy = "allowed_energy"
    }

    /// 显式 memberwise init —— 因为下面自定义了 `init(from:)`，Swift 不再合成默认
    /// memberwise init，但 `ListingFilter.empty` 和测试代码还要用它。
    init(
        maxRent: Double?,
        minArea: Double?,
        minFloor: Int?,
        allowedOccupancy: [String],
        allowedTypes: [String],
        allowedNeighborhoods: [String],
        allowedCities: [String],
        allowedSources: [String],
        allowedContract: [String],
        allowedTenant: [String],
        allowedOffer: [String],
        allowedFinishing: [String],
        allowedEnergy: String
    ) {
        self.maxRent = maxRent
        self.minArea = minArea
        self.minFloor = minFloor
        self.allowedOccupancy = allowedOccupancy
        self.allowedTypes = allowedTypes
        self.allowedNeighborhoods = allowedNeighborhoods
        self.allowedCities = allowedCities
        self.allowedSources = allowedSources
        self.allowedContract = allowedContract
        self.allowedTenant = allowedTenant
        self.allowedOffer = allowedOffer
        self.allowedFinishing = allowedFinishing
        self.allowedEnergy = allowedEnergy
    }

    /// 容错 decoder ——任一 list/字符串字段缺失都回退默认值，避免老 backend
    /// 不返回 `allowed_sources` 等新字段时直接 `data error`。
    ///
    /// 背景
    /// ----
    /// P1 多源重构时 iOS 加了 `allowedSources: [String]` 必需字段，但 prod
    /// backend 在升级到含 multi-source 的 config.py 之前，``asdict(listing_filter)``
    /// 不会输出 `allowed_sources` key —— Swift 严格 Decodable 抛 keyNotFound，
    /// 用户看到登录后 "data error" 立即返回 LoginView。
    ///
    /// 解法
    /// ----
    /// 每个 list 字段用 `decodeIfPresent ?? []`，allowedEnergy 用 `?? ""`，
    /// 跨版本前后兼容。新 iOS ↔ 老 backend / 老 iOS ↔ 新 backend 都不爆。
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.maxRent           = try c.decodeIfPresent(Double.self, forKey: .maxRent)
        self.minArea           = try c.decodeIfPresent(Double.self, forKey: .minArea)
        self.minFloor          = try c.decodeIfPresent(Int.self,    forKey: .minFloor)
        self.allowedOccupancy  = try c.decodeIfPresent([String].self, forKey: .allowedOccupancy)     ?? []
        self.allowedTypes      = try c.decodeIfPresent([String].self, forKey: .allowedTypes)         ?? []
        self.allowedNeighborhoods = try c.decodeIfPresent([String].self, forKey: .allowedNeighborhoods) ?? []
        self.allowedCities     = try c.decodeIfPresent([String].self, forKey: .allowedCities)        ?? []
        self.allowedSources    = try c.decodeIfPresent([String].self, forKey: .allowedSources)       ?? []
        self.allowedContract   = try c.decodeIfPresent([String].self, forKey: .allowedContract)      ?? []
        self.allowedTenant     = try c.decodeIfPresent([String].self, forKey: .allowedTenant)        ?? []
        self.allowedOffer      = try c.decodeIfPresent([String].self, forKey: .allowedOffer)         ?? []
        self.allowedFinishing  = try c.decodeIfPresent([String].self, forKey: .allowedFinishing)     ?? []
        self.allowedEnergy     = try c.decodeIfPresent(String.self,   forKey: .allowedEnergy)        ?? ""
    }

    /// 空 filter（所有字段 default）—— Edit view 的"重置"按钮用。
    static let empty = ListingFilter(
        maxRent: nil, minArea: nil, minFloor: nil,
        allowedOccupancy: [], allowedTypes: [], allowedNeighborhoods: [],
        allowedCities: [], allowedSources: [], allowedContract: [], allowedTenant: [],
        allowedOffer: [], allowedFinishing: [], allowedEnergy: "")

    /// 后端 ``is_empty`` 等价判断：所有字段都为默认。
    var isEmpty: Bool {
        maxRent == nil && minArea == nil && minFloor == nil
            && allowedOccupancy.isEmpty && allowedTypes.isEmpty
            && allowedNeighborhoods.isEmpty && allowedCities.isEmpty
            && allowedSources.isEmpty
            && allowedContract.isEmpty && allowedTenant.isEmpty
            && allowedOffer.isEmpty && allowedFinishing.isEmpty
            && allowedEnergy.isEmpty
    }

    /// 人类可读的一行摘要，用于 Settings 入口卡片下方提示。
    /// 例 "≤ €900/mo · ≥ 25 m² · Eindhoven, Amsterdam · Energy ≥ B"
    var summary: String {
        var parts: [String] = []
        if let r = maxRent { parts.append("≤ €\(Int(r))/mo") }
        if let a = minArea { parts.append("≥ \(Int(a)) m²") }
        if let f = minFloor { parts.append("Floor ≥ \(f)") }
        if !allowedCities.isEmpty {
            parts.append(allowedCities.prefix(3).joined(separator: ", ")
                + (allowedCities.count > 3 ? "…" : ""))
        }
        if !allowedSources.isEmpty {
            parts.append(allowedSources.map(Self.sourceShortText).joined(separator: ", "))
        }
        if !allowedEnergy.isEmpty { parts.append("Energy ≥ \(allowedEnergy)") }
        return parts.isEmpty ? "No filters" : parts.joined(separator: " · ")
    }

    private static nonisolated func sourceShortText(_ source: String) -> String {
        switch source.lowercased() {
        case "holland2stay": return "H2S"
        case "ourdomain": return "OD"
        case "xior": return "XR"
        default: return source.uppercased()
        }
    }
}

/// 已知能耗等级白名单，与后端 ``config.ENERGY_LABELS`` 对齐（优→差排序）。
/// FilterEditView 的 picker 选项。
let energyLabels = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
