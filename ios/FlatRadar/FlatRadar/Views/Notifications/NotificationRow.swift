import SwiftUI

/// V3 · 与 Dashboard / Browse 视觉语言对齐的 Alerts 行
///
/// 设计要点（来自 Claude Design "FlatRadar Alerts.html" V1）
/// ----------------------------------------------------------
/// - 没有独立 card 阴影：每行只是 inset list row，由外层 List section 的
///   白色大圆角容器统一包裹（与 Browse "NEW TODAY" 容器同款）
/// - 左侧 8pt **小色点**（type 指示 + 未读锚点），未读时叠 halo 圈光晕；
///   已读时降到 0.45 透明。**没有 32×32 彩色 icon tile**——那是 V2 的设计
/// - 事件 mono caps 标签（NEW · BOOK / STATUS CHANGE / SYSTEM）+ kind 色
/// - title 15pt 半粗（未读 .bold / 已读 .semibold）
/// - meta line per kind：status 用 `from → to · €1,118/mo`，
///   book/lottery 用 `€1,584/mo · from May 14`，system 用 body 原文
/// - 右侧 mono 时间 + chevron（仅可跳转的行有 chevron）
struct NotificationRow: View {
    let notification: NotificationItem
    /// Increase Contrast: 已读卡的 .tertiary 文字提到 .secondary，满足 AA。
    @Environment(\.colorSchemeContrast) private var contrast

    var body: some View {
        let style = TypeSpec(kind: notification.kind)
        let isRead = notification.isRead
        let isTappable = !notification.listingID.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty

        HStack(alignment: .top, spacing: 12) {
            // Middle: event pill / title / meta（移除了左侧小色点——类型颜色信号
            // 改由事件标签本身的有色胶囊承担，视觉更紧凑、不再有"两个视觉重心"）
            VStack(alignment: .leading, spacing: 5) {
                eventPill(label: style.label, color: style.color, isRead: isRead)

                Text(displayTitle)
                    .font(.system(size: 15, weight: isRead ? .semibold : .bold))
                    .foregroundStyle(isRead ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.tail)

                metaLine(isRead: isRead)
            }

            Spacer(minLength: 8)

            // Right: time + chevron
            VStack(alignment: .trailing, spacing: 8) {
                Text(notification.ageText)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .foregroundStyle(isRead ? readSecondaryStyle : .secondary)
                    .fixedSize()
                if isTappable {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.top, 4)
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        // VoiceOver: 整行合并，避免分多段朗读断节奏
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(a11yLabel)
        .accessibilityHint(isTappable
            ? Text("Double tap to open listing details")
            : Text(""))
    }

    // MARK: - Pieces

    /// 事件类型胶囊：mono caps 文字 + 同色 12% tint 底 + 1pt stroke。
    /// 与 Live pill / Browse 状态药丸视觉语言一致，类型颜色由 tint + 文字色
    /// 双重表达，未读时饱和、已读时降至 secondary 灰。
    @ViewBuilder
    private func eventPill(label: String, color: Color, isRead: Bool) -> some View {
        let textColor: AnyShapeStyle = {
            if isRead { return AnyShapeStyle(Color.secondary) }
            if contrast == .increased { return AnyShapeStyle(Color.primary) }
            return AnyShapeStyle(color)
        }()
        let bgColor = isRead
            ? Color(.systemFill).opacity(0.5)
            : color.opacity(0.12)
        let strokeColor = isRead
            ? Color.secondary.opacity(0.15)
            : color.opacity(0.30)

        Text(label)
            .font(.system(size: 10, weight: .heavy, design: .monospaced))
            .tracking(0.7)
            .foregroundStyle(textColor)
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(bgColor, in: Capsule())
            .overlay(Capsule().strokeBorder(strokeColor, lineWidth: 0.5))
    }

    @ViewBuilder
    private func metaLine(isRead: Bool) -> some View {
        let bodyText = notification.body.trimmingCharacters(in: .whitespacesAndNewlines)
        if let transition = statusTransition {
            // status change 用 "Book → Reserved · €1,118/mo" 文字化形式
            statusTransitionView(transition, isRead: isRead)
        } else if !bodyText.isEmpty {
            Text(bodyText)
                .font(.system(size: 12.5))
                .foregroundStyle(isRead ? readSecondaryStyle : .secondary)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// 解析 status change body 里的 "Book → Reserved · €1,118/mo" 形态。
    /// 后端 notifier 现已发这种格式（含 "→" 箭头）；解析失败回退到原 body 直显。
    private var statusTransition: (from: String, to: String, tail: String)? {
        guard notification.kind == .status else { return nil }
        let body = notification.body
        // 找 "→" 或 ASCII "->"
        let arrow: String
        if body.contains("→") { arrow = "→" }
        else if body.contains("->") { arrow = "->" }
        else { return nil }
        let parts = body.components(separatedBy: arrow)
        guard parts.count >= 2 else { return nil }
        let from = parts[0].trimmingCharacters(in: .whitespaces)
        let rest = parts.dropFirst().joined(separator: arrow)
        // "Reserved · €1,118/mo" → 切首个 "·" 或 "," 分出 to / tail
        let toAndTail = rest.split(
            separator: "·",
            maxSplits: 1,
            omittingEmptySubsequences: false
        )
        let to: String
        let tail: String
        if toAndTail.count == 2 {
            to = toAndTail[0].trimmingCharacters(in: .whitespaces)
            tail = toAndTail[1].trimmingCharacters(in: .whitespaces)
        } else {
            to = rest.trimmingCharacters(in: .whitespaces)
            tail = ""
        }
        return (from, to, tail)
    }

    @ViewBuilder
    private func statusTransitionView(
        _ t: (from: String, to: String, tail: String),
        isRead: Bool
    ) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            Text(t.from)
                .font(.system(size: 12.5, weight: .semibold))
                .foregroundStyle(statusColor(for: t.from, isRead: isRead))
            Image(systemName: "arrow.right")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(.tertiary)
            Text(t.to)
                .font(.system(size: 12.5, weight: .bold))
                .foregroundStyle(statusColor(for: t.to, isRead: isRead))
            if !t.tail.isEmpty {
                Text("·  \(t.tail)")
                    .font(.system(size: 12.5))
                    .foregroundStyle(isRead ? readSecondaryStyle : .secondary)
                    .lineLimit(1)
            }
        }
    }

    /// "Book" / "Reserved" / "Occupied" / "Lottery" → 颜色映射
    private func statusColor(for raw: String, isRead: Bool) -> Color {
        if isRead { return .secondary }
        let s = raw.lowercased()
        if s.contains("book")     { return .statusBook }
        if s.contains("lottery")  { return .statusLottery }
        if s.contains("reserved") { return .statusReserved }
        if s.contains("occupied") { return .statusReserved }
        return .primary
    }

    // MARK: - Styles

    /// 显示用 title：优先 listingTitleHint（去掉 emoji/前缀的纯地址），
    /// 拿不到时回退到原 title。
    private var displayTitle: String {
        let hint = notification.listingTitleHint
        return hint.isEmpty ? notification.title : hint
    }

    private var readSecondaryStyle: HierarchicalShapeStyle {
        contrast == .increased ? .secondary : .tertiary
    }

    private func eventLabelStyle(isRead: Bool, accent: Color) -> AnyShapeStyle {
        if isRead { return AnyShapeStyle(Color.secondary) }
        if contrast == .increased { return AnyShapeStyle(Color.primary) }
        return AnyShapeStyle(accent)
    }

    private var a11yLabel: Text {
        let style = TypeSpec(kind: notification.kind)
        var parts: [String] = [style.label.replacingOccurrences(of: " · ", with: ", ")]
        parts.append(displayTitle)
        let bodyText = notification.body.trimmingCharacters(in: .whitespacesAndNewlines)
        if !bodyText.isEmpty { parts.append(bodyText) }
        parts.append(notification.ageText + " ago")
        if notification.isRead { parts.append("Read") }
        return Text(parts.joined(separator: ", "))
    }
}

// MARK: - Type → 颜色 + 标签

/// 单一 source of truth：通知类型 → 显示颜色 + 标签。
/// 与 Color+Tokens.swift 里的 status 颜色（Asset Catalog）对齐，
/// dark mode 自动切色相不糊。
private struct TypeSpec {
    let label: String
    /// mono caps 事件标签 + 左侧小色点用同一颜色
    let color: Color
    let dot: Color

    init(kind: NotificationItem.Kind) {
        switch kind {
        case .book:
            label = "NEW · BOOK"
            color = .statusBook
            dot   = .statusBook
        case .lottery:
            label = "NEW · LOTTERY"
            color = .statusLottery
            dot   = .statusLottery
        case .status:
            label = "STATUS CHANGE"
            color = .blue
            dot   = .blue
        case .alert:
            label = "ALERT"
            color = .red
            dot   = .red
        case .test:
            label = "TEST"
            color = .blue
            dot   = .blue
        case .system:
            label = "SYSTEM"
            color = Color(.secondaryLabel)
            dot   = Color(.tertiaryLabel)
        }
    }
}
