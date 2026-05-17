import SwiftUI

/// 紧凑型 listing 行（V1 设计）
///
/// 设计要点
/// --------
/// - 无缩略图（Holland2Stay 不暴露房源照），纯文字布局把密度推到一屏 8+ 行
/// - 标题旁内联 `NEW · 38m` 小徽章——监听类应用最有价值的信号
/// - 价格右对齐 + monospaced bold，扫描列表时价格列对齐美观
/// - 状态徽章 ●Book / ●Lottery / ●Reserved 对齐 Holland2Stay 业务语义
/// - "1 Jan 2050" 占位日期不显示（`Listing.availableShortText` 已过滤）
struct ListingRow: View {
    @Environment(\.horizontalSizeClass) private var hSizeClass

    let listing: Listing

    var body: some View {
        if hSizeClass == .regular {
            // iPad（含 portrait/landscape）都是 .regular，但实际可用宽度差很多。
            // ViewThatFits 按最大→最小依次尝试，picks 第一个能装下的：
            //   regularBody (~854pt min) → iPad landscape / 大屏：4 列详情
            //   mediumBody  (~460pt min) → iPad portrait / mini portrait：单列 +
            //                              名字下加一行 type/energy/move-in 细节
            //   compactBody             → 兜底，iPhone / 极窄分屏窗口
            ViewThatFits(in: .horizontal) {
                regularBody
                mediumBody
                compactBody
            }
        } else {
            compactBody
        }
    }

    private var compactBody: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                titleLine

                if !metaText.isEmpty {
                    Text(metaText)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }

            Spacer(minLength: 6)

            VStack(alignment: .trailing, spacing: 5) {
                Text(priceText)
                    .font(.system(size: 17, weight: .bold, design: .monospaced))
                    .monospacedDigit()
                    .lineLimit(1)
                    .fixedSize()
                statusBadge
            }
        }
        .padding(.vertical, 4)
    }

    private var regularBody: some View {
        HStack(alignment: .center, spacing: 22) {
            VStack(alignment: .leading, spacing: 5) {
                titleLine
                Text(locationText)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(minWidth: 260, maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 18) {
                detailColumn("Area", normalizedAreaText ?? "—")
                detailColumn("Move-in", listing.availableShortText ?? "—")
                detailColumn("Type", listing.typeText ?? listing.contractText ?? "—")
                detailColumn("Energy", listing.energyText ?? listing.floorText ?? "—")
            }
            .frame(width: 430, alignment: .leading)

            VStack(alignment: .trailing, spacing: 6) {
                Text(priceText)
                    .font(.system(size: 17, weight: .bold, design: .monospaced))
                    .monospacedDigit()
                    .lineLimit(1)
                    .fixedSize()
                statusBadge
            }
            .frame(width: 120, alignment: .trailing)
        }
        .padding(.vertical, 8)
    }

    /// iPad portrait 专用中间档：在 compact 的"名字 + 一行 meta"基础上，
    /// 多加一行 sub-meta 把 type / energy / move-in 暴露出来。
    /// minWidth 320 + 110 + 16 ≈ 446pt：iPhone Pro Max 装不下 → 自动走 compact。
    private var mediumBody: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                titleLine
                Text(locationText)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if !detailsText.isEmpty {
                    Text(detailsText)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            .frame(minWidth: 320, maxWidth: .infinity, alignment: .leading)

            VStack(alignment: .trailing, spacing: 6) {
                Text(priceText)
                    .font(.system(size: 17, weight: .bold, design: .monospaced))
                    .monospacedDigit()
                    .lineLimit(1)
                    .fixedSize()
                statusBadge
            }
            .frame(width: 110, alignment: .trailing)
        }
        .padding(.vertical, 6)
    }

    private var detailsText: String {
        var parts: [String] = []
        if let area = normalizedAreaText { parts.append(area) }
        if let type = listing.typeText { parts.append(type) }
        if let energy = listing.energyText { parts.append("Energy \(energy)") }
        if let from = listing.availableShortText { parts.append("from \(from)") }
        return parts.joined(separator: " · ")
    }

    // MARK: - Pieces

    private var titleLine: some View {
        HStack(spacing: 6) {
            Text(listing.name)
                .font(.system(size: 15.5, weight: .semibold))
                .lineLimit(1)
                .truncationMode(.tail)

            if listing.isNew, let age = listing.ageText {
                Text("NEW · \(age)")
                    .font(.system(size: 9, weight: .heavy, design: .monospaced))
                    .tracking(0.5)
                    // 之前硬编码 RGB 没有 dark 变体；改用语义 token，
                    // Asset Catalog 已配好亮/暗双值。底色同色相不同明度。
                    .foregroundStyle(Color.statusBook)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.statusBook.opacity(0.14),
                                in: RoundedRectangle(cornerRadius: 4))
                    .fixedSize()
            }
        }
    }

    private var metaText: String {
        var parts: [String] = []
        if !listing.city.isEmpty { parts.append(listing.city) }
        if let area = normalizedAreaText {
            parts.append(area)
        }
        if let avail = listing.availableShortText {
            parts.append("from \(avail)")
        }
        return parts.joined(separator: " · ")
    }

    private var locationText: String {
        var parts: [String] = []
        if !listing.city.isEmpty { parts.append(listing.city) }
        if let building = listing.buildingText { parts.append(building) }
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
    }

    private var normalizedAreaText: String? {
        guard let area = listing.areaText else { return nil }
        let trimmed = area.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        return trimmed.lowercased().contains("m") ? trimmed : "\(trimmed)m²"
    }

    private func detailColumn(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .tracking(0.5)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(value == "—" ? .tertiary : .secondary)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .frame(width: 88, alignment: .leading)
    }

    private var priceText: String {
        if let v = listing.priceValue {
            // 用 static let 共享 NumberFormatter —— 之前每次 priceText 调用
            // （compact / medium / regular 三种 body 每帧都跑一次）都 new 一
            // 个 NumberFormatter，列表 100 行 + 60fps 滚动 ≈ 6000 实例/秒，
            // 完全没必要。NumberFormatter.string(from:) 本身是线程安全的，
            // 但视图都在 MainActor 上，不存在并发问题。
            let n = Self.priceFormatter.string(from: NSNumber(value: v))
                ?? "\(Int(v))"
            return "€\(n)"
        }
        return listing.priceRaw ?? "—"
    }

    /// 千分位英文逗号 + 整数（`1,067`）格式化器。整个 App 共享一份。
    private static let priceFormatter: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.locale = Locale(identifier: "en_US")
        f.maximumFractionDigits = 0
        f.usesGroupingSeparator = true
        return f
    }()

    @ViewBuilder
    private var statusBadge: some View {
        let info = statusInfo
        HStack(spacing: 5) {
            Circle()
                .fill(info.color)
                .frame(width: 6, height: 6)
            Text(info.label)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(info.color)
        }
        .padding(.leading, 7)
        .padding(.trailing, 9)
        .padding(.vertical, 3)
        .background(info.color.opacity(0.13), in: Capsule())
    }

    private var statusInfo: (label: String, color: Color) {
        switch listing.statusKind {
        case .book:     return ("Book",     .statusBook)
        case .lottery:  return ("Lottery",  .statusLottery)
        case .reserved: return ("Reserved", .statusReserved)
        case .other:    return (listing.status, .statusReserved)
        }
    }
}
