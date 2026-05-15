import Foundation

/// From GET /stats/public/charts — array of chart keys
typealias ChartKeysResponse = [String]

/// From GET /stats/public/charts/<key>
struct ChartData: Decodable, Sendable {
    let key: String
    let days: Int
    let data: [ChartEntry]
}

struct ChartEntry: Decodable, Identifiable, Sendable {
    let label: String?
    let date: String?
    let count: Int

    var id: String { label ?? date ?? UUID().uuidString }
}
