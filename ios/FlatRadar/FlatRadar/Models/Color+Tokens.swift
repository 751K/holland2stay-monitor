import SwiftUI

/// 全 App 共享的**语义色 token**。每个 token 都在 Assets.xcassets 中配了
/// 亮/暗双值，UIKit / SwiftUI 自动按 traitCollection 切换。
///
/// 设计原则
/// --------
/// - **按语义命名，不按颜色值**：`statusBook` 而非 `green`。这样未来 Holland2Stay
///   重新定义"book"语义颜色时，改 Asset Catalog 一处即可，所有调用方自动跟随。
/// - **跨文件复用才进 token**：屏幕专属的 chrome（如 LoginView hero gradient）
///   保留在原文件里，避免 token 体系膨胀。
/// - **优先复用系统语义色**：能直接用 `Color.accentColor` / `Color(.systemGray)`
///   的就别再造 token。这里只定义 SwiftUI 体系里**没有现成对应**的业务色：
///     - status 是 Holland2Stay 三态业务语义（book/lottery/reserved）
///     - energy 是房源能效等级颜色光谱（A+++ 深绿 → D+ 红，跨多色 hue）
extension Color {
    // MARK: - Status (Holland2Stay 三态业务语义)

    /// "Available to book"——绿色，先到先得状态。
    /// 复用为：listing 状态徽章、notification kind=book 卡片、NEW 标签。
    static let statusBook = Color("Status/Book")

    /// "Available in lottery"——橙色，抽签状态。
    static let statusLottery = Color("Status/Lottery")

    /// "Reserved" / "In Process"——蓝色，**暂时**订不了但可能回来。
    ///
    /// 2026-09-02 由灰改蓝：此前 Reserved 和 Occupied 共用同一个灰，于是「有人
    /// 占着，退订就放出来」和「已经租出去了」在界面上完全一样。原来那套灰保留
    /// 在 ``statusOccupied``，语义没变的调用点应当改指它。
    static let statusReserved = Color("Status/Reserved")

    /// "Occupied" / "Rented" / "Not available"——灰色，**终态**。
    static let statusOccupied = Color("Status/Occupied")

    /// 认不出的状态——紫色。不并进灰色，否则新平台的新状态会跟着终态一起被
    /// 地图筛选默认隐藏，从图上静默消失。见 ``ListingStatus``。
    static let statusUnknown = Color("Status/Unknown")

    // MARK: - Energy label 能效等级光谱

    /// A+++ / A++ —— 最高能效等级，深绿。
    static let energyTop = Color("Energy/Top")

    /// A+ —— Apple system green，同 `statusBook` 但语义不同（这里是能效不是租态）。
    static let energyAPlus = Color("Energy/APlus")

    /// A —— 浅绿/lime，比 A+ 更淡一档。
    static let energyA = Color("Energy/A")
}
