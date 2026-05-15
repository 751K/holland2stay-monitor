import Charts
import SwiftUI

/// 通用图表详情 sheet。
///
/// 给定一个 ``chartKey``（与后端 `/api/v1/stats/public/charts/<key>` 对应），
/// 拉取数据并用 Swift Charts 渲染条形图 + 数值表格。
///
/// 自动判定图表方向
/// ----------------
/// - 时间序列（``daily_new`` / ``daily_changes`` / ``hourly_dist``）→ 横向 BarMark
/// - 分类（``city_dist`` / ``status_dist`` / 其它）→ 横向 BarMark 同样，但
///   labels 按 count 降序显示，更直观
struct ChartDetailView: View {
    let chartKey: String
    let title: String
    let subtitle: String?
    let days: Int

    @State private var chart: ChartData?
    @State private var isLoading = false
    @State private var errorMessage: String?

    init(chartKey: String, title: String, subtitle: String? = nil, days: Int = 30) {
        self.chartKey = chartKey
        self.title = title
        self.subtitle = subtitle
        self.days = days
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let subtitle {
                        Text(subtitle)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal)
                    }

                    if isLoading && chart == nil {
                        ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
                    } else if let err = errorMessage, chart == nil {
                        ContentUnavailableView(
                            "Unable to Load",
                            systemImage: "exclamationmark.triangle",
                            description: Text(err))
                    } else if let chart, chart.data.isEmpty {
                        ContentUnavailableView(
                            "No Data",
                            systemImage: "chart.bar",
                            description: Text("This chart has no entries yet."))
                    } else if let chart {
                        chartView(chart)
                        Divider().padding(.horizontal)
                        breakdownTable(chart)
                    }
                }
                .padding(.vertical)
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .task { await load() }
            .refreshable { await load() }
        }
    }

    // MARK: - Chart

    @ViewBuilder
    private func chartView(_ chart: ChartData) -> some View {
        let isTime = isTimeSeries(chart.key)
        let sorted = isTime ? chart.data : chart.data.sorted { $0.count > $1.count }
        Chart(sorted) { entry in
            BarMark(
                x: .value("Label", entry.label),
                y: .value("Count", entry.count)
            )
            .foregroundStyle(.blue.gradient)
            .annotation(position: .top, alignment: .center) {
                if entry.count > 0 {
                    Text("\(entry.count)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic) { value in
                AxisGridLine()
                AxisTick()
                AxisValueLabel {
                    if let s = value.as(String.self) {
                        Text(prettyLabel(s, isTime: isTime))
                            .font(.caption2)
                    }
                }
            }
        }
        .frame(height: 260)
        .padding(.horizontal)
    }

    // MARK: - Breakdown table

    @ViewBuilder
    private func breakdownTable(_ chart: ChartData) -> some View {
        let isTime = isTimeSeries(chart.key)
        let sorted = isTime ? chart.data.reversed() : Array(chart.data.sorted { $0.count > $1.count })
        let totalAll = chart.data.reduce(0) { $0 + $1.count }

        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Breakdown").font(.headline)
                Spacer()
                Text("\(totalAll) total")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal)
            .padding(.bottom, 8)

            ForEach(Array(sorted.enumerated()), id: \.offset) { idx, entry in
                HStack {
                    Text(prettyLabel(entry.label, isTime: isTime))
                        .font(.subheadline)
                    Spacer()
                    if totalAll > 0 {
                        Text("\(Int(round(Double(entry.count) / Double(totalAll) * 100)))%")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.trailing, 8)
                    }
                    Text("\(entry.count)")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .frame(minWidth: 32, alignment: .trailing)
                }
                .padding(.horizontal)
                .padding(.vertical, 8)
                .background(idx.isMultiple(of: 2) ? Color.clear : Color.gray.opacity(0.08))
            }
        }
    }

    // MARK: - Helpers

    private func isTimeSeries(_ key: String) -> Bool {
        key.hasPrefix("daily_") || key == "hourly_dist"
    }

    /// 时间序列把 "2026-05-13" 缩成 "05-13"；其它原样。
    private func prettyLabel(_ s: String, isTime: Bool) -> String {
        if isTime, s.count >= 10, s.contains("-") {
            return String(s.suffix(5))
        }
        return s
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            chart = try await APIClient.shared.getPublicChart(key: chartKey, days: days)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
