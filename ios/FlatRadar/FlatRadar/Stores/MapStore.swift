import Foundation

/// 地图视图状态：缓存房源 + 当前选中 + 加载/错误标志。
///
/// 数据加载策略
/// ------------
/// - 进入 Map tab 时 `.task` 调一次 ``fetch()``
/// - 下拉触发 ``refresh()``
/// - 不做无限滚动（地图视图天然适合一次性加载，后端已 LIMIT 2000）
@MainActor
@Observable
final class MapStore {
    var listings: [MapListing] = []
    var uncached: Int = 0
    var isLoading: Bool = false
    var errorMessage: String?

    /// 当前选中的房源——MapView 用 `Map(selection:)` 双向绑定，
    /// 选中时弹底部 sheet 卡片。
    var selectedID: String?

    private let client = APIClient.shared

    var selected: MapListing? {
        guard let id = selectedID else { return nil }
        return listings.first(where: { $0.id == id })
    }

    func fetch() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let resp = try await client.getMap()
            listings = resp.listings
            uncached = resp.uncached
        } catch {
            errorMessage = error.localizedDescription
            print("[MapStore] fetch error: \(error)")
        }
    }

    func refresh() async {
        await fetch()
    }
}
