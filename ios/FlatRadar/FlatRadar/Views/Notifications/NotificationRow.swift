import SwiftUI

/// V2 · 卡片式通知行
///
/// 设计要点
/// --------
/// - 圆角 16 卡片，未读时浅色 tint 背景（绿/橙/蓝/灰），已读自动 0.72 opacity
/// - 左侧 32×32 圆角图标方块（颜色随 kind 变化）
/// - 头部 mono caps 事件标签 + 右上角相对年龄（`8m` / `2h` / `1d`）
/// - 主行：notification.title（未读 .bold / 已读 .semibold）
/// - 副行：notification.body — 价格 + 入住 / Reserved → Book / 系统消息正文
struct NotificationRow: View {
    let notification: NotificationItem

    var body: some View {
        let style = CardStyle(kind: notification.kind)
        let isRead = notification.isRead

        HStack(alignment: .top, spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(style.iconBackground)
                Image(systemName: style.iconName)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(style.accent)
            }
            .frame(width: 32, height: 32)
            // 已读时图标方块也跟着降低一档可见度（不是降透明度，是降图标饱和度）
            .opacity(isRead ? 0.55 : 1)

            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(style.eventLabel)
                        .font(.system(size: 10.5, weight: .heavy, design: .monospaced))
                        .tracking(0.5)
                        // 已读时 mono caps 标签从 kind 色降到 .secondary（系统灰）
                        .foregroundStyle(isRead ? Color.secondary : style.accent)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Text(notification.ageText)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(isRead ? .tertiary : .secondary)
                        .fixedSize()
                }

                if !notification.title.isEmpty {
                    Text(notification.title)
                        .font(.system(size: 15,
                                      weight: isRead ? .regular : .bold))
                        // 关键：已读用 .secondary，不用 .opacity()，避免 dark 模式糊掉
                        .foregroundStyle(isRead ? .secondary : .primary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !notification.body.isEmpty {
                    Text(notification.body)
                        .font(.system(size: 12.5))
                        .foregroundStyle(isRead ? .tertiary : .secondary)
                        .lineLimit(3)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(14)
        .background(
            // 已读用基础卡面（secondarySystemGroupedBackground），未读再叠 kind 色 tint。
            // 不再用 .opacity() 抹整张卡——dark 模式下那样做会让卡片透出黑底，
            // 文字也跟着掉到 72% 白，可读性差。改成靠文字层级（primary→secondary→tertiary）
            // 区分已读/未读，纯色字在两个模式下都清晰。
            ZStack {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color(.secondarySystemGroupedBackground))
                if !isRead {
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(style.cardTint)
                }
            }
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
        )
        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

// MARK: - Card style per kind

private struct CardStyle {
    let iconName: String
    /// 主色 —— mono caps 事件标签 + 图标颜色。
    /// 用 SwiftUI 语义色（.green / .orange / .blue / .red / .purple），系统会按
    /// 亮/暗模式自动切到合适的饱和度（亮模式偏深，暗模式偏亮），
    /// 不会出现深色模式下深绿/深橙糊掉的问题。
    let accent: Color
    /// 32×32 图标方块底色——叠在卡面上的半透明 tint。
    let iconBackground: Color
    /// 未读卡的 kind 色 tint（叠在 secondarySystemGroupedBackground 之上）。
    let cardTint: Color
    let eventLabel: String

    init(kind: NotificationItem.Kind) {
        switch kind {
        case .book:
            iconName       = "house.fill"
            accent         = .green
            iconBackground = Color.green.opacity(0.18)
            cardTint       = Color.green.opacity(0.12)
            eventLabel     = "NEW · BOOK"

        case .lottery:
            iconName       = "ticket.fill"
            accent         = .orange
            iconBackground = Color.orange.opacity(0.20)
            cardTint       = Color.orange.opacity(0.12)
            eventLabel     = "NEW · LOTTERY"

        case .status:
            iconName       = "arrow.triangle.2.circlepath"
            accent         = .blue
            iconBackground = Color.blue.opacity(0.18)
            cardTint       = Color.blue.opacity(0.10)
            eventLabel     = "STATUS CHANGE"

        case .alert:
            iconName       = "exclamationmark.triangle.fill"
            accent         = .red
            iconBackground = Color.red.opacity(0.18)
            cardTint       = Color.red.opacity(0.12)
            eventLabel     = "ALERT"

        case .test:
            iconName       = "wand.and.stars"
            accent         = .blue
            iconBackground = Color.blue.opacity(0.18)
            cardTint       = Color.blue.opacity(0.12)
            eventLabel     = "TEST"

        case .system:
            iconName       = "info.circle.fill"
            accent         = Color(.secondaryLabel)
            iconBackground = Color(.systemFill)
            cardTint       = Color.clear      // 系统消息不再额外加 tint，靠基础卡面即可
            eventLabel     = "SYSTEM"
        }
    }
}
