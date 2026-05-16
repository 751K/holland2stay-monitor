import Foundation

@MainActor
@Observable
final class ListingsStore {
    var listings: [Listing] = []
    var total = 0
    var isLoading = false
    var isLoadingMore = false
    var errorMessage: String?
    var lastError: APIError?
    var isFiltered = false
    /// 最近一次成功 fetch 的本地时间戳 — 用于 ListingsView 顶部 "updated 2m ago" 心跳条。
    var lastUpdated: Date?

    private let client = APIClient.shared
    private let pageSize = 50

    // Current filter state
    private var currentCity: String?
    private var currentStatus: String?
    private var currentQuery: String?
    private var currentCities: [String] = []
    private var currentTypes: [String] = []
    private var currentContract: String?
    private var currentEnergy: String?

    var hasMore: Bool { listings.count < total }

    func fetch(city: String? = nil, status: String? = nil, query: String? = nil,
               cities: [String]? = nil, types: [String]? = nil,
               contract: String? = nil, energy: String? = nil) async {
        currentCity = city
        currentStatus = status
        currentQuery = query
        currentCities = cities ?? []
        currentTypes = types ?? []
        currentContract = contract
        currentEnergy = energy
        isLoading = true
        errorMessage = nil
        do {
            let resp = try await client.getListings(
                city: city, status: status, query: query,
                limit: pageSize, offset: 0,
                cities: cities, types: types,
                contract: contract, energy: energy)
            listings = resp.items
            total = resp.total
            isFiltered = resp.filtered ?? false
            lastUpdated = Date()
        } catch {
            lastError = error as? APIError
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    func loadMore() async {
        guard hasMore, !isLoadingMore else { return }
        isLoadingMore = true
        do {
            let resp = try await client.getListings(
                city: currentCity, status: currentStatus, query: currentQuery,
                limit: pageSize, offset: listings.count,
                cities: currentCities.isEmpty ? nil : currentCities,
                types: currentTypes.isEmpty ? nil : currentTypes,
                contract: currentContract, energy: currentEnergy)
            listings.append(contentsOf: resp.items)
            total = resp.total
        } catch {
            // Silently fail on pagination; user can pull-to-refresh
        }
        isLoadingMore = false
    }

    func refresh() async {
        await fetch(city: currentCity, status: currentStatus, query: currentQuery,
                    cities: currentCities.isEmpty ? nil : currentCities,
                    types: currentTypes.isEmpty ? nil : currentTypes,
                    contract: currentContract, energy: currentEnergy)
    }
}
