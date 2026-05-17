import SwiftUI
import UIKit

/// Listing 详情页。
///
/// 支持两种打开方式（``ListingRoute``）：
/// - ``known(Listing)``：从列表行点入，data 已在手，立即渲染
/// - ``byId(String)``：从推送通知 deep link 进来，只有 id，``.task`` 拉取
///   ``getListing(id:)`` 再渲染；中间显示 ProgressView
///
/// 加载失败（404 / 网络异常）时用 ContentUnavailableView 兜底。
struct ListingDetailView: View {
    let route: ListingRoute

    @State private var listing: Listing?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if let listing {
                content(listing)
            } else if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let err = errorMessage {
                ContentUnavailableView(
                    "Listing Not Available",
                    systemImage: "house.slash",
                    description: Text(err))
            } else {
                Color.clear
            }
        }
        .navigationTitle(listing?.name ?? "Loading…")
        .toolbar {
            // 房源加载好后才显示分享按钮——加载中 / 失败时分享一个空 deep link
            // 没意义。SwiftUI ShareLink 直接调起系统标准 Share Sheet（AirDrop /
            // 信息 / 邮件 / 复制 / 拷贝链接 ...），item 用 h2smonitor:// deep link
            // —— 收件人装了 FlatRadar 点一下就跳到本房源详情；没装的话
            // message 文本里也带了房源摘要 + Holland2Stay 官网 URL 作为兜底。
            if let listing {
                ToolbarItem(placement: .topBarTrailing) {
                    ShareLink(
                        item: deepLink(for: listing),
                        subject: Text(listing.name),
                        message: Text(shareMessage(for: listing)),
                        // 自定义 scheme（h2smonitor://...）系统不会自动抓 OpenGraph
                        // 预览，分享面板默认显示一个灰色占位格子。提供 SharePreview
                        // 让分享面板顶部正确显示房源名 + App 图标。
                        preview: SharePreview(
                            sharePreviewTitle(for: listing),
                            image: Self.sharePreviewIcon
                        )
                    )
                }
            }
        }
        .task { await load() }
    }

    /// `h2smonitor://listing/<id>` —— 跟 FlatRadarApp.handleURL 解析的 scheme/host 一致。
    private func deepLink(for listing: Listing) -> URL {
        URL(string: "h2smonitor://listing/\(listing.id)") ?? URL(string: "h2smonitor://")!
    }

    /// 分享文本：地址 · 价格 · 城市 + 官网链接。
    /// 用 \n 分行，让 iMessage / 邮件 / Notes 等通讯类接收方显示更清晰。
    private func shareMessage(for listing: Listing) -> String {
        var head: [String] = [listing.name]
        if let price = listing.priceRaw, !price.isEmpty { head.append(price) }
        if !listing.city.isEmpty { head.append(listing.city) }
        var lines = [head.joined(separator: " · ")]
        if !listing.url.isEmpty { lines.append(listing.url) }
        return lines.joined(separator: "\n")
    }

    /// Share Sheet 顶部预览的标题——地址 + 价格（如有），比 deep link 字符串
    /// 友好得多。
    private func sharePreviewTitle(for listing: Listing) -> String {
        if let price = listing.priceRaw, !price.isEmpty {
            return "\(listing.name) · \(price)"
        }
        return listing.name
    }

    /// Share Sheet 预览图标 —— 优先用 App 自身图标，让收件人/拷贝面板里有品牌
    /// 识别度；读不到（极少见）退回 SF 房子符号。`static let` 一次加载终生复用。
    private static let sharePreviewIcon: Image = {
        if let ui = loadAppIcon() {
            return Image(uiImage: ui)
        }
        return Image(systemName: "house.fill")
    }()

    /// 从 Info.plist `CFBundleIcons` 取最后一个（最大尺寸）icon 文件名，再用
    /// `UIImage(named:)` 加载。Apple 没有公开 API 直接获取 AppIcon，只能这样绕。
    private static func loadAppIcon() -> UIImage? {
        guard let icons = Bundle.main.infoDictionary?["CFBundleIcons"] as? [String: Any],
              let primary = icons["CFBundlePrimaryIcon"] as? [String: Any],
              let files = primary["CFBundleIconFiles"] as? [String],
              let last = files.last
        else { return nil }
        return UIImage(named: last)
    }

    private func load() async {
        switch route {
        case .known(let l):
            listing = l
        case .byId(let id):
            guard listing == nil else { return }   // 二次进入不重复 fetch
            isLoading = true
            errorMessage = nil
            do {
                listing = try await APIClient.shared.getListing(id: id)
            } catch {
                errorMessage = error.localizedDescription
            }
            isLoading = false
        }
    }

    @ViewBuilder
    private func content(_ listing: Listing) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(listing.name)
                        .font(.title2)
                        .fontWeight(.bold)

                    HStack(spacing: 8) {
                        Label(listing.city, systemImage: "mappin.and.ellipse")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)

                        Text(listing.status)
                            .font(.caption)
                            .fontWeight(.semibold)
                            .lineLimit(1)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(statusColor(for: listing).opacity(0.16))
                            .foregroundStyle(statusColor(for: listing))
                            .clipShape(Capsule())
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    DetailMetricCard(
                        title: "Price",
                        value: listing.priceRaw ?? "Unknown",
                        systemImage: "eurosign.circle")
                        DetailMetricCard(
                            title: "Available",
                            value: listing.availableFrom.map(ServerTime.displayDate) ?? "Unknown",
                            systemImage: "calendar")
                    if let area = listing.areaText {
                        DetailMetricCard(title: "Area", value: area, systemImage: "square.resize")
                    }
                    if let floor = listing.floorText {
                        DetailMetricCard(title: "Floor", value: floor, systemImage: "stairs")
                    }
                }

                if !primaryDetails(for: listing).isEmpty {
                    DetailSection(title: "Key Details") {
                        ForEach(primaryDetails(for: listing), id: \.title) { item in
                            LabeledContent(item.title, value: item.value)
                        }
                    }
                }

                if !secondaryDetails(for: listing).isEmpty {
                    DetailSection(title: "All Details") {
                        ForEach(secondaryDetails(for: listing), id: \.key) { key, value in
                            LabeledContent(displayKey(key), value: value)
                        }
                    }
                } else if !listing.features.isEmpty {
                    DetailSection(title: "Features") {
                        ForEach(listing.features, id: \.self) { feature in
                            Label(feature, systemImage: "checkmark.circle")
                                .font(.subheadline)
                        }
                    }
                }

                if listing.firstSeen != nil || listing.lastSeen != nil {
                    DetailSection(title: "Monitoring") {
                        if let first = listing.firstSeen {
                            LabeledContent("First seen", value: ServerTime.display(first))
                        }
                        if let last = listing.lastSeen {
                            LabeledContent("Last seen", value: ServerTime.display(last))
                        }
                    }
                }

                if let url = URL(string: listing.url), !listing.url.isEmpty {
                    Text("Always verify listing details on the official Holland2Stay website before making decisions.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 8)

                    Link(destination: url) {
                        Label("Open on Holland2Stay", systemImage: "safari")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
            }
            .padding()
        }
    }

    private func primaryDetails(for listing: Listing) -> [DetailItem] {
        let items: [(title: String, value: String?)] = [
            ("Type", listing.typeText),
            ("Contract", listing.contractText),
            ("Energy", listing.energyText),
            ("Available from", listing.availableFrom.map(ServerTime.displayDate))
        ]
        return items.compactMap { item in
            guard let value = item.value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
                return nil
            }
            return DetailItem(title: item.title, value: value)
        }
    }

    private func secondaryDetails(for listing: Listing) -> [(key: String, value: String)] {
        let primaryKeys = Set(["type", "property type", "apartment type", "contract", "rental agreement", "agreement", "energy", "energy label"])
        return listing.featureMap
            .filter { key, value in
                let normalized = key
                    .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
                    .lowercased()
                return !primaryKeys.contains(where: { normalized.contains($0) })
                    && !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
            .sorted { $0.key.localizedCaseInsensitiveCompare($1.key) == .orderedAscending }
    }

    private func displayKey(_ key: String) -> String {
        key
            .replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ")
            .split(separator: " ")
            .map { word in
                let lower = word.lowercased()
                return lower.prefix(1).uppercased() + lower.dropFirst()
            }
            .joined(separator: " ")
    }

    private func statusColor(for listing: Listing) -> Color {
        let s = listing.status.lowercased()
        if s.contains("available to book") { return .green }
        if s.contains("lottery") { return .orange }
        if s.contains("reserved") || s.contains("rented") { return .red }
        return .secondary
    }
}

private struct DetailItem {
    let title: String
    let value: String
}

private struct DetailMetricCard: View {
    let title: String
    let value: String
    let systemImage: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: systemImage)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.headline)
                .lineLimit(2)
                .minimumScaleFactor(0.85)
        }
        .frame(maxWidth: .infinity, minHeight: 74, alignment: .leading)
        .padding(12)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

private struct DetailSection<Content: View>: View {
    let title: String
    let content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title)
                .font(.headline)
            VStack(alignment: .leading, spacing: 8) {
                content
            }
            .font(.subheadline)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
    }
}
