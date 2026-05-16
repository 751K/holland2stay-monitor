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

    /// 普通构造器——给 bucketing 合并后造新 entry 用。
    init(label: String, count: Int) {
        self.label = label
        self.count = count
    }

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

extension Array where Element == ChartEntry {
    /// 按 chart key 做语义合并展示——dashboard mini card 和 detail sheet 共用，
    /// 保证两边看到的标签一致。未识别的 key 原样返回。
    ///
    /// - `type_dist`   后端发的是 "1"/"2"/"3"/"4"（数字代表 N-room apt）→ 合并成
    ///                 "Apt"；"studio" / "loft (...)" / "house" 按关键字归类，
    ///                 括号注释剥掉。
    /// - `energy_dist` "A+"/"A++"/"A+++" 合并成 "A+"；"A" 独立一组；
    ///                 B/C/D/E/F/G 原样。
    func bucketed(forKey key: String) -> [ChartEntry] {
        switch key {
        case "type_dist":
            return mergedByBucket { Self.typeBucketLabel($0) }
        case "energy_dist":
            return mergedByBucket(orderedKeys: ["A+", "A", "B", "C", "D", "E", "F", "G"]) {
                Self.energyBucketLabel($0)
            }
        default:
            return self
        }
    }

    /// 按 `bucket` 闭包归桶，保留**首次出现顺序**。
    /// 给 type_dist 用——结果再由调用方按 count desc 排。
    private func mergedByBucket(_ bucket: (String) -> String) -> [ChartEntry] {
        var counts: [String: Int] = [:]
        var order: [String] = []
        for entry in self {
            let key = bucket(entry.label)
            guard !key.isEmpty else { continue }
            if counts[key] == nil { order.append(key) }
            counts[key, default: 0] += entry.count
        }
        return order.compactMap { label in
            guard let c = counts[label] else { return nil }
            return ChartEntry(label: label, count: c)
        }
    }

    /// 按 `bucket` 归桶，并按 `orderedKeys` 给定顺序输出（缺失等级跳过）。
    /// 给 energy_dist 用——A→G 是天然顺序，不按 count 排。
    private func mergedByBucket(orderedKeys: [String], _ bucket: (String) -> String) -> [ChartEntry] {
        var counts: [String: Int] = [:]
        for entry in self {
            let key = bucket(entry.label)
            guard !key.isEmpty else { continue }
            counts[key, default: 0] += entry.count
        }
        return orderedKeys.compactMap { label in
            guard let c = counts[label], c > 0 else { return nil }
            return ChartEntry(label: label, count: c)
        }
    }

    static func typeBucketLabel(_ label: String) -> String {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        // 后端 type_dist 直接发数字 "1"/"2"/"3"/"4"，代表 N-room apt
        if !trimmed.isEmpty, trimmed.allSatisfy(\.isNumber) {
            return "Apt"
        }
        let lower = trimmed.lowercased()
        // 顺序很重要：特定类型先判，否则 "Loft (apartment)" / "Loft apartment"
        // 会被下面 .contains("apartment") 错归到 Apt，把 Loft 整桶吞掉。
        if lower.contains("studio") { return "Studio" }
        if lower.contains("loft")   { return "Loft" }
        if lower.contains("house")  { return "House" }
        if lower.contains("room") || lower.contains("apartment") || lower.hasPrefix("apt") {
            return "Apt"
        }
        // 兜底：剥括号取主标签
        return trimmed.components(separatedBy: "(").first?
            .trimmingCharacters(in: .whitespaces) ?? trimmed
    }

    static func energyBucketLabel(_ label: String) -> String {
        let cleaned = label.uppercased().trimmingCharacters(in: .whitespacesAndNewlines)
        // A+/A++/A+++ → "A+"；纯 "A" → "A"；B/C/D/E/F/G 原样。
        // 注意：A++ 也以 "A" 开头，所以先 check "A+" 前缀再 fallback 到 "A"。
        if cleaned.hasPrefix("A+") { return "A+" }
        if cleaned == "A"          { return "A" }
        for prefix in ["B", "C", "D", "E", "F", "G"] where cleaned.hasPrefix(prefix) {
            return prefix
        }
        return ""
    }
}
