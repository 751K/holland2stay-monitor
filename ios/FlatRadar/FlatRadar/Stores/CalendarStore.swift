import Foundation

/// 日历视图状态机：拉取 + 按日分组。
///
/// 月历界面需要"某一天有多少套可入住"的快速查询，原始列表 O(N) 太慢。
/// fetch 完成后立刻 build ``listingsByDay``（dict[yyyy-MM-dd → [Listing]]），
/// 视图渲染日单元格时 O(1) 查 count。
@MainActor
@Observable
final class CalendarStore {
    var listings: [CalendarListing] = []
    var listingsByDay: [String: [CalendarListing]] = [:]
    var isLoading = false
    var errorMessage: String?

    private let client = APIClient.shared

    /// 数据范围：第一个 / 最后一个可入住日期；UI 限制月份切换不超出。
    var dateRange: (start: Date, end: Date)? {
        let dates = listings.compactMap(\.date)
        guard let first = dates.min(), let last = dates.max() else { return nil }
        return (first, last)
    }

    func fetch() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let resp = try await client.getCalendar()
            listings = resp.listings
            listingsByDay = Dictionary(grouping: listings, by: \.dayKey)
        } catch {
            errorMessage = error.localizedDescription
            print("[CalendarStore] fetch error: \(error)")
        }
    }

    func refresh() async {
        await fetch()
    }

    /// 某个日期所属当天的可入住房源列表。
    func listings(on date: Date) -> [CalendarListing] {
        let key = Self.dayKey(for: date)
        return listingsByDay[key] ?? []
    }

    /// 用作 dict key 的 yyyy-MM-dd（与后端 ``available_from`` 前 10 位对齐）。
    static func dayKey(for date: Date) -> String {
        Self.formatter.string(from: date)
    }

    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "UTC")
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
