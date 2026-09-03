import Foundation

/// 把后端的「某维度对哪些平台生效」翻译成一句可读的说明。
///
/// 这条信息此前完全不存在于界面上
/// ------------------------------
/// 后端 ``config._SOURCE_FILTER_DIMS`` 规定：一个过滤维度只对**登记了该维度**
/// 的平台生效，其余平台整条跳过（fail-open，见 ``_source_supports_dim``）。
/// 转置之后是这样的：
///
/// | 维度          | 生效平台数（共 7） |
/// |---------------|-------------------|
/// | Contract      | 1（仅 Holland2Stay） |
/// | Neighborhood  | 1（仅 Holland2Stay） |
/// | Offer         | 1（仅 Holland2Stay） |
/// | Energy        | 2 |
/// | Occupancy     | 3 |
/// | Finishing     | 4 |
/// | Floor         | 5 |
///
/// 用户在筛选表单里勾上 "Contract: Indefinite"，看起来是设了一条全局条件，
/// 实际上只约束了七分之一的房源来源——另外六个平台照推不误。界面上没有一个
/// 字提过这件事，用户只会觉得"筛选没生效"。
///
/// 表本身仍然只有后端一份：``/filter/options`` 的 `dim_sources` 是
/// ``config.filter_dim_sources()`` 的输出。这里只负责措辞，不重抄一份映射——
/// 老 backend 不返回该 key 时 `appliesTo` 为空，退回**不作标注**，
/// 而不是断言"对所有平台生效"。
nonisolated enum PlatformScope {

    struct Note {
        let text: String
        /// 用户勾选的平台里没有一个支持该维度 —— 这条过滤等于没设。
        let isWarning: Bool
    }

    /// - Parameters:
    ///   - appliesTo: 该维度生效的 source key（`FilterOptions.dimSources[dim]`）。
    ///   - selectedSources: 用户在 Platforms 里勾的 source key，空表示不限。
    /// - Returns: 需要提示时给一句话；覆盖全部已知平台或信息缺失时给 `nil`。
    static func note(appliesTo: [String], selectedSources: [String]) -> Note? {
        guard !appliesTo.isEmpty else { return nil }

        let supported = Set(appliesTo)
        // 覆盖了全部已登记平台 —— 没什么可说的。
        if Set(Platform.knownKeys).subtracting(supported).isEmpty { return nil }

        let names = appliesTo
            .map { Platform.displayName($0) }
            .sorted()

        // 用户限定了平台：只讲他选的那几个里有哪些真的会被这条过滤影响。
        if !selectedSources.isEmpty {
            let chosen = Set(selectedSources.map { $0.lowercased() })
            let effective = chosen.intersection(supported)
            if effective.isEmpty {
                return Note(
                    text: "None of the platforms you selected support this filter, "
                        + "so it currently has no effect.",
                    isWarning: true)
            }
            if effective.count < chosen.count {
                let list = effective.map { Platform.displayName($0) }.sorted()
                return Note(
                    text: "Of the platforms you selected, this only filters "
                        + "\(sentenceList(list)). The rest are not affected.",
                    isWarning: false)
            }
            return nil
        }

        return Note(
            text: "Only filters \(sentenceList(names)). "
                + "Listings from other platforms are not affected.",
            isWarning: false)
    }

    /// "A"、"A and B"、"A, B, and C" —— 逗号分隔在只有两项时读起来像漏了词。
    static func sentenceList(_ items: [String]) -> String {
        switch items.count {
        case 0:  return ""
        case 1:  return items[0]
        case 2:  return "\(items[0]) and \(items[1])"
        default: return items.dropLast().joined(separator: ", ") + ", and \(items[items.count - 1])"
        }
    }
}
