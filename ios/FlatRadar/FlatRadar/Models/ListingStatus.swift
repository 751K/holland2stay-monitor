import SwiftUI

/// 房源状态的五档归一——**全 App 唯一一份判据**。
///
/// 与后端的对应
/// ------------
/// 判据抄自 `app/jinja_filters.py` 的 `status_capsule` 与 `static/app.js` 的
/// `window.statusBucket`，颜色与 Web 地图一致。后端加新状态时三处一起改。
///
/// 为什么 Reserved 和 Occupied 必须分开
/// ------------------------------------
/// 一个可能回来（有人退订就重新放出），一个永远不会。地图此前把两者一起丢进
/// `.blue` 兜底分支——既和 App 其它页面的灰色不一致，又让「暂时没了」和「彻底
/// 没了」长得一模一样。
///
/// 为什么认不出的状态要单独一档
/// ----------------------------
/// 归进 `.occupied` 的话，它会跟着 occupied 一起被地图筛选默认隐藏，于是**新平台
/// 冒出的新状态会从地图上静默消失**。这正是这个项目反复出现的那个形状：把
/// 「不知道」当成一个确定的答案。`.other` 默认显示，且只在真的出现时才占一个位置。
enum ListingStatus: String, CaseIterable, Identifiable, Sendable {
    case book
    case lottery
    case reserved
    case other
    case occupied

    var id: String { rawValue }

    /// 从后端返回的原始状态串归一。
    ///
    /// 后端写法不止一种（`Available to book` / `available_to_book` /
    /// `Available in lottery` / `Reserved` / `In Process` / `Occupied` /
    /// `Rented out` / `Not available`），一律按**子串**判。
    static func from(_ raw: String?) -> ListingStatus {
        let s = (raw ?? "").lowercased().replacingOccurrences(of: "_", with: " ")
        if s.contains("lottery") { return .lottery }
        if s.contains("book") { return .book }
        if s.contains("reserved") || s.contains("in process") || s.contains("pending") {
            return .reserved
        }
        if s.contains("occupied") || s.contains("rented") || s.contains("not available") {
            return .occupied
        }
        return .other
    }

    var color: Color {
        switch self {
        case .book:     return .statusBook
        case .lottery:  return .statusLottery
        case .reserved: return .statusReserved
        case .other:    return .statusUnknown
        case .occupied: return .statusOccupied
        }
    }

    var label: String {
        switch self {
        case .book:     return String(localized: "Direct book")
        case .lottery:  return String(localized: "Lottery")
        case .reserved: return String(localized: "Reserved")
        case .other:    return String(localized: "Unknown status")
        case .occupied: return String(localized: "Occupied")
        }
    }

    /// 地图筛选栏里这一档是否默认打开。
    ///
    /// 生产实测：地图上 235 条里 Occupied 117 条、Reserved 48 条——七成是租不到的。
    /// 默认全开等于把能租的那三成淹掉。`.other` 默认开着的理由见类型注释。
    var isOnByDefault: Bool {
        switch self {
        case .book, .lottery, .other: return true
        case .reserved, .occupied:    return false
        }
    }

    /// 聚合气泡取簇内「最值得看」的一档，顺序即优先级。
    static let byPriority: [ListingStatus] = [.book, .lottery, .reserved, .other, .occupied]

    var priority: Int { Self.byPriority.firstIndex(of: self) ?? Self.byPriority.count }
}
