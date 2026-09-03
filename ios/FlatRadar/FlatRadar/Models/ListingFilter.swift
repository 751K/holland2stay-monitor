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

    /// 人类可读摘要，用于 Settings 入口卡片下方提示。
    /// 例 "≤ €900/mo · ≥ 25 m² · Eindhoven, Amsterdam · Energy ≥ B"
    var summary: String {
        let parts = summaryParts
        return parts.isEmpty ? "No filters" : parts.joined(separator: " · ")
    }

    /// 摘要的各段，按"用户最可能先关心"排序。
    ///
    /// 为什么单独暴露出来
    /// ------------------
    /// 这个数组必须覆盖 ``isEmpty`` 判断的**全部十三个维度**。此前只覆盖六个
    /// （rent / area / floor / cities / sources / energy），另外七个——房型、
    /// 合同、租客、入住人数、街区、优惠、装修——一个字都不出现。后果不是"显示
    /// 得不全"，是**显示成了相反的意思**：只勾了 "Finishing: Furnished" 的用户，
    /// 设置页写着 "No filters"，而后端 ``config.py`` 的 ``matches()`` 正在按这
    /// 条过滤掉推送。用户会去别处找"为什么收不到通知"。
    ///
    /// 用数组而不是直接拼字符串，是为了让测试能逐段断言，而不是对一整行做
    /// 子串匹配——后者在漏掉一个维度时照样通过。
    var summaryParts: [String] {
        var parts: [String] = []
        if let r = maxRent { parts.append("≤ €\(Int(r))/mo") }
        if let a = minArea { parts.append("≥ \(Int(a)) m²") }
        if let f = minFloor { parts.append("Floor ≥ \(f)") }
        if !allowedCities.isEmpty { parts.append(Self.brief(allowedCities)) }
        if !allowedNeighborhoods.isEmpty { parts.append(Self.brief(allowedNeighborhoods)) }
        if !allowedSources.isEmpty {
            parts.append(allowedSources.map(Self.sourceShortText).joined(separator: ", "))
        }
        if !allowedEnergy.isEmpty { parts.append("Energy ≥ \(allowedEnergy)") }
        if !allowedTypes.isEmpty { parts.append(Self.brief(allowedTypes)) }
        if !allowedOccupancy.isEmpty { parts.append(Self.brief(allowedOccupancy, label: "Occupancy")) }
        if !allowedTenant.isEmpty { parts.append(Self.brief(allowedTenant, label: "Tenant")) }
        if !allowedContract.isEmpty { parts.append(Self.brief(allowedContract, label: "Contract")) }
        if !allowedFinishing.isEmpty { parts.append(Self.brief(allowedFinishing)) }
        if !allowedOffer.isEmpty { parts.append(Self.brief(allowedOffer, label: "Offer")) }
        return parts
    }

    /// 摘要的结构化形态，用于把设置页那行连排文字排成可折行的 chip。
    ///
    /// 为什么不直接用 ``summaryParts``
    /// -------------------------------
    /// 那个数组把平台压成了一段字符串（`"H2S, OC, XR, MG, PZ, SE"`），排成
    /// chip 时它会变成一枚很长的胶囊，而平台本来就有一套认得出的彩色徽章
    /// （``PlatformBadge``）。这里把平台拆开单独给出，其余条件仍是文本。
    ///
    /// 选中平台多于三个时反而合并成一枚 "N platforms"：七选六的时候摆六枚
    /// 徽章，占掉整张卡片却几乎等于"没筛"，信息密度是负的。
    var summaryChips: [SummaryChip] {
        var chips: [SummaryChip] = []
        if let r = maxRent { chips.append(.text("≤ €\(Int(r))/mo")) }
        if let a = minArea { chips.append(.text("≥ \(Int(a)) m²")) }
        if let f = minFloor { chips.append(.text("Floor ≥ \(f)")) }
        if !allowedCities.isEmpty { chips.append(.text(Self.brief(allowedCities))) }
        if !allowedNeighborhoods.isEmpty { chips.append(.text(Self.brief(allowedNeighborhoods))) }
        if !allowedSources.isEmpty {
            if allowedSources.count > 3 {
                chips.append(.platformCount(allowedSources.count))
            } else {
                chips += allowedSources.map { .platform($0) }
            }
        }
        if !allowedEnergy.isEmpty { chips.append(.text("Energy ≥ \(allowedEnergy)")) }
        if !allowedTypes.isEmpty { chips.append(.text(Self.brief(allowedTypes))) }
        if !allowedOccupancy.isEmpty { chips.append(.text(Self.brief(allowedOccupancy, label: "Occupancy"))) }
        if !allowedTenant.isEmpty { chips.append(.text(Self.brief(allowedTenant, label: "Tenant"))) }
        if !allowedContract.isEmpty { chips.append(.text(Self.brief(allowedContract, label: "Contract"))) }
        if !allowedFinishing.isEmpty { chips.append(.text(Self.brief(allowedFinishing))) }
        if !allowedOffer.isEmpty { chips.append(.text(Self.brief(allowedOffer, label: "Offer"))) }
        return chips
    }

    /// 列表压成一段：最多两项，其余记成 "+N"。
    /// 值本身读不出维度的（"Two"、"Indefinite"）由调用方给 `label`。
    ///
    /// 取值走 ``FeatureText/display(_:)`` 统一首字母大写——摘要和筛选页看到的
    /// 必须是同一个写法，否则设置页写着 "Tenant: student only"、点进去列表里
    /// 是 "Student only"，像两个来源。
    static nonisolated func brief(_ values: [String], label: String? = nil) -> String {
        let shown = values.prefix(2).map(FeatureText.display).joined(separator: ", ")
        let rest = values.count - min(2, values.count)
        let body = rest > 0 ? "\(shown) +\(rest)" : shown
        return label.map { "\($0): \(body)" } ?? body
    }

    private static nonisolated func sourceShortText(_ source: String) -> String {
        Platform.shortName(source)
    }
}

/// 设置页摘要里的一枚 chip。
///
/// 平台单独成一档：它有 ``PlatformBadge`` 那套彩色徽章，塞进文本胶囊等于把
/// 已经建立起来的辨识度丢掉。
enum SummaryChip: Hashable, Sendable {
    /// 单个平台，渲染成彩色徽章。关联值是 source key。
    case platform(String)
    /// 选中平台过多时的合并写法。
    case platformCount(Int)
    /// 其余条件，中性胶囊。
    case text(String)
}

/// 已知能耗等级白名单，与后端 ``config.ENERGY_LABELS`` 对齐（优→差排序）。
/// FilterEditView 的 picker 选项。
let energyLabels = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
