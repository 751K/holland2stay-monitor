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

    private let client = APIClient.shared
    private let pageSize = 50

    // Current filter state
    private var currentCity: String?
    private var currentStatus: String?
    private var currentQuery: String?

    var hasMore: Bool { listings.count < total }

    func fetch(city: String? = nil, status: String? = nil, query: String? = nil) async {
        currentCity = city
        currentStatus = status
        currentQuery = query
        isLoading = true
        errorMessage = nil
        do {
            let resp = try await client.getListings(
                city: city, status: status, query: query,
                limit: pageSize, offset: 0)
            listings = resp.items
            total = resp.total
            isFiltered = resp.filtered ?? false
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
                limit: pageSize, offset: listings.count)
            listings.append(contentsOf: resp.items)
            total = resp.total
        } catch {
            // Silently fail on pagination; user can pull-to-refresh
        }
        isLoadingMore = false
    }

    func refresh() async {
        await fetch(city: currentCity, status: currentStatus, query: currentQuery)
    }
}
