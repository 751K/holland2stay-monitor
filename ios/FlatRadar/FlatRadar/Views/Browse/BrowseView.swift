import SwiftUI
import UIKit

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
/// - iPhone 上 segmented picker 放在 nav bar 左侧，避免不同子页 toolbar
///   组合后出现一页靠左、一页居中的跳动
/// - 每个子视图保留自己的 `.toolbar` 项（搜索框、刷新、Today 等），通过修饰
///   器累积合并到本视图的 NavigationStack
struct BrowseView: View {
    @Environment(NavigationCoordinator.self) private var coord

    var body: some View {
        @Bindable var coord = coord

        NavigationStack(path: $coord.listingsPath) {
            content
            // 不显示 nav title：iPad inline picker / iPhone compactModeMenu
            // 都已标明当前模式，nav bar 里再写一遍 "Calendar" 冗余。
            // 但 nav bar 本身要保留布局高度——map 视图 ignoresSafeArea(.top)
            // 把地图穿到顶部，picker 靠 ZStack(.top) 落位；nav bar 高度塌掉
            // 会让 picker 直接顶到 status bar 下方。用 .toolbar(.visible,…)
            // 强制 nav bar 占位但内容空。
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar(.visible, for: .navigationBar)
            // 把 nav bar 背景锁到 systemGroupedBackground，避免 iPad portrait
            // 下系统默认 nav bar 透出白色 systemBackground 与下面灰底 insetGrouped
            // 列表分层。
            // 唯独 map 模式要例外——map 视图主动 ignoresSafeArea(.top) 让地图穿
            // 过顶部一直延伸到 status bar，picker 浮在地图上是设计本意。如果给
            // nav bar 加灰底，会切出一条灰带 + picker 突兀压在绿色地图上。
            .toolbarBackground(Color(.systemGroupedBackground), for: .navigationBar)
            .toolbarBackground(
                coord.selectedBrowseMode == .map ? .hidden : .visible,
                for: .navigationBar
            )
            .toolbar {
                if !usesInlineModePicker {
                    ToolbarItem(placement: .topBarLeading) {
                        compactModeMenu
                    }
                }
            }
            .navigationDestination(for: ListingRoute.self) { route in
                ListingDetailView(route: route)
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if usesInlineModePicker {
            // iPad：MapView 始终留在 ZStack 底层 —— 切到 list/calendar 时只是
            // opacity=0 隐藏，**不销毁实例**。否则每次切回 map 都重新 init
            // MapView / 重拉瓦片 / 重建 cluster，肉眼能看到加载闪烁。
            ZStack(alignment: .top) {
                MapView(overlayTopPadding: 132)
                    .ignoresSafeArea(edges: .top)
                    .opacity(coord.selectedBrowseMode == .map ? 1 : 0)
                    .allowsHitTesting(coord.selectedBrowseMode == .map)

                if coord.selectedBrowseMode == .map {
                    modePicker(maxWidth: 360)
                        .padding(.horizontal, 28)
                        .padding(.top, 8)
                } else {
                    VStack(spacing: 8) {
                        modePicker(maxWidth: 360)
                            .padding(.horizontal, 28)
                            .padding(.top, 8)
                        nonMapContent
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(.systemGroupedBackground))
                }
            }
        } else {
            // iPhone：同样的 MapView 保活策略。switch 创建 MapView 那条路径会
            // 在每次模式切换时 destroy + recreate，闪烁原因。
            // List / Calendar 视图本身有不透明 systemGroupedBackground，盖住下面
            // 的 map，视觉上感受跟之前一致。
            ZStack {
                MapView()
                    .opacity(coord.selectedBrowseMode == .map ? 1 : 0)
                    .allowsHitTesting(coord.selectedBrowseMode == .map)

                if coord.selectedBrowseMode != .map {
                    nonMapContent
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Color(.systemGroupedBackground))
                }
            }
        }
    }

    /// 非 map 模式的内容 —— list 或 calendar。
    /// map 是外层 ZStack 永久保留的，这里不再渲染 MapView，
    /// 避免与外层的"持久 MapView"形成两个实例。
    @ViewBuilder
    private var nonMapContent: some View {
        switch coord.selectedBrowseMode {
        case .list:     ListingsView()
        case .calendar: CalendarView()
        case .map:      EmptyView()   // outer condition guarantees unreachable
        }
    }

    private var usesInlineModePicker: Bool {
        UIDevice.current.userInterfaceIdiom == .pad
    }

    private var compactModeMenu: some View {
        @Bindable var coord = coord
        return Menu {
            Picker("Mode", selection: $coord.selectedBrowseMode) {
                ForEach(BrowseMode.allCases) { mode in
                    Label(mode.label, systemImage: mode.systemImage).tag(mode)
                }
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: coord.selectedBrowseMode.systemImage)
                Text(coord.selectedBrowseMode.label)
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
            }
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.primary)
            .padding(.horizontal, 4)
            .contentShape(Rectangle())
        }
    }

    private func modePicker(maxWidth: CGFloat) -> some View {
        @Bindable var coord = coord
        return Picker("Mode", selection: $coord.selectedBrowseMode) {
            ForEach(BrowseMode.allCases) { mode in
                Label(mode.label, systemImage: mode.systemImage).tag(mode)
            }
        }
        .pickerStyle(.segmented)
        .frame(maxWidth: maxWidth)
    }
}
