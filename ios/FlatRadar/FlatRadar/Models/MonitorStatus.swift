import Foundation

/// From GET /stats/public/summary
///
/// 解码故意写得很宽容 —— 任何字段 null / 缺失 / 类型不一致都退化到默认值
/// 而不是抛错。原因：之前 `let total: Int` 全部 required，只要某个字段是
/// null 整个 envelope decode 就抛 `APIError.decoding`，下拉刷新就显示"连接
/// 失败"。监控仪表盘的数据稍微不准比整页报错好。
struct MonitorStatus: Decodable, Sendable {
    let total: Int
    let new24h: Int
    let new7d: Int
    let changes24h: Int
    let lastScrape: String

    enum CodingKeys: String, CodingKey {
        case total
        case new24h = "new_24h"
        case new7d = "new_7d"
        case changes24h = "changes_24h"
        case lastScrape = "last_scrape"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total       = (try? c.decode(Int.self, forKey: .total))      ?? 0
        new24h      = (try? c.decode(Int.self, forKey: .new24h))     ?? 0
        new7d       = (try? c.decode(Int.self, forKey: .new7d))      ?? 0
        changes24h  = (try? c.decode(Int.self, forKey: .changes24h)) ?? 0
        lastScrape  = (try? c.decode(String.self, forKey: .lastScrape)) ?? ""
    }
}
