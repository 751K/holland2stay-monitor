import SwiftUI

struct DashboardView: View {
    @Environment(DashboardStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @Environment(NavigationCoordinator.self) private var coord
    @Environment(\.colorScheme) private var colorScheme

    /// Cached chart data for inline mini visualizations.
    @State private var chartDailyNew: ChartData?
    @State private var chartStatus: ChartData?
    @State private var chartPrice: ChartData?
    @State private var chartType: ChartData?
    @State private var chartEnergy: ChartData?
    @State private var matchedPreviews: [Listing] = []
    @State private var activeChart: ChartDetail?
    /// "New · 24h" / "New · 7d" / "Changes" 三个 mini stat 点开后的 detail sheet
    @State private var activeRecentMode: RecentActivityMode?

    struct ChartDetail: Identifiable {
        let id = UUID()
        let key: String
        let title: String
        let subtitle: String?
        let days: Int
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if store.isLoading && store.summary == nil {
                        ProgressView().padding(.top, 80).frame(maxWidth: .infinity)
                    } else if let err = store.errorMessage, store.summary == nil {
                        errorView(err)
                    } else {
                        headerRow
                        liveBadge
                        statsCard
                        if auth.isUser, let me = store.meSummary {
                            matchesSection(me)
                        }
                        exploreSection
                    }
                }
                .padding(.bottom, 24)
            }
            .refreshable { await refresh() }
            .background(Color(.systemGroupedBackground))
            .navigationBarTitleDisplayMode(.inline)
            .task { await refresh() }
            .sheet(item: $activeChart) { detail in
                ChartDetailView(chartKey: detail.key,
                                title: detail.title,
                                subtitle: detail.subtitle,
                                days: detail.days)
                    .presentationDetents([.fraction(0.65), .large])
                    .presentationDragIndicator(.visible)
            }
            .sheet(item: $activeRecentMode) { mode in
                RecentActivitySheet(mode: mode)
                    .presentationDetents([.fraction(0.75), .large])
                    .presentationDragIndicator(.visible)
            }
        }
    }

    // MARK: - Greeting

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        let name = auth.userInfo?.name ?? ""
        let prefix: String
        switch hour {
        case 5..<12: prefix = String(localized: "Good morning")
        case 12..<17: prefix = String(localized: "Good afternoon")
        default:      prefix = String(localized: "Good evening")
        }
        return name.isEmpty ? prefix : "\(prefix), \(name)"
    }

    // MARK: - User pill

    @ViewBuilder
    private var userPill: some View {
        let label: String = {
            if auth.isAdmin { return "Admin" }
            if auth.isGuest { return "Guest" }
            return auth.userInfo?.name ?? "User"
        }()
        let initial: String = {
            if auth.isGuest { return "G" }
            if auth.isAdmin { return "A" }
            return String(auth.userInfo?.name.prefix(1) ?? "U").uppercased()
        }()

        if auth.isGuest {
            Menu {
                Button("Sign out", systemImage: "rectangle.portrait.and.arrow.right",
                       role: .destructive) {
                    Task { await auth.logout() }
                }
            } label: {
                HStack(spacing: 8) {
                    ZStack {
                        Circle().fill(Color.secondary.opacity(0.12)).frame(width: 28, height: 28)
                        Text(initial)
                            .font(.system(size: 12, weight: .bold)).foregroundStyle(.secondary)
                    }
                    Text("Guest")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                }
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Color(.systemBackground), in: Capsule())
                .overlay(Capsule().strokeBorder(.secondary.opacity(0.2), lineWidth: 1))
            }
            .menuOrder(.fixed)
        } else {
            Menu {
                Section { Text(label) }
                Button("Log out", systemImage: "rectangle.portrait.and.arrow.right",
                       role: .destructive) {
                    Task { await auth.logout() }
                }
            } label: {
                HStack(spacing: 8) {
                    ZStack {
                        Circle().fill(auth.isAdmin ? Color.red.opacity(0.12) : Color.blue.opacity(0.12))
                            .frame(width: 28, height: 28)
                        Text(initial)
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(auth.isAdmin ? .red : .blue)
                    }
                    Text(label)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                }
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Color(.systemBackground), in: Capsule())
                .overlay(Capsule().strokeBorder(.secondary.opacity(0.2), lineWidth: 1))
            }
            .menuOrder(.fixed)
        }
    }

    // MARK: - Header

    private var headerRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Dashboard")
                    .font(.system(size: 28, weight: .heavy))
                    .tracking(-0.8)
                Spacer()
                if auth.isAuthenticated {
                    userPill
                }
            }
            Text(greeting)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 20)
        .padding(.top, 8).padding(.bottom, 14)
    }

    // MARK: - Live badge

    private var liveBadge: some View {
        let s = store.summary
        // 刷新失败时只把心跳点换橙色 + 文字 "Offline"，不再弹 modal alert
        // 打断用户。store 在 fetch 失败时不会清空 summary，所以页面数据仍可用，
        // 用户感知到的就是"数据稍微过期了"。
        let isStale = store.errorMessage != nil
        let dotColor: Color = isStale ? .orange : .green
        let statusText: String = isStale ? "Offline" : "Live"

        return HStack(spacing: 7) {
            Circle().fill(dotColor)
                .frame(width: 10, height: 10)
                .shadow(color: dotColor.opacity(0.4), radius: 7, x: 0, y: 0)
            Text(statusText)
                .fontWeight(isStale ? .semibold : .regular)
                .foregroundStyle(isStale ? .orange : .primary)
            Text("·")
            Text("updated \(relativeTime(s?.lastScrape ?? ""))")
        }
        .font(.subheadline)
        .padding(.horizontal, 14).padding(.vertical, 9)
        .background(Color(.systemBackground), in: Capsule())
        .overlay(Capsule().strokeBorder(.secondary.opacity(0.15), lineWidth: 1))
        .padding(.horizontal, 20)
        .padding(.bottom, 16)
    }

    // MARK: - Stats card

    private var statsCard: some View {
        let s = store.summary
        let weekGrowth = weekGrowthText
        return VStack(spacing: 0) {
            // Top: big number + sparkline
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("TOTAL LISTINGS")
                        .font(.system(size: 13, weight: .heavy))
                        .foregroundStyle(.secondary)
                        .tracking(1.5)
                    Text("\(s?.total ?? 0)")
                        .font(.system(size: 58, weight: .heavy))
                        .monospacedDigit()
                        .tracking(-2)
                    if let wg = weekGrowth {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.up")
                                .font(.caption2.weight(.bold))
                            Text("+\(wg)")
                            Text("this week")
                                .foregroundStyle(.secondary)
                        }
                        .font(.subheadline)
                        .foregroundStyle(.green)
                    }
                }
                Spacer()
                Sparkline(data: chartDailyNew?.data.map(\.count) ?? [])
                    .stroke(.blue, lineWidth: 2.5)
                    .frame(width: 130, height: 70)
                    .opacity(chartDailyNew == nil ? 0 : 1)
            }
            .padding(20)

            Divider().padding(.horizontal, 20)

            // Bottom: 3 mini stats —— 三个 tap 行为分别匹配：
            //   24h  → 实际房源列表（最 actionable）
            //   7d   → 7 日趋势 chart + 每日 breakdown
            //   Changes → 状态变化趋势 chart（用户可见的 notification 不包含
            //             status_change 事件，无法重建变化的房源列表）
            HStack(spacing: 0) {
                miniStat(num: s?.new24h ?? 0, desc: "New · 24h") {
                    activeRecentMode = .newPast24h
                }
                Rectangle().fill(.secondary.opacity(0.2)).frame(width: 2, height: 36)
                    .padding(.horizontal, 14)
                miniStat(num: s?.new7d ?? 0, desc: "New · 7d") {
                    openMiniChart(key: "daily_new",
                                  title: "New listings",
                                  subtitle: "Last 7 days",
                                  days: 7)
                }
                Rectangle().fill(.secondary.opacity(0.2)).frame(width: 2, height: 36)
                    .padding(.horizontal, 14)
                miniStat(num: s?.changes24h ?? 0, desc: "Changes") {
                    openMiniChart(key: "daily_changes",
                                  title: "Status changes",
                                  subtitle: "Last 7 days",
                                  days: 7)
                }
            }
            .padding(.vertical, 14)
            .padding(.horizontal, 20)
        }
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 22))
        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(.secondary.opacity(0.12), lineWidth: 1))
        .shadow(color: .black.opacity(0.04), radius: 10, y: 4)
        .padding(.horizontal, 20)
        .padding(.bottom, 24)
    }

    private func miniStat(num: Int, desc: String, onTap: @escaping () -> Void) -> some View {
        Button(action: onTap) {
            VStack(alignment: .leading, spacing: 4) {
                Text("\(num)")
                    .font(.system(size: 26, weight: .heavy))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Text(desc)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var weekGrowthText: String? {
        guard let daily = chartDailyNew else {
            // Without chart data, fallback to new7d
            if let s = store.summary { return "\(s.new7d)" }
            return nil
        }
        let last7 = daily.data.suffix(7).reduce(0) { $0 + $1.count }
        guard last7 > 0 else { return nil }
        return "\(last7)"
    }

    // MARK: - Matches section (user only)

    private func matchesSection(_ me: MeSummary) -> some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 4) {
                    Text("Your matches")
                        .font(.system(size: 22, weight: .heavy))
                        .tracking(-0.5)
                    if me.filterActive {
                        Text("(filtered)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("See all \(me.matchedTotal)") {
                    coord.selectedTab = .browse; coord.selectedBrowseMode = .list
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.blue)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 12)

            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(me.matchedTotal)")
                        .font(.system(size: 38, weight: .heavy))
                        .monospacedDigit()
                        .tracking(-1)
                    Text("matched · all available")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(minWidth: 80)

                if matchedPreviews.isEmpty {
                    ForEach(0..<3, id: \.self) { _ in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("—").font(.system(size: 13, weight: .bold))
                            ForEach(0..<5, id: \.self) { _ in
                                Text("⋯⋯").font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 128, alignment: .topLeading)
                        .padding(.vertical, 12).padding(.horizontal, 10)
                        .background(Color(.systemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
                    }
                } else {
                    ForEach(matchedPreviews.prefix(3)) { listing in
                        Button {
                            coord.openListing(id: listing.id)
                        } label: {
                            matchPreviewCard(listing)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(15)
            .background(Color(.systemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(.secondary.opacity(0.12), lineWidth: 1))
            .padding(.horizontal, 20)
            .padding(.bottom, 26)
        }
    }

    // MARK: - Match preview card

    /// 一张 ~80pt 高的迷你卡，挂在 Your matches 区下方 3 列。
    /// 露价格 / 状态点 / 城市+面积，比之前两行（仅 price + city）多一档信息。
    private func matchPreviewCard(_ listing: Listing) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(listing.priceRaw ?? "—")
                .font(.system(size: 13, weight: .bold))
                .lineLimit(1)
                .minimumScaleFactor(0.85)

            HStack(spacing: 4) {
                Circle()
                    .fill(matchStatusColor(listing))
                    .frame(width: 5, height: 5)
                Text(matchStatusLabel(listing))
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(matchStatusColor(listing))
                    .lineLimit(1)
            }

            // 副信息层：面积 → 楼栋 → 城市 → 起租日，每行独占一条，10pt 副字号
            if let area = matchAreaText(listing) {
                Text(area)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if let building = listing.buildingText {
                Text(building)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            if !listing.city.isEmpty {
                Text(listing.city)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            if let from = listing.availableShortText {
                // 不加 "from" 前缀 —— 用户希望窄卡里直接显示"Jun 22"，少噪声
                Text(from)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 128, alignment: .topLeading)
        .padding(.vertical, 12).padding(.horizontal, 10)
        .background(Color(.systemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
    }

    private func matchStatusLabel(_ listing: Listing) -> String {
        switch listing.statusKind {
        case .book: return "Book"
        case .lottery: return "Lottery"
        case .reserved: return "Reserved"
        case .other: return listing.status
        }
    }

    private func matchStatusColor(_ listing: Listing) -> Color {
        switch listing.statusKind {
        case .book: return .green
        case .lottery: return .orange
        case .reserved, .other: return Color(.systemGray)
        }
    }

    private func matchAreaText(_ listing: Listing) -> String? {
        guard let area = listing.areaText else { return nil }
        let trimmed = area.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.lowercased().contains("m") ? trimmed : "\(trimmed)m²"
    }

    // MARK: - Explore section

    private var exploreSection: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("Explore")
                    .font(.system(size: 22, weight: .heavy))
                    .tracking(-0.5)
                Spacer()
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 12)

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                statusMiniCard
                priceMiniCard
                typeMiniCard
                energyMiniCard
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 18)

            Button {
                coord.selectedTab = .browse; coord.selectedBrowseMode = .list
            } label: {
                Text("More breakdowns ›")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.blue)
            }
            .frame(maxWidth: .infinity)
        }
    }

    // MARK: - Mini cards
    //
    // 共用骨架（exploreCard）保证 4 张卡：
    //   - 标题永远在最上 14pt padding 处对齐，chevron 用小一号 11pt semibold
    //     替代之前 18pt light，少抢戏；
    //   - header 和 content 之间 Spacer(minLength:) 强行拉开，所有 chart 视觉
    //     落在卡下半部同一条带；
    //   - 卡高 88 → 116，给 byStatus 的 3 个 mono 统计列、byType 的 3 行
    //     水平条留呼吸空间，不再"太满"。

    @ViewBuilder
    private func exploreCard<Content: View>(
        title: String,
        tapKey: String,
        tapTitle: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center) {
                Text(title)
                    .font(.system(size: 13, weight: .heavy))
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 10)
            content()
        }
        .padding(14)
        .frame(height: 116)
        .background(Color(.systemBackground), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.secondary.opacity(0.1), lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { openMiniChart(key: tapKey, title: tapTitle) }
    }

    private var statusMiniCard: some View {
        exploreCard(title: "By status", tapKey: "status_dist", tapTitle: "By Status") {
            if let chart = chartStatus, !chart.data.isEmpty {
                let total = chart.data.reduce(0) { $0 + $1.count }
                let available = chart.data.first(where: { $0.label.lowercased().contains("available") })?.count ?? 0
                let lottery = chart.data.first(where: { $0.label.lowercased().contains("lottery") })?.count ?? 0
                let unavailable = total - available - lottery
                let sum = max(available + lottery + unavailable, 1)

                VStack(alignment: .leading, spacing: 8) {
                    GeometryReader { proxy in
                        let w = proxy.size.width
                        HStack(spacing: 0) {
                            if available > 0 {
                                RoundedRectangle(cornerRadius: 2).fill(.green)
                                    .frame(width: max(4, w * CGFloat(available) / CGFloat(sum)))
                            }
                            if lottery > 0 {
                                RoundedRectangle(cornerRadius: 2).fill(.orange)
                                    .frame(width: max(4, w * CGFloat(lottery) / CGFloat(sum)))
                            }
                            if unavailable > 0 {
                                RoundedRectangle(cornerRadius: 2).fill(.gray.opacity(0.4))
                                    .frame(width: max(4, w * CGFloat(unavailable) / CGFloat(sum)))
                            }
                        }
                    }
                    .frame(height: 6).clipShape(Capsule())

                    HStack(alignment: .firstTextBaseline) {
                        VStack(spacing: 2) {
                            Text("\(available)").fontWeight(.bold).foregroundStyle(.green)
                            Text("book").foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(spacing: 2) {
                            Text("\(lottery)").fontWeight(.bold).foregroundStyle(.orange)
                            Text("lottery").foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(spacing: 2) {
                            Text("\(unavailable)").fontWeight(.bold)
                            Text("other").foregroundStyle(.secondary)
                        }
                    }
                    .font(.system(size: 11, design: .monospaced))
                }
            }
        }
    }

    private var priceMiniCard: some View {
        exploreCard(title: "By price", tapKey: "price_dist", tapTitle: "By Price") {
            if let chart = chartPrice, !chart.data.isEmpty {
                let sorted = chart.data.sorted { priceSortKey($0.label) < priceSortKey($1.label) }
                let maxCount = sorted.map(\.count).max() ?? 1

                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .bottom, spacing: 3) {
                        ForEach(sorted.prefix(9)) { entry in
                            RoundedRectangle(cornerRadius: 2)
                                .fill(.blue.opacity(0.55))
                                .frame(height: max(4, ratio(entry.count, maxCount) * 36))
                        }
                    }
                    HStack {
                        Text(sorted.first?.label ?? "€—").foregroundStyle(.secondary)
                        Spacer()
                        Text(sorted.last?.label ?? "€—").foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
            }
        }
    }

    private var typeMiniCard: some View {
        exploreCard(title: "By type", tapKey: "type_dist", tapTitle: "By Type") {
            if let chart = chartType, !chart.data.isEmpty {
                // 后端发 "1"/"2"/"3"/"4" 表示 N-room，bucketed 合并成 "Apt"；
                // "Studio" / "Loft" 关键字保留。
                let merged = chart.data.bucketed(forKey: "type_dist")
                    .sorted { $0.count > $1.count }
                    .prefix(3)
                let maxCount = merged.map(\.count).max() ?? 1

                VStack(spacing: 5) {
                    ForEach(Array(merged)) { entry in
                        HStack(spacing: 6) {
                            Text(entry.label)
                                .font(.system(size: 11))
                                .lineLimit(1)
                                .frame(width: 44, alignment: .leading)
                            GeometryReader { proxy in
                                RoundedRectangle(cornerRadius: 2)
                                    .fill(.blue.opacity(0.6))
                                    .frame(width: proxy.size.width * ratio(entry.count, maxCount))
                            }
                            .frame(height: 5)
                            Text("\(entry.count)")
                                .font(.system(size: 11, weight: .bold))
                                .frame(width: 26, alignment: .trailing)
                        }
                    }
                }
            }
        }
    }

    private var energyMiniCard: some View {
        exploreCard(title: "By energy", tapKey: "energy_dist", tapTitle: "By Energy") {
            if let chart = chartEnergy, !chart.data.isEmpty {
                // A+/A++/A+++ 归为 "A"；bucketed 已按 A→G 顺序输出。
                let merged = chart.data.bucketed(forKey: "energy_dist")
                let maxCount = merged.map(\.count).max() ?? 1

                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .bottom, spacing: 4) {
                        ForEach(merged) { entry in
                            RoundedRectangle(cornerRadius: 2)
                                .fill(energyBarColor(entry.label))
                                .frame(maxWidth: .infinity)
                                .frame(height: max(4, ratio(entry.count, maxCount) * 32))
                        }
                    }
                    HStack(spacing: 4) {
                        ForEach(merged.prefix(5)) { entry in
                            Text(entry.label)
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity)
                        }
                    }
                }
            }
        }
    }


    // MARK: - Error

    @ViewBuilder
    private func errorView(_ err: String) -> some View {
        let apiErr = store.lastError
        ContentUnavailableView {
            Label(apiErr?.errorDescription ?? "Unable to Load",
                  systemImage: apiErr?.systemImage ?? "wifi.slash")
        } description: { Text(err) } actions: {
            Button("Try Again") { Task { await refresh() } }
        }
    }

    // MARK: - Helpers

    private func ratio(_ value: Int, _ maxVal: Int) -> CGFloat {
        guard maxVal > 0 else { return 0 }
        let r = CGFloat(value) / CGFloat(maxVal)
        return r < 0.04 ? 0.04 : r
    }

    private func energyRank(_ label: String) -> Int {
        let labels = ["A+++", "A++", "A+", "A", "B", "C", "D", "E", "F"]
        return labels.firstIndex(of: label.uppercased().trimmingCharacters(in: .whitespaces)) ?? 99
    }

    private func priceSortKey(_ label: String) -> Double {
        // Parse numeric lower bound from strings like "€0-500", "500-1000", "€1,200+"
        let cleaned = label.replacingOccurrences(of: "€", with: "")
            .replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespaces)
        if let dashIdx = cleaned.firstIndex(of: "-") {
            return Double(cleaned[..<dashIdx].trimmingCharacters(in: .whitespaces)) ?? 0
        }
        return Double(cleaned.replacingOccurrences(of: "+", with: "")) ?? 0
    }

    private func openMiniChart(key: String, title: String,
                               subtitle: String? = nil, days: Int = 30) {
        activeChart = ChartDetail(key: key, title: title, subtitle: subtitle, days: days)
    }

    private func energyBarColor(_ label: String) -> Color {
        // 桶后标签是 "A+" / "A" / "B" / "C" / "D" / ... — rank 表里:
        //   A+++=0, A++=1, A+=2, A=3, B=4, C=5, D=6, E=7, F=8
        // 让 A+ 单独绿色，A 用 mint 跟它视觉拉开档次。
        switch energyRank(label) {
        case 0...1: return Color(red: 20/255, green: 140/255, blue: 70/255)   // A+++ / A++
        case 2:     return Color(red: 52/255, green: 199/255, blue: 89/255)    // A+
        case 3:     return Color(red: 140/255, green: 200/255, blue: 80/255)   // A
        case 4:     return .yellow                                              // B
        case 5:     return .orange                                              // C
        default:    return .red                                                 // D 及以下
        }
    }

    private func refresh() async {
        // 防御：SwiftUI 的 .refreshable / .task 在 @Observable 状态变化时可能把
        // 当前任务 cancel 掉（之前的 bug：SSE 推一批通知时整个 dashboard 的 refresh
        // 任务被 cancel，所有 URLSession.data 同步抛 NSURLErrorCancelled -999）。
        //
        // Task { ... } 是非结构化任务，**不继承父任务的 cancellation**。即便父任务
        // (.refreshable) 被 cancel，里面的 URLSession 请求也会正常跑完、状态正常更新。
        let work = Task { @MainActor in
            await store.fetchSummary()
            if auth.isUser {
                await store.fetchMeSummary()
                await fetchMatchedPreviews()
            }
            await fetchMiniCharts()
        }
        // 等结果完成；work 不继承父任务 cancellation，因此即使 .refreshable
        // 被取消，里面的数据请求仍会继续跑完。
        await work.value
    }

    private func fetchMatchedPreviews() async {
        do {
            let resp = try await APIClient.shared.getListings(limit: 3, offset: 0)
            matchedPreviews = resp.items
        } catch {
            matchedPreviews = []
        }
    }

    private func fetchMiniCharts() async {
        async let dn = try? APIClient.shared.getPublicChart(key: "daily_new", days: 30)
        async let st = try? APIClient.shared.getPublicChart(key: "status_dist", days: 30)
        async let pr = try? APIClient.shared.getPublicChart(key: "price_dist", days: 30)
        async let tp = try? APIClient.shared.getPublicChart(key: "type_dist", days: 30)
        async let en = try? APIClient.shared.getPublicChart(key: "energy_dist", days: 30)
        let (dnR, stR, prR, tpR, enR) = await (dn, st, pr, tp, en)
        chartDailyNew = dnR
        chartStatus = stR
        chartPrice = prR
        chartType = tpR
        chartEnergy = enR
    }

    private func relativeTime(_ iso: String) -> String {
        ServerTime.relativeTime(iso)
    }
}

// MARK: - Recent activity sheet

/// 暂时只剩 "新房 24h" 一种 sheet ——7d 和 Changes 改成走 ChartDetailView 趋势图。
enum RecentActivityMode: String, Identifiable {
    case newPast24h
    var id: String { rawValue }

    var title: String {
        switch self {
        case .newPast24h: return "New · last 24h"
        }
    }

    var maxAge: TimeInterval {
        switch self {
        case .newPast24h: return 24 * 3600
        }
    }
}

/// 拉一页 listings（后端会按用户角色自动套用 user filter），客户端按
/// `firstSeen` 时间窗筛掉旧的，剩下的就是"24h 内新增 + 符合用户过滤条件的房源"。
/// 用 ListingRow 渲染，跟 Browse 页样式一致。
struct RecentActivitySheet: View {
    let mode: RecentActivityMode
    @Environment(NavigationCoordinator.self) private var coord
    @Environment(\.dismiss) private var dismiss

    @State private var listings: [Listing] = []
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Group {
                if isLoading && listings.isEmpty {
                    ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
                } else if let err = errorMessage, listings.isEmpty {
                    ContentUnavailableView("Unable to Load",
                                           systemImage: "wifi.slash",
                                           description: Text(err))
                } else if filtered.isEmpty {
                    ContentUnavailableView(
                        "No new listings",
                        systemImage: "house",
                        description: Text("No listings matched your filter in the last 24 hours."))
                } else {
                    List {
                        Section {
                            ForEach(filtered) { listing in
                                Button {
                                    let lid = listing.id
                                    dismiss()
                                    coord.openListing(id: lid)
                                } label: {
                                    ListingRow(listing: listing)
                                }
                                .buttonStyle(.plain)
                            }
                        } header: {
                            Text("\(filtered.count) \(filtered.count == 1 ? "listing" : "listings") · matches your filter")
                                .font(.system(size: 11, weight: .bold, design: .monospaced))
                                .tracking(0.7)
                                .foregroundStyle(.secondary)
                                .textCase(nil)
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle(mode.title)
            .navigationBarTitleDisplayMode(.inline)
            .task { await fetch() }
            .refreshable { await fetch() }
        }
    }

    /// 24h 内 first_seen 的房源——后端默认 sort by first_seen desc，所以一页 100
    /// 基本能覆盖任何 24h 增量（即便系统正常一天也就 5-50 条新增）。
    private var filtered: [Listing] {
        let cutoff = Date().addingTimeInterval(-mode.maxAge)
        return listings
            .filter { ($0.firstSeenDate ?? .distantPast) >= cutoff }
            .sorted { ($0.firstSeenDate ?? .distantPast) > ($1.firstSeenDate ?? .distantPast) }
    }

    @MainActor
    private func fetch() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let resp = try await APIClient.shared.getListings(limit: 100, offset: 0)
            listings = resp.items
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Sparkline

private struct Sparkline: Shape {
    let data: [Int]
    func path(in rect: CGRect) -> Path {
        Path { p in
            guard data.count > 1, let maxVal = data.max(), maxVal > 0 else { return }
            let stepX = rect.width / CGFloat(data.count - 1)
            let scaleY = rect.height / CGFloat(maxVal)
            p.move(to: CGPoint(x: 0, y: rect.height - CGFloat(data[0]) * scaleY))
            for i in data.indices.dropFirst() {
                p.addLine(to: CGPoint(x: CGFloat(i) * stepX,
                                      y: rect.height - CGFloat(data[i]) * scaleY))
            }
        }
    }
}
