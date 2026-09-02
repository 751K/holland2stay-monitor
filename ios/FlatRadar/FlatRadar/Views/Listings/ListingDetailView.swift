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

    @Environment(NavigationCoordinator.self) private var coord

    @State private var listing: Listing?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if let listing {
                content(listing)
            } else if isLoading {
                loadingContent
            } else if let err = errorMessage {
                ContentUnavailableView(
                    "Listing Not Available",
                    systemImage: "house.slash",
                    description: Text(err))
            } else {
                Color.clear
            }
        }
        .navigationTitle(navigationTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // 房源加载好后才显示分享按钮——加载中 / 失败时分享一个空 deep link
            // 没意义。SwiftUI ShareLink 直接调起系统标准 Share Sheet（AirDrop /
            // 信息 / 邮件 / 复制 / 拷贝链接 ...），item 用 h2smonitor:// deep link
            // —— 收件人装了 FlatRadar 点一下就跳到本房源详情；没装的话
            // message 文本里也带了房源摘要 + 官方平台 URL 作为兜底。
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

    private var titleHint: String? {
        switch route {
        case .known(let l):
            return l.name
        case .byId(_, let hint):
            let clean = (hint ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return clean.isEmpty ? nil : clean
        }
    }

    private var navigationTitle: String {
        listing?.name ?? titleHint ?? "Listing"
    }

    private var loadingContent: some View {
        VStack {
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// `h2smonitor://listing/<id>` —— 跟 FlatRadarApp.handleURL 解析的 scheme/host 一致。
    private func deepLink(for listing: Listing) -> URL {
        URL(string: "h2smonitor://listing/\(listing.id)") ?? URL(string: "h2smonitor://")!
    }

    /// 分享文本：地址 · 价格 · 城市 + 官网链接。
    /// 用 \n 分行，让 iMessage / 邮件 / Notes 等通讯类接收方显示更清晰。
    private func shareMessage(for listing: Listing) -> String {
        var head: [String] = [listing.sourceShortText, listing.name]
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
            withoutImplicitAnimation {
                listing = l
                isLoading = false
                errorMessage = nil
            }
        case .byId(let id, _):
            guard listing == nil else { return }   // 二次进入不重复 fetch
            withoutImplicitAnimation {
                isLoading = true
                errorMessage = nil
            }
            do {
                let fetched = try await APIClient.shared.getListing(id: id)
                withoutImplicitAnimation {
                    listing = fetched
                    isLoading = false
                }
            } catch {
                withoutImplicitAnimation {
                    errorMessage = error.localizedDescription
                    isLoading = false
                }
            }
        }
    }

    private func withoutImplicitAnimation(_ updates: () -> Void) {
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) {
            updates()
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
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)

                    HStack(spacing: 8) {
                        PlatformBadge(source: listing.normalizedSourceKey, size: .large)

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
                            LabeledContent(displayKey(key), value: displayValue(value))
                                // 地址是这一屏唯一会被拷去别处用的东西（发给中介、
                                // 贴进别的地图、查通勤）。长按复制。
                                //
                                // 每一行都挂：哪一行"值得复制"是用户说了算，
                                // 只给地址开的话，其余行长按没反应反而像坏了。
                                .contextMenu {
                                    Button {
                                        UIPasteboard.general.string = value
                                    } label: {
                                        Label("Copy", systemImage: "doc.on.doc")
                                    }
                                }
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
                    Text("Always verify listing details on the official platform website before making decisions.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 8)

                    Link(destination: url) {
                        Label("Open on \(listing.sourceDisplayText)", systemImage: "safari")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }

                viewOnMapButton(for: listing)
            }
            .padding()
        }
    }

    /// 「在地图上查看」。
    ///
    /// 此前详情页只有一个出口：跳到平台官网的预订页。想知道这套房在城里的哪个
    /// 位置，得自己退回去开地图、再在几百个图钉里找。地图 → 详情这一向早就有
    /// （弹卡上的 View Details），反过来一直是缺的。
    ///
    /// 不在这里直接调 MapStore：点下去的这一刻地图视图可能还没挂载（iPhone 上
    /// 它在 Browse 的另一个模式里）。交给 coordinator 挂一个待办，MapView 出现
    /// 时自取。定位不到时由地图那边说明是哪一种「看不到」。
    @ViewBuilder
    private func viewOnMapButton(for listing: Listing) -> some View {
        Button {
            coord.openMap(focusing: listing.id)
        } label: {
            // 字号跟上面那颗「Open on …」一致（.headline）。两颗按钮上下叠着、
            // 宽度一样，字号却差一档时，看着像其中一颗没做完。
            // 层级差别交给按钮样式表达：主操作 borderedProminent，这颗 bordered。
            Label("View on map", systemImage: "map")
                .font(.headline)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
    }

    private func primaryDetails(for listing: Listing) -> [DetailItem] {
        let items: [(title: String, value: String?)] = [
            ("Type", listing.typeText),
            ("Platform", listing.sourceDisplayText),
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
            .filter { !Self.isRedundant(value: $0.value, on: listing) }
            .sorted { $0.key.localizedCaseInsensitiveCompare($1.key) == .orderedAscending }
    }

    /// 这一行说的事，本屏别处是否已经写过。
    ///
    /// 起因是 `Detail: ourcampus`——值就是平台名，而平台徽标就在标题旁边。
    /// 这类行不是错的，是**没有信息**：占一行、让人扫一遍、什么也没多知道。
    ///
    /// 判据刻意用**完全相等**（去空白 + 忽略大小写），不用包含匹配。包含匹配
    /// 会把 "Building: OurCampus Amsterdam Diemen Tower B" 这种真有增量的行
    /// 一起删掉——宁可漏删几行冗余，也不能删掉用户唯一能看到那条信息的地方。
    private static func isRedundant(value: String, on listing: Listing) -> Bool {
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !v.isEmpty else { return true }

        // 平台名：标题旁边就是平台徽标
        if Platform.knownKeys.contains(v) { return true }
        if v == Platform.displayName(listing.normalizedSourceKey).lowercased() { return true }
        // 状态：标题旁边就是状态徽标
        if v == listing.status.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
            return true
        }
        // 房源名 / 城市：标题和它下面那行就是
        if v == listing.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
            return true
        }
        if v == listing.city.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
            return true
        }
        return false
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

    /// 值的显示形态：统一首字母大写。
    ///
    /// 后端把 feature 的值原样透出来，大小写全看各平台怎么写的——同一屏上
    /// "One"、"student only"、"ourcampus" 三种风格并排，看着像没做完。
    ///
    /// 已登记的平台走 ``Platform.displayName``，因为机械地首字母大写会得到
    /// "Ourcampus"——那既不是原样也不是正确写法，比不改还糟。
    private func displayValue(_ value: String) -> String {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first else { return trimmed }
        if Platform.knownKeys.contains(trimmed.lowercased()) {
            return Platform.displayName(trimmed)
        }
        // 只动第一个字母：值里常有 "m²"、"excl."、"XC" 这类不能碰的写法，
        // 整串 title case 会把它们改坏。
        guard first.isLowercase else { return trimmed }
        return first.uppercased() + trimmed.dropFirst()
    }

    private func statusColor(for listing: Listing) -> Color {
        ListingStatus.from(listing.status).color
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
