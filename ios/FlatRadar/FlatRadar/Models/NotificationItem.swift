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
    /// Decode 时计算一次，后续访问 O(1)，避免每次 filter 都重复做 lowercased + contains。
    let kind: Kind

    enum CodingKeys: String, CodingKey {
        case id, type, title, body, url, read
        case createdAt = "created_at"
        case listingID = "listing_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        createdAt = try c.decode(String.self, forKey: .createdAt)
        type = try c.decode(String.self, forKey: .type)
        title = try c.decode(String.self, forKey: .title)
        body = try c.decode(String.self, forKey: .body)
        url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
        listingID = try c.decodeIfPresent(String.self, forKey: .listingID) ?? ""
        read = try c.decodeIfPresent(Int.self, forKey: .read) ?? 0
        kind = Self.classifyKind(type: type, title: title, body: body)
    }

    /// 用于 markedRead() / 测试构造的手动 init
    init(id: Int, createdAt: String, type: String, title: String,
         body: String, url: String, listingID: String, read: Int) {
        self.id = id
        self.createdAt = createdAt
        self.type = type
        self.title = title
        self.body = body
        self.url = url
        self.listingID = listingID
        self.read = read
        self.kind = Self.classifyKind(type: type, title: title, body: body)
    }

    var isRead: Bool { read != 0 }

    func markedRead() -> NotificationItem {
        NotificationItem(id: id, createdAt: createdAt, type: type,
                         title: title, body: body, url: url,
                         listingID: listingID, read: 1)
    }

    var listingTitleHint: String {
        let separators = ["：", ":"]
        var value = title
        for sep in separators {
            if let range = value.range(of: sep) {
                value = String(value[range.upperBound...])
                break
            }
        }
        value = value.replacingOccurrences(
            of: #"^\s*(?:[^\p{L}\p{N}\[]+\s*)?(?:\[[^\]]+\]\s*)?"#,
            with: "",
            options: .regularExpression)
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
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
    ///
    /// 这是静态方法，decode 时由 ``init(from:)`` 调用一次存入 ``kind`` 存储属性，
    /// 之后所有 filter / group 操作都是 O(1) struct field read。
    static func classifyKind(type: String, title: String, body: String) -> Kind {
        let t = type.lowercased().replacingOccurrences(of: "_", with: " ")
        let blob = "\(title) \(body)".lowercased()

        // 显式 test（含中文测试推送）
        if t.contains("test") || blob.contains("sse test") || blob.contains("test push")
            || blob.contains("🧪") || blob.contains("测试推送") || blob.contains("推送链路") {
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

    // MARK: - 共享日期格式化器（static，避免每次访问重新分配）
    //
    // DateFormatter / ISO8601DateFormatter 的创建是已知最贵的 Foundation
    // 操作之一（~100–200μs）。createdDate 是计算属性，ageText / dayBucket
    // 都会调它——之前每次访问都现 new 1 个 ISO + 最多 4 个 DateFormatter。
    // 列表滚动时每行每帧重复分配，开销显著。
    //
    // 改成 static let 一次性建好复用。DateFormatter / ISO8601DateFormatter
    // 自 iOS 7 起对并发"解析/格式化"是线程安全的（只读不改 options），所以
    // 全局共享安全。注意：ISO 拆成两个（含/不含小数秒），避免运行时改
    // formatOptions（那会破坏共享）。

    fileprivate static let amsterdamTZ: TimeZone =
        TimeZone(identifier: "Europe/Amsterdam") ?? .current

    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    /// 多格式兜底解析器（Europe/Amsterdam，en_US_POSIX 固定 locale）。
    private static let fallbackParsers: [DateFormatter] = {
        ["yyyy-MM-dd HH:mm:ss",
         "yyyy-MM-dd HH:mm",
         "yyyy-MM-dd'T'HH:mm:ss.SSS",
         "yyyy-MM-dd'T'HH:mm:ss"].map { fmt in
            let f = DateFormatter()
            f.calendar = Calendar(identifier: .gregorian)
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = amsterdamTZ
            f.dateFormat = fmt
            return f
        }
    }()

    /// "超过一周"时显示的具体日期（"MMM d"，跟随系统 locale）。
    private static let shortDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = .autoupdatingCurrent
        f.timeZone = amsterdamTZ
        f.dateFormat = "MMM d"
        return f
    }()

    /// 把 `createdAt` 解成 Date —— 复用 listing 那套多格式解析。
    /// 用 Europe/Amsterdam 算相对年龄，避免本地时区漂移。
    var createdDate: Date? {
        let raw = createdAt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }
        if let d = Self.isoFractional.date(from: raw) { return d }
        if let d = Self.isoPlain.date(from: raw) { return d }
        for f in Self.fallbackParsers {
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
        // 超过一周回退到具体日期（共享 formatter）
        return Self.shortDateFormatter.string(from: d)
    }

    /// "Today" / "Yesterday" / "Earlier" 三段——给 NotificationsView 做 Section 分组。
    enum DayBucket: String, CaseIterable {
        case today, yesterday, earlier
    }

    private static let amsterdamCalendar: Calendar = {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = amsterdamTZ
        return cal
    }()

    var dayBucket: DayBucket {
        guard let d = createdDate else { return .earlier }
        let cal = Self.amsterdamCalendar
        if cal.isDateInToday(d) { return .today }
        if cal.isDateInYesterday(d) { return .yesterday }
        return .earlier
    }
}
