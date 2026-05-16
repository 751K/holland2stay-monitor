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
        case allowedContract = "allowed_contract"
        case allowedTenant = "allowed_tenant"
        case allowedOffer = "allowed_offer"
        case allowedFinishing = "allowed_finishing"
        case allowedEnergy = "allowed_energy"
    }

    /// 空 filter（所有字段 default）—— Edit view 的"重置"按钮用。
    static let empty = ListingFilter(
        maxRent: nil, minArea: nil, minFloor: nil,
        allowedOccupancy: [], allowedTypes: [], allowedNeighborhoods: [],
        allowedCities: [], allowedContract: [], allowedTenant: [],
        allowedOffer: [], allowedFinishing: [], allowedEnergy: "")

    /// 后端 ``is_empty`` 等价判断：所有字段都为默认。
    var isEmpty: Bool {
        maxRent == nil && minArea == nil && minFloor == nil
            && allowedOccupancy.isEmpty && allowedTypes.isEmpty
            && allowedNeighborhoods.isEmpty && allowedCities.isEmpty
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
        if !allowedEnergy.isEmpty { parts.append("Energy ≥ \(allowedEnergy)") }
        return parts.isEmpty ? "No filters" : parts.joined(separator: " · ")
    }
}

/// 已知能耗等级白名单，与后端 ``config.ENERGY_LABELS`` 对齐（优→差排序）。
/// FilterEditView 的 picker 选项。
let energyLabels = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
