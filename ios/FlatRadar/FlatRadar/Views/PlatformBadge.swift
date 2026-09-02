import SwiftUI

/// 平台徽标（"H2S" / "OC" / …）——**全 App 唯一一份**。
///
/// 收拢之前，地图弹卡、日历行、房源详情各有一份 `sourceBadge` + `sourceColor`，
/// 三份完全一样，而且都只认三个平台：
///
/// ```swift
/// case "ourdomain": return .purple
/// case "xior":      return .teal
/// default:          return .blue      // ← OurCampus / Magis / SE / Plaza 全在这
/// ```
///
/// 四个平台和 Holland2Stay 同色，等于颜色没有承载任何信息。颜色现在按平台取
/// （见 ``Platform.color``），一个平台在任何页面都是同一个颜色。
///
/// 三处原本只有字号和内边距不同，用 ``Size`` 表达，其余一律共用。
struct PlatformBadge: View {

    enum Size {
        /// 日历行等信息密度高的地方。
        case small
        /// 地图弹卡。
        case medium
        /// 房源详情页头部。
        case large

        var font: CGFloat {
            switch self {
            case .small: return 9
            case .medium: return 10
            case .large: return 11
            }
        }
        var hPadding: CGFloat {
            switch self {
            case .small: return 5
            case .medium: return 6
            case .large: return 8
            }
        }
        var vPadding: CGFloat {
            switch self {
            case .small: return 2
            case .medium: return 2
            case .large: return 4
            }
        }
    }

    let source: String?
    var size: Size = .medium

    var body: some View {
        let color = Platform.color(source)
        Text(Platform.shortName(source))
            .font(.system(size: size.font, weight: .heavy, design: .monospaced))
            .padding(.horizontal, size.hPadding)
            .padding(.vertical, size.vPadding)
            .background(color.opacity(0.16), in: Capsule())
            .foregroundStyle(color)
            // 缩写对读屏软件没有意义，念全名。
            .accessibilityLabel("Platform \(Platform.displayName(source))")
    }
}
