import Foundation

/// From GET /stats/public/charts — array of chart keys
typealias ChartKeysResponse = [String]

/// From GET /stats/public/charts/<key>
struct ChartData: Decodable, Sendable {
    let key: String
    let days: Int
    let data: [ChartEntry]
}

/// 一条图表数据。后端不同图表 key 字段名各异：
///   daily_new / daily_changes → ``date``
///   city_dist                 → ``city``
///   status_dist               → ``status``
///   price_dist / area_dist /
///   floor_dist                → ``range``
///   hourly_dist               → ``hour``
///   tenant_dist / contract_dist
///   / type_dist / energy_dist → ``label``
///
/// 这里用动态 key 解码：除 ``count`` 外，第一个字符串值就是 ``label``。
struct ChartEntry: Decodable, Identifiable, Sendable {
    let label: String
    let count: Int

    var id: String { label }

    private struct DynamicKey: CodingKey {
        let stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: DynamicKey.self)
        var resolvedCount = 0
        var resolvedLabel = ""
        for key in c.allKeys {
            if key.stringValue == "count" {
                resolvedCount = (try? c.decode(Int.self, forKey: key)) ?? 0
            } else if resolvedLabel.isEmpty {
                if let s = try? c.decode(String.self, forKey: key) {
                    resolvedLabel = s
                } else if let i = try? c.decode(Int.self, forKey: key) {
                    resolvedLabel = String(i)
                }
            }
        }
        self.label = resolvedLabel
        self.count = resolvedCount
    }
}
