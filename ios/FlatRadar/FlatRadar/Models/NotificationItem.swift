import Foundation

struct NotificationItem: Decodable, Identifiable, Sendable {
    let id: Int
    let createdAt: String
    let type: String
    let title: String
    let body: String
    let url: String
    let listingID: String
    let read: Int

    enum CodingKeys: String, CodingKey {
        case id, type, title, body, url, read
        case createdAt = "created_at"
        case listingID = "listing_id"
    }

    var isRead: Bool { read != 0 }

    func markedRead() -> NotificationItem {
        NotificationItem(id: id, createdAt: createdAt, type: type,
                         title: title, body: body, url: url,
                         listingID: listingID, read: 1)
    }
}

extension NotificationItem {
    /// 通知的语义分类——决定 V2 卡片的颜色 / 图标 / 事件标签。
    ///
    /// - `book`    新房源（available to book）
    /// - `lottery` 新房源（available in lottery）
    /// - `status`  状态变化（reserved ↔ book ↔ lottery）
    /// - `alert`   服务端异常（403 / blocked / 抓取失败）
    /// - `test`    手动触发的测试推送（SSE TEST / Test push）
    /// - `system`  兜底——其它系统消息
    enum Kind {
        case book, lottery, status, alert, test, system
    }

    /// 后端的 `type` 字段写法不统一：new_listing / status_change / error / blocked /
    /// test / sse_test / info / system 都见过。再叠加 title/body 的关键字做兜底
    /// （比如"available in lottery"出现在 body 里就归为 lottery）。
    var kind: Kind {
        let t = type.lowercased().replacingOccurrences(of: "_", with: " ")
        let blob = "\(title) \(body)".lowercased()

        // 显式 test
        if t.contains("test") || blob.contains("sse test") || blob.contains("test push") {
            return .test
        }
        // 服务端异常类
        if t.contains("error") || t.contains("block") || t.contains("alert")
            || t.contains("403") || t.contains("fail") {
            return .alert
        }
        // 状态变化
        if t.contains("status") || t.contains("change") || blob.contains("→") {
            return .status
        }
        // 新房源 — 用 lottery 关键字细分
        if t.contains("new listing") || t.contains("listing") || t.contains("booking") {
            if blob.contains("lottery") || blob.contains("抽签") {
                return .lottery
            }
            return .book
        }
        return .system
    }

    /// 把 `createdAt` 解成 Date —— 复用 listing 那套多格式解析。
    /// 用 Europe/Amsterdam 算相对年龄，避免本地时区漂移。
    var createdDate: Date? {
        let raw = createdAt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }

        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = iso.date(from: raw) { return d }
        iso.formatOptions = [.withInternetDateTime]
        if let d = iso.date(from: raw) { return d }

        let tz = TimeZone(identifier: "Europe/Amsterdam") ?? .current
        let formats = [
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "yyyy-MM-dd'T'HH:mm:ss",
        ]
        for fmt in formats {
            let f = DateFormatter()
            f.calendar = Calendar(identifier: .gregorian)
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = tz
            f.dateFormat = fmt
            if let d = f.date(from: raw) { return d }
        }
        return nil
    }

    /// 相对年龄串：`now` / `38m` / `5h` / `2d`。
    var ageText: String {
        guard let d = createdDate else { return "" }
        let interval = Date().timeIntervalSince(d)
        if interval < 60 { return "now" }
        if interval < 3600 { return "\(Int(interval / 60))m" }
        if interval < 86400 { return "\(Int(interval / 3600))h" }
        if interval < 86400 * 7 { return "\(Int(interval / 86400))d" }
        // 超过一周回退到具体日期
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = TimeZone(identifier: "Europe/Amsterdam") ?? .current
        f.dateFormat = "MMM d"
        return f.string(from: d)
    }

    /// "Today" / "Yesterday" / "Earlier" 三段——给 NotificationsView 做 Section 分组。
    enum DayBucket: String, CaseIterable {
        case today, yesterday, earlier
    }

    var dayBucket: DayBucket {
        guard let d = createdDate else { return .earlier }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Europe/Amsterdam") ?? .current
        if cal.isDateInToday(d) { return .today }
        if cal.isDateInYesterday(d) { return .yesterday }
        return .earlier
    }
}
