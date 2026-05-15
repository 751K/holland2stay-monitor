import SwiftUI

struct DashboardView: View {
    @Environment(DashboardStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @Environment(NavigationCoordinator.self) private var coord

    /// 当前要展示的图表详情 sheet；nil = 不显示。
    @State private var activeChart: ChartDetail?

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
                if store.isLoading {
                    ProgressView().padding(.top, 60)
                } else if let s = store.summary {
                    LazyVGrid(columns: [
                        GridItem(.flexible()), GridItem(.flexible()),
                    ], spacing: 12) {
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

                    // Personalized stats for logged-in users
                    if let me = store.meSummary, auth.isUser {
                        Divider().padding(.horizontal)
                        HStack(spacing: 4) {
                            Image(systemName: "person.fill")
                                .font(.caption)
                            Text("Your Matches")
                                .font(.subheadline)
                                .fontWeight(.semibold)
                            if me.filterActive {
                                Text("(filtered)")
                                    .font(.caption)
                                    .foregroundStyle(.blue)
                            }
                            Spacer()
                        }
                        .padding(.horizontal)
                        .padding(.top, 4)

                        LazyVGrid(columns: [
                            GridItem(.flexible()), GridItem(.flexible()),
                        ], spacing: 12) {
                            StatCard(
                                title: "Matched", value: me.matchedTotal.formatted(),
                                systemImage: "checkmark.circle", color: .blue,
                                action: { coord.selectedTab = .listings })
                            StatCard(
                                title: "Available",
                                value: me.matchedAvailable?.formatted() ?? "--",
                                systemImage: "house.circle", color: .green,
                                action: { coord.selectedTab = .listings })
                        }
                        .padding(.horizontal)
                    }

                    // 额外的全库分布——也很常被想看
                    Divider().padding(.horizontal)
                    HStack(spacing: 4) {
                        Image(systemName: "chart.pie.fill")
                            .font(.caption)
                        Text("Explore")
                            .font(.subheadline)
                            .fontWeight(.semibold)
                        Spacer()
                    }
                    .padding(.horizontal)
                    .padding(.top, 4)

                    LazyVGrid(columns: [
                        GridItem(.flexible()), GridItem(.flexible()),
                    ], spacing: 12) {
                        StatCard(
                            title: "By Status", value: "—",
                            systemImage: "chart.bar.fill", color: .indigo,
                            action: { activeChart = ChartDetail(
                                title: "Status Distribution",
                                chartKey: "status_dist") })
                        StatCard(
                            title: "By Price", value: "—",
                            systemImage: "eurosign.circle", color: .teal,
                            action: { activeChart = ChartDetail(
                                title: "Price Distribution",
                                chartKey: "price_dist") })
                        StatCard(
                            title: "By Type", value: "—",
                            systemImage: "house.lodge", color: .pink,
                            action: { activeChart = ChartDetail(
                                title: "Type Distribution",
                                chartKey: "type_dist") })
                        StatCard(
                            title: "By Energy", value: "—",
                            systemImage: "bolt.fill", color: .yellow,
                            action: { activeChart = ChartDetail(
                                title: "Energy Label Distribution",
                                chartKey: "energy_dist") })
                    }
                    .padding(.horizontal)

                    if !s.lastScrape.isEmpty, s.lastScrape != "--" {
                        HStack {
                            Image(systemName: "clock")
                                .foregroundStyle(.secondary)
                            Text("Last scrape: \(s.lastScrape)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 8)
                    }
                } else if let err = store.errorMessage {
                    ContentUnavailableView(
                        "Unable to Load",
                        systemImage: "wifi.slash",
                        description: Text(err))
                } else {
                    ContentUnavailableView(
                        "No Data",
                        systemImage: "chart.bar",
                        description: Text("Pull to refresh"))
                }
            }
            .refreshable {
                await store.fetchSummary()
                if auth.isUser { await store.fetchMeSummary() }
            }
            .navigationTitle("Dashboard")
            .toolbar {
                // 仅保留右上角的角色指示器；登出统一放 Settings tab
                ToolbarItem(placement: .automatic) {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(auth.isGuest ? Color.gray : auth.isAdmin ? Color.red : Color.blue)
                            .frame(width: 8, height: 8)
                        Text(auth.role.rawValue.capitalized)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .task {
                await store.fetchSummary()
                if auth.isUser { await store.fetchMeSummary() }
            }
            .sheet(item: $activeChart) { detail in
                ChartDetailView(
                    chartKey: detail.chartKey,
                    title: detail.title,
                    subtitle: detail.subtitle,
                    days: detail.days)
                .presentationDetents([.large, .medium])
            }
        }
    }
}
