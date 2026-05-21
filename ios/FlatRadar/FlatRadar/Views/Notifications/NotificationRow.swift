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
    /// "增加对比度"开关。开启时把已读卡的 .tertiary 文字提到 .secondary，
    /// 满足 WCAG AA 4.5:1。
    @Environment(\.colorSchemeContrast) private var contrast

    /// 已读状态下"次要"文字（body / 时间）的样式分层。Increase Contrast
    /// 开启时整体上抬一档，避免 .tertiary 在 secondarySystemGroupedBackground
    /// 上仅 ~3.4:1 的低对比。
    private var readSecondaryStyle: HierarchicalShapeStyle {
        contrast == .increased ? .secondary : .tertiary
    }

    /// 事件标签（NEW · BOOK / STATUS CHANGE 等）的前景色。
    /// - 已读：恒用 .secondary 灰
    /// - 未读 + Increase Contrast：.primary（避开 orange/red 在白底上低对比）
    /// - 未读 + 普通对比：style.accent（kind 色，保留色彩识别）
    private func eventLabelStyle(isRead: Bool, accent: Color) -> AnyShapeStyle {
        if isRead { return AnyShapeStyle(Color.secondary) }
        if contrast == .increased { return AnyShapeStyle(Color.primary) }
        return AnyShapeStyle(accent)
    }

    var body: some View {
        let style = CardStyle(kind: notification.kind)
        let isRead = notification.isRead

        // 带 listingID 的行可跳详情；alert/system/test 类没有跳转目标。
        let isTappable = !notification.listingID.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty

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
                        // iOS HIG 最小正文字号 11pt——抬到 11pt 后即使用户开了
                        // 较大 Dynamic Type，缩放下限也不会进可读阈值以下。
                        // tracking 微调到 0.4，跟原本 10.5/0.5 的"紧凑 caps"视觉
                        // 密度保持基本一致。
                        .font(.system(size: 11, weight: .heavy, design: .monospaced))
                        .tracking(0.4)
                        // 未读时用 kind 色（绿/橙/蓝/红）传达类别；已读降到 secondary
                        // 灰。Increase Contrast 开启时：未读改用 .primary——因为
                        // statusLottery 橙色文字在白色 cardTint 上仅 ~3.4:1，违 AA。
                        // 类别色信号已经由左侧 32×32 icon 方块 + cardTint 底色携带，
                        // 文字降级到 .primary 不丢语义、显著提对比。
                        .foregroundStyle(eventLabelStyle(isRead: isRead, accent: style.accent))
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Text(notification.ageText)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(isRead ? readSecondaryStyle : .secondary)
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
                        .foregroundStyle(isRead ? readSecondaryStyle : .secondary)
                        .lineLimit(3)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            // 可跳转的行（new_listing / status_change）右侧加 chevron 提示。
            // alert / system / test 类没 listingID 不画 chevron，避免误导用户
            // "点了好像没反应"。
            if isTappable {
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
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
        // VoiceOver：把整张卡视为单个元素，朗读 event label + title + body + 时间。
        // 不 combine 的话 VO 会分别读 "NEW · BOOK" / 8m / title / body 四段，
        // 用户得多滑几次手势才能读完一条；combine 后一气呵成。
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(a11yLabel)
        .accessibilityHint(isTappable
            ? Text("Double tap to open listing details")
            : Text(""))
        .accessibilityAddTraits(isRead ? [] : [.isStaticText])
    }

    /// 整卡 VoiceOver 朗读：event · title · body · 相对时间。
    /// 顺序贴合视觉扫读顺序：左上 event → 中间 title → body → 右上时间。
    private var a11yLabel: Text {
        let style = CardStyle(kind: notification.kind)
        var parts: [String] = [style.eventLabel.replacingOccurrences(of: " · ", with: ", ")]
        if !notification.title.isEmpty { parts.append(notification.title) }
        if !notification.body.isEmpty { parts.append(notification.body) }
        parts.append(notification.ageText + " ago")
        if notification.isRead { parts.append("Read") }
        return Text(parts.joined(separator: ", "))
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
            accent         = .statusBook
            iconBackground = Color.statusBook.opacity(0.18)
            cardTint       = Color.statusBook.opacity(0.12)
            eventLabel     = "NEW · BOOK"

        case .lottery:
            iconName       = "ticket.fill"
            accent         = .statusLottery
            iconBackground = Color.statusLottery.opacity(0.20)
            cardTint       = Color.statusLottery.opacity(0.12)
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
