import SwiftUI

struct DashboardView: View {
    @Environment(DashboardStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @Environment(NavigationCoordinator.self) private var coord
    @Environment(\.horizontalSizeClass) private var hSizeClass

    /// 当前要展示的图表详情 sheet；nil = 不显示。
    @State private var activeChart: ChartDetail?
    @State private var showRefreshError = false

    /// 响应式列数：compact=2, regular=3
    private var gridColumns: [GridItem] {
        let count = hSizeClass == .regular ? 3 : 2
        return Array(repeating: GridItem(.flexible(), spacing: 12), count: count)
    }

    /// 描述一次卡片点击应展示的 chart key + 标题 + 范围。
    struct ChartDetail: Identifiable {
        let id: String          // 用 chartKey 当 sheet identity
        let title: String
        let subtitle: String?
        let chartKey: String
        let days: Int

        init(title: String, subtitle: String? = nil, chartKey: String, days: Int = 30) {
            self.id = chartKey + "-\(days)"
            self.title = title
            self.subtitle = subtitle
            self.chartKey = chartKey
            self.days = days
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                Group {
                    if store.isLoading {
                        ProgressView().padding(.top, 60)
                    } else if let s = store.summary {
                        summaryContent(s)
                    } else if let err = store.errorMessage {
                        errorView(err)
                    } else {
                        ContentUnavailableView(
                            "No Data",
                            systemImage: "chart.bar",
                            description: Text("Pull to refresh"))
                    }
                }
            }
            .refreshable { await refresh() }
            .navigationTitle("Dashboard")
            .toolbar { roleBadge }
            .task { await refresh() }
            .onChange(of: store.errorMessage) { _, new in
                showRefreshError = new != nil && store.summary != nil
            }
            .alert(
                store.lastError?.errorDescription ?? "Refresh Failed",
                isPresented: $showRefreshError
            ) {
                Button("OK") {}
            } message: {
                Text(store.errorMessage ?? "")
            }
            .sheet(item: $activeChart) { detail in
                ChartDetailView(
                    chartKey: detail.chartKey,
                    title: detail.title,
                    subtitle: detail.subtitle,
                    days: detail.days)
                .presentationDetents([.fraction(0.65), .large])
                .presentationDragIndicator(.visible)
            }
        }
    }

    private func refresh() async {
        await store.fetchSummary()
        if auth.isUser { await store.fetchMeSummary() }
    }

    // MARK: - Content views

    @ViewBuilder
    private func summaryContent(_ s: MonitorStatus) -> some View {
        LazyVGrid(columns: gridColumns, spacing: 12) {
            StatCard(
                title: "Total Listings", value: s.total.formatted(),
                systemImage: "house.fill", color: .blue,
                action: {
                    activeChart = ChartDetail(
                        title: "Total by City",
                        subtitle: "All \(s.total) listings, grouped",
                        chartKey: "city_dist")
                })
            StatCard(
                title: "New (24h)", value: s.new24h.formatted(),
                systemImage: "sparkles", color: .green,
                action: {
                    activeChart = ChartDetail(
                        title: "New Listings, Last 7 Days",
                        subtitle: "\(s.new24h) added in past 24h",
                        chartKey: "daily_new", days: 7)
                })
            StatCard(
                title: "New (7d)", value: s.new7d.formatted(),
                systemImage: "calendar", color: .orange,
                action: {
                    activeChart = ChartDetail(
                        title: "New Listings, Last 30 Days",
                        subtitle: "\(s.new7d) added in past 7 days",
                        chartKey: "daily_new", days: 30)
                })
            StatCard(
                title: "Changes (24h)", value: s.changes24h.formatted(),
                systemImage: "arrow.triangle.swap", color: .purple,
                action: {
                    activeChart = ChartDetail(
                        title: "Status Changes, Last 7 Days",
                        subtitle: "\(s.changes24h) in past 24h",
                        chartKey: "daily_changes", days: 7)
                })
        }
        .padding(.horizontal)

        if let me = store.meSummary, auth.isUser {
            Divider().padding(.horizontal)
            HStack(spacing: 4) {
                Image(systemName: "person.fill").font(.caption)
                Text("Your Matches")
                    .font(.subheadline).fontWeight(.semibold)
                if me.filterActive {
                    Text("(filtered)").font(.caption).foregroundStyle(.blue)
                }
                Spacer()
            }
            .padding(.horizontal).padding(.top, 4)

            LazyVGrid(columns: gridColumns, spacing: 12) {
                StatCard(
                    title: "Matched", value: me.matchedTotal.formatted(),
                    systemImage: "checkmark.circle", color: .blue,
                    action: { coord.selectedTab = .browse; coord.selectedBrowseMode = .list })
                StatCard(
                    title: "Available",
                    value: me.matchedAvailable?.formatted() ?? "--",
                    systemImage: "house.circle", color: .green,
                    action: { coord.selectedTab = .browse; coord.selectedBrowseMode = .list })
            }
            .padding(.horizontal)
        }

        Divider().padding(.horizontal)
        HStack(spacing: 4) {
            Image(systemName: "chart.pie.fill").font(.caption)
            Text("Explore").font(.subheadline).fontWeight(.semibold)
            Spacer()
        }
        .padding(.horizontal).padding(.top, 4)

        LazyVGrid(columns: gridColumns, spacing: 12) {
            StatCard(title: "By Status", value: "—", systemImage: "chart.bar.fill", color: .indigo,
                     action: { activeChart = ChartDetail(title: "Status Distribution", chartKey: "status_dist") })
            StatCard(title: "By Price", value: "—", systemImage: "eurosign.circle", color: .teal,
                     action: { activeChart = ChartDetail(title: "Price Distribution", chartKey: "price_dist") })
            StatCard(title: "By Type", value: "—", systemImage: "house.lodge", color: .pink,
                     action: { activeChart = ChartDetail(title: "Type Distribution", chartKey: "type_dist") })
            StatCard(title: "By Energy", value: "—", systemImage: "bolt.fill", color: .yellow,
                     action: { activeChart = ChartDetail(title: "Energy Label Distribution", chartKey: "energy_dist") })
        }
        .padding(.horizontal)

        if !s.lastScrape.isEmpty, s.lastScrape != "--" {
            HStack {
                Image(systemName: "clock").foregroundStyle(.secondary)
                Text("Last scrape: \(s.lastScrape)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(.top, 8)
        }
    }

    @ViewBuilder
    private func errorView(_ err: String) -> some View {
        let apiErr = store.lastError
        ContentUnavailableView {
            Label(
                apiErr?.errorDescription ?? "Unable to Load",
                systemImage: apiErr?.systemImage ?? "wifi.slash")
        } description: {
            Text(err)
        } actions: {
            Button("Try Again") { Task { await refresh() } }
        }
    }

    @ToolbarContentBuilder
    private var roleBadge: some ToolbarContent {
        ToolbarItem(placement: .automatic) {
            HStack(spacing: 4) {
                Circle()
                    .fill(auth.isGuest ? Color.gray : auth.isAdmin ? Color.red : Color.blue)
                    .frame(width: 8, height: 8)
                Text(auth.role.rawValue.capitalized)
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
