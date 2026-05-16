import SwiftUI

/// "Browse" tab — 把 Listings/Map/Calendar 三个"同一份数据的不同视图"合并到
/// 一个 tab，靠顶部 toolbar 的 segmented picker 切换模式。
///
/// 为什么合并
/// ----------
/// iPhone tab bar 最多显示 5 个 tab，多出来的自动塞 "More" 标签下折叠。我们
/// 之前 6 个 tab（含 Dashboard/Listings/Map/Calendar/Notifications/Settings）
/// → Notifications/Settings 中的一个被折叠，体验不好。
///
/// 这三个 view 本质都是浏览房源，合并后语义反而更干净，腾出 tab 给真正不同
/// 职责的 Notifications / Settings。
///
/// 设计
/// ----
/// - 单一 NavigationStack(path: $coord.listingsPath)，所有模式共享同一个导航栈
/// - `navigationDestination(for: ListingRoute.self)` 上提到这里，三个子视图
///   里直接 `NavigationLink(value: ListingRoute.xxx)` 即可 push 详情
/// - segmented picker 用 ``.principal`` 放在 nav bar 正中
/// - 每个子视图保留自己的 `.toolbar` 项（搜索框、刷新、Today 等），通过修饰
///   器累积合并到本视图的 NavigationStack
struct BrowseView: View {
    @Environment(NavigationCoordinator.self) private var coord

    var body: some View {
        @Bindable var coord = coord

        NavigationStack(path: $coord.listingsPath) {
            ZStack {
                ListingsView()
                    .opacity(coord.selectedBrowseMode == .list ? 1 : 0)
                    .allowsHitTesting(coord.selectedBrowseMode == .list)
                MapView()
                    .opacity(coord.selectedBrowseMode == .map ? 1 : 0)
                    .allowsHitTesting(coord.selectedBrowseMode == .map)
                CalendarView()
                    .opacity(coord.selectedBrowseMode == .calendar ? 1 : 0)
                    .allowsHitTesting(coord.selectedBrowseMode == .calendar)
            }
            .navigationTitle(coord.selectedBrowseMode.label)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Picker("Mode", selection: $coord.selectedBrowseMode) {
                        ForEach(BrowseMode.allCases) { mode in
                            Image(systemName: mode.systemImage).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 220)
                }
            }
            .navigationDestination(for: ListingRoute.self) { route in
                ListingDetailView(route: route)
            }
        }
    }
}
