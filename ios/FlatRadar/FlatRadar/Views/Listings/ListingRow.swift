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
    let listing: Listing

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(listing.name)
                        .font(.system(size: 15.5, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(.tail)

                    if listing.isNew, let age = listing.ageText {
                        Text("NEW · \(age)")
                            .font(.system(size: 9, weight: .heavy, design: .monospaced))
                            .tracking(0.5)
                            .foregroundStyle(Color(red: 31/255, green: 128/255, blue: 67/255))
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.green.opacity(0.14),
                                        in: RoundedRectangle(cornerRadius: 4))
                            .fixedSize()
                    }
                }

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

    // MARK: - Pieces

    private var metaText: String {
        var parts: [String] = []
        if !listing.city.isEmpty { parts.append(listing.city) }
        if let area = listing.areaText {
            // areaText may already be "65m²" or just "65"; normalize lightly
            let trimmed = area.trimmingCharacters(in: .whitespacesAndNewlines)
            parts.append(trimmed.lowercased().contains("m") ? trimmed : "\(trimmed)m²")
        }
        if let avail = listing.availableShortText {
            parts.append("from \(avail)")
        }
        return parts.joined(separator: " · ")
    }

    private var priceText: String {
        if let v = listing.priceValue {
            let f = NumberFormatter()
            f.numberStyle = .decimal
            f.locale = Locale(identifier: "en_US")
            f.maximumFractionDigits = 0
            f.usesGroupingSeparator = true
            let n = f.string(from: NSNumber(value: v)) ?? "\(Int(v))"
            return "€\(n)"
        }
        return listing.priceRaw ?? "—"
    }

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
        case .book:     return ("Book", .green)
        case .lottery:  return ("Lottery", .orange)
        case .reserved: return ("Reserved", Color(.systemGray))
        case .other:    return (listing.status, Color(.systemGray))
        }
    }
}
