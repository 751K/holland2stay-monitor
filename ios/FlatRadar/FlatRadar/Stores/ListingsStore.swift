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

    /// 每次 fetch / loadMore 自增；返回数据时比对，只接受最新一代的结果。
    /// 防止用户飞速改 filter 时旧请求的响应覆盖新结果。
    private var fetchGeneration: UInt64 = 0

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
        fetchGeneration &+= 1
        let myGen = fetchGeneration
        do {
            let resp = try await client.getListings(
                city: city, status: status, query: query,
                limit: pageSize, offset: 0,
                cities: cities, types: types,
                contract: contract, energy: energy)
            // 期间又被 fetch 一次 → 当前响应已过期，整体丢弃，不写 state。
            guard myGen == fetchGeneration else { return }
            listings = resp.items
            total = resp.total
            isFiltered = resp.filtered ?? false
            lastUpdated = Date()
        } catch {
            guard myGen == fetchGeneration else { return }
            lastError = error as? APIError
            errorMessage = error.localizedDescription
        }
        if myGen == fetchGeneration { isLoading = false }
    }

    func loadMore() async {
        guard hasMore, !isLoadingMore else { return }
        isLoadingMore = true
        let myGen = fetchGeneration   // load-more 不自增 generation；用 fetch 的代号
        do {
            let resp = try await client.getListings(
                city: currentCity, status: currentStatus, query: currentQuery,
                limit: pageSize, offset: listings.count,
                cities: currentCities.isEmpty ? nil : currentCities,
                types: currentTypes.isEmpty ? nil : currentTypes,
                contract: currentContract, energy: currentEnergy)
            // load-more 期间 filter 改了 → 这批分页响应属于旧 filter，丢掉
            guard myGen == fetchGeneration else { return }
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

    /// 登出时清空所有用户相关状态，防止下个登入用户看到上个用户的数据。
    func clear() {
        listings = []
        total = 0
        isLoading = false
        isLoadingMore = false
        errorMessage = nil
        lastError = nil
        isFiltered = false
        lastUpdated = nil
        currentCity = nil
        currentStatus = nil
        currentQuery = nil
        currentCities = []
        currentTypes = []
        currentContract = nil
        currentEnergy = nil
        // 自增 generation —— 任何残留的 in-flight fetch 回来时都会被识别为过期丢弃。
        fetchGeneration &+= 1
    }
}
