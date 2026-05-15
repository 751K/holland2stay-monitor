import Foundation

/// From GET /stats/public/summary
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
}
