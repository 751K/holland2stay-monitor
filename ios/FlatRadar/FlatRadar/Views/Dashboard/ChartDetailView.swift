import Charts
import SwiftUI

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
                VStack(alignment: .leading, spacing: 20) {
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
                        // 给 type_dist / energy_dist 等做语义合并（"1"/"2"/"3"→Apt,
                        // A+/A++→A），保证 mini card 和 detail sheet 看到同一套标签。
                        let displayed = ChartData(
                            key: chart.key,
                            days: chart.days,
                            data: chart.data.bucketed(forKey: chart.key))

                        if isTimeSeries(chart.key) {
                            // 时序图：折线/柱图 + 下方表格（互补，不冗余）
                            timeSeriesChart(displayed)
                            breakdownTable(displayed)
                        } else {
                            // 分布图：原本同时画 "Top Categories" 排行 + "Breakdown"
                            // 表格，两者展示同一份数据。删掉 Top Categories，只留
                            // 信息更全的 Breakdown（带百分比、总计、完整列表）。
                            breakdownTable(displayed)
                        }
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

    // MARK: - Time series

    private func timeSeriesChart(_ chart: ChartData) -> some View {
        VStack(spacing: 0) {
            TimeSeriesChartContent(data: chart.data, formatLabel: { prettyLabel($0, isTime: true) })
                .frame(height: 240)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal)
    }

    // MARK: - Breakdown table

    private func breakdownTable(_ chart: ChartData) -> some View {
        let isTime = isTimeSeries(chart.key)
        let sorted = isTime ? chart.data.reversed() : Array(chart.data.sorted { $0.count > $1.count })
        let totalAll = chart.data.reduce(0) { $0 + $1.count }
        let maxCount = sorted.map(\.count).max() ?? 1

        return BreakdownContent(
            entries: sorted,
            total: totalAll,
            maxCount: maxCount,
            formatLabel: { prettyLabel($0, isTime: isTime) }
        )
    }

    // MARK: - Helpers

    private func isTimeSeries(_ key: String) -> Bool {
        key.hasPrefix("daily_") || key == "hourly_dist"
    }

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

// MARK: - Breakdown row

private struct BreakdownRow: View {
    let label: String
    let count: Int
    let total: Int
    let maxCount: Int
    let isEven: Bool

    private var pct: String {
        guard total > 0 else { return "—" }
        return "\(Int(round(Double(count) / Double(total) * 100)))%"
    }

    private var ratio: CGFloat {
        guard maxCount > 0 else { return 0 }
        return max(0.02, CGFloat(count) / CGFloat(maxCount))
    }

    var body: some View {
        HStack(spacing: 12) {
            Text(label)
                .font(.subheadline)
                .frame(width: 100, alignment: .leading)
                .lineLimit(1)

            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(.clear)
                    RoundedRectangle(cornerRadius: 3)
                        .fill(.blue.opacity(isEven ? 0.45 : 0.35))
                        .frame(width: proxy.size.width * ratio)
                }
            }
            .frame(height: 6)

            Text(pct)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .frame(width: 36, alignment: .trailing)

            Text("\(count)")
                .font(.subheadline.weight(.medium))
                .monospacedDigit()
                .frame(width: 40, alignment: .trailing)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(isEven ? Color.clear : Color.primary.opacity(0.03))
    }
}

// MARK: - Extracted content structs (help the Swift type-checker)

private struct TimeSeriesChartContent: View {
    let data: [ChartEntry]
    let formatLabel: (String) -> String

    var body: some View {
        Chart(data) { entry in
            BarMark(
                x: .value("Label", entry.label),
                y: .value("Count", entry.count)
            )
            .foregroundStyle(.blue.gradient)
            .cornerRadius(4, style: .continuous)
            .annotation(position: .top, alignment: .center) {
                if entry.count > 0 {
                    Text("\(entry.count)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 6)) { value in
                AxisGridLine().foregroundStyle(.secondary.opacity(0.3))
                AxisTick().foregroundStyle(.secondary.opacity(0.4))
                AxisValueLabel {
                    if let s = value.as(String.self) {
                        Text(formatLabel(s))
                            .font(.caption2)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks {
                AxisGridLine().foregroundStyle(.secondary.opacity(0.3))
                AxisTick().foregroundStyle(.secondary.opacity(0.4))
                AxisValueLabel().font(.caption2)
            }
        }
    }
}

private struct BreakdownContent: View {
    let entries: [ChartEntry]
    let total: Int
    let maxCount: Int
    let formatLabel: (String) -> String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Label("Breakdown", systemImage: "list.bullet.rectangle")
                    .font(.headline)
                Spacer()
                Text("\(total) total")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal)
            .padding(.bottom, 12)

            ForEach(Array(entries.enumerated()), id: \.offset) { idx, entry in
                BreakdownRow(
                    label: formatLabel(entry.label),
                    count: entry.count,
                    total: total,
                    maxCount: maxCount,
                    isEven: idx.isMultiple(of: 2))
            }
        }
        .padding(16)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal)
    }
}
