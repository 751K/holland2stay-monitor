import Foundation

@MainActor
@Observable
final class DashboardStore {
    var summary: MonitorStatus?
    var meSummary: MeSummary?
    var chartKeys: [String] = []
    var selectedChart: ChartData?
    var isLoading = false
    var isLoadingChart = false
    var errorMessage: String?

    private let client = APIClient.shared

    func fetchSummary() async {
        isLoading = true
        errorMessage = nil
        do {
            summary = try await client.getPublicSummary()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    func fetchMeSummary() async {
        do {
            meSummary = try await client.getMeSummary()
        } catch {
            // me summary is additive; failure keeps public summary visible
        }
    }

    func fetchChartKeys() async {
        do {
            chartKeys = try await client.getPublicCharts()
        } catch {
            // Chart keys are non-critical; don't set errorMessage
        }
    }

    func fetchChart(key: String, days: Int = 30) async {
        isLoadingChart = true
        do {
            selectedChart = try await client.getPublicChart(key: key, days: days)
        } catch {
            // Chart detail failure is non-critical
        }
        isLoadingChart = false
    }
}
