import Foundation

/// 平台（source）显示名——**全 App 唯一一份**。
///
/// 为什么单独抽出来
/// ----------------
/// 收拢之前，这份映射在七个文件里各写了一遍：`MapListing`、`CalendarListing`、
/// `Listing`、`ListingFilter`、`ChartData`、`ListingsView`（两处）、
/// `FilterEditView`。七份没有一份是全的——最好的认得 3 个平台，地图那份只认得
/// 2 个，而后端有 7 个。于是 OurCampus / Magis / Student Experience / Plaza
/// 在界面上一路显示成 `OURCAMPUS` `MAGIS` `STUDENTEXPERIENCE` `PLAZA`
/// （把 source key 直接大写）。
///
/// 这不是会崩的那种错，是**每接一个新平台就悄悄多一处**的错：加平台的人改了
/// 后端和一两个显眼的地方，剩下五处要等有人截图才会发现。
///
/// 与后端的对应
/// ------------
/// 与 `static/app.js` 的 `SOURCE_LABELS` / `SOURCE_SHORT`、以及
/// `notifier.py` 的 `_source_short` 对齐。新增平台时三处一起加。
enum Platform {

    /// 全名，用于卡片、徽章、筛选器选项。
    private static let displayNames: [String: String] = [
        "holland2stay": "Holland2Stay",
        "ourdomain": "OurDomain",
        "ourcampus": "OurCampus",
        "xior": "Xior",
        "magis": "Magis",
        "studentexperience": "Student Experience",
        "plaza": "Plaza",
    ]

    /// 缩写，用于图表坐标轴和空间紧张的徽章（放全名会挤成一团）。
    private static let shortNames: [String: String] = [
        "holland2stay": "H2S",
        "ourdomain": "OD",
        "ourcampus": "OC",
        "xior": "XR",
        "magis": "MG",
        "studentexperience": "SE",
        "plaza": "PZ",
    ]

    /// 已登记的平台 key，按显示名排序。
    static var knownKeys: [String] {
        displayNames.keys.sorted { displayNames[$0]! < displayNames[$1]! }
    }

    /// 平台全名。
    ///
    /// 认不出的 key **不套一个默认平台名**——把未知 source 显示成
    /// "Holland2Stay" 会让人以为数据是那边来的。退回一个可读的转写：
    /// `some_new_site` → `Some New Site`。
    static func displayName(_ source: String?) -> String {
        let key = normalize(source)
        if key.isEmpty { return "Platform" }
        return displayNames[key] ?? titleCased(key)
    }

    /// 平台缩写。认不出时退回前三个字母大写，而不是整段大写——
    /// `STUDENTEXPERIENCE` 会把徽章撑变形。
    static func shortName(_ source: String?) -> String {
        let key = normalize(source)
        if key.isEmpty { return "PLT" }
        if let s = shortNames[key] { return s }
        return String(key.prefix(3)).uppercased()
    }

    /// 归一化：去空白、小写。`nil` / 空串返回空串，由调用方决定兜底文案。
    static func normalize(_ source: String?) -> String {
        (source ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func titleCased(_ key: String) -> String {
        key.split { $0 == "_" || $0 == "-" || $0 == " " }
            .map { word in
                let lower = word.lowercased()
                return lower.prefix(1).uppercased() + lower.dropFirst()
            }
            .joined(separator: " ")
    }
}
