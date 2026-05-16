import Foundation

/// `GET /api/v1/calendar` ``listings[]`` 数组的元素。
///
/// 与 ``Listing`` / ``MapListing`` 的区别
/// ------------------------------------
/// CalendarListing 是日历专用 DTO：必含非空 ``availableFrom``（后端 SQL 已
/// `WHERE available_from IS NOT NULL AND != ''`），其它字段稀疏。点击进
/// 详情时走 ``ListingRoute.byId`` 让 ``ListingDetailView`` 自己 fetch 全字段。
struct CalendarListing: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let status: String
    let priceRaw: String
    let availableFrom: String   // ISO yyyy-MM-dd
    let url: String
    let city: String
    let building: String

    enum CodingKeys: String, CodingKey {
        case id, name, status, url, city, building
        case priceRaw = "price_raw"
        case availableFrom = "available_from"
    }

    /// 解析 ``availableFrom`` 为 ``Date``（按服务器 Amsterdam 日期）；解析失败返回 nil。
    var date: Date? { Self.dateFormatter.date(from: availableFrom) }

    /// 用于按"日"分组的 key（YYYY-MM-DD），保证同一天的房源会聚合在一起。
    var dayKey: String { String(availableFrom.prefix(10)) }

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = ServerTime.timeZone
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}

struct CalendarResponse: Decodable, Sendable {
    let listings: [CalendarListing]
}
