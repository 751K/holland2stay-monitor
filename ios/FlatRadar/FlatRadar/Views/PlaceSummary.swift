import Foundation

/// 房源下方那行「地点」文字。
///
/// 为什么需要它
/// ------------
/// 各平台往 city / building / neighborhood 里塞的东西不一致：OurCampus 的
/// `city` 和 `building` 都是 "OurCampus Amsterdam Diemen"，而房源名是
/// "OurCampus Diemen #3250"。照原样并排的结果是同一件事被念三遍：
///
///     OurCampus Diemen #3250
///     OurCampus Amsterdam Diemen · OurCampus Amsterdam Diemen
///
/// 三道过滤：空的去掉、重复的去掉、已经被标题包含的去掉。
///
/// **只有一份实现**：地图弹卡和日历行都用它。此前地图那边修好了、日历那边没有，
/// 正是"同一段逻辑写两遍、只改一处"——这个项目反复出现的形状。
nonisolated enum PlaceSummary {

    /// - Parameters:
    ///   - name: 房源名，用来判断哪些部分是重复的。
    ///   - parts: 候选片段，按想要的先后顺序传入（如 `[building, city]`）。
    /// - Returns: 用 " · " 连接的地点串；没有值得显示的内容时返回 nil。
    static func text(name: String, parts: [String]) -> String? {
        let known = tokenSet(of: name)
        var kept: [String] = []
        var used = known

        for raw in parts {
            // **按词过滤，不是整串比较。**
            //
            // 截图里的实例：标题 "OurCampus Diemen #3250"、片段
            // "OurCampus Amsterdam Diemen"——两串**互不包含**，整串比较会全部
            // 放行，于是同一件事念三遍。按词看就清楚了：OurCampus 和 Diemen
            // 标题里已经有，真正新的只有 Amsterdam。
            let fresh = tokens(of: raw).filter { !used.contains($0.lowercased()) }
            guard !fresh.isEmpty else { continue }
            fresh.forEach { used.insert($0.lowercased()) }
            kept.append(fresh.joined(separator: " "))
        }
        return kept.isEmpty ? nil : kept.joined(separator: " · ")
    }

    /// 切词。跳过纯数字和单字符——门牌号、"#" 之类不承载地点信息，
    /// 拿它们判重只会误伤。
    private static func tokens(of text: String) -> [String] {
        text.split(whereSeparator: { !$0.isLetter && !$0.isNumber })
            .map(String.init)
            .filter { $0.count > 1 && !$0.allSatisfy(\.isNumber) }
    }

    /// 同名重载只差返回类型的话 Swift 解析不出来（"ambiguous use of"），
    /// 所以取两个名字。
    private static func tokenSet(of text: String) -> Set<String> {
        Set(tokens(of: text).map { $0.lowercased() })
    }
}
