import Foundation

/// 后端 feature 值的显示形态 —— **全 App 唯一一份**。
///
/// 后端把 feature 的值原样透出来，大小写全看各平台怎么写的：筛选页的 Tenant
/// 一屏上并排着 `student only` / `custom` / `employed only`，而同一个值在别处
/// 又写作 `Students only`。看着像没做完。
///
/// 这份逻辑原本长在 ``ListingDetailView.displayValue`` 里，是那个视图的私有方法。
/// 筛选页要用同样的规则，抄一份就会变成两份各自演化的实现——本项目已经在平台
/// 显示名上吃过一次亏（七个文件七份映射，没有一份是全的）。
nonisolated enum FeatureText {

    /// 统一首字母大写，但避开两类会被改坏的值。
    ///
    /// - 已登记的平台走 ``Platform.displayName``：机械地首字母大写会得到
    ///   "Ourcampus"，那既不是原样也不是正确写法，比不改还糟。
    /// - 首字母**不是小写**的一律原样返回：值里常有 "m²"、"excl."、"XC"、
    ///   "1-Bedroom" 这类写法，整串 title case 会把它们改坏。
    static func display(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first else { return trimmed }
        if Platform.knownKeys.contains(trimmed.lowercased()) {
            return Platform.displayName(trimmed)
        }
        guard first.isLowercase else { return trimmed }
        return first.uppercased() + trimmed.dropFirst()
    }
}
