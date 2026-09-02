import CoreLocation
import MapKit
import SwiftUI

/// 房源地图视图。
///
/// MapKit + SwiftUI（iOS 17+ API）
/// -------------------------------
/// - ``Map(position:)`` 维护 camera 位置，初始锚定在 Eindhoven 附近
///   （Holland2Stay 大部分房源所在城市）
/// - ``Annotation`` 自定义 pin，颜色按状态区分（available/lottery/unavailable）
/// - ``Map(selection:)`` 双向绑 ``store.selectedID``，点 pin 选中 → sheet 弹卡
/// - 底图用 ``.mapStyle(.standard(elevation: .flat))``——不要地形高程
///
/// 详情入口
/// --------
/// 弹卡 "View Details" 按钮调 ``coord.openListing(id:)`` ——
/// 复用 deep link 同一路由，切到 Listings tab 推 ``ListingDetailView(.byId)``。
struct MapView: View {
    @Environment(MapStore.self) private var store
    @Environment(NavigationCoordinator.self) private var coord
    @State private var locationProvider = UserLocationProvider()

    let overlayTopPadding: CGFloat

    init(overlayTopPadding: CGFloat = 12) {
        self.overlayTopPadding = overlayTopPadding
    }

    // 初始视野：Eindhoven 中心，约 60km 直径
    @State private var camera = MapCameraPosition.region(
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 51.4416, longitude: 5.4697),
            span: MKCoordinateSpan(latitudeDelta: 0.55, longitudeDelta: 0.55)))
    @State private var showRefreshError = false
    @State private var showLocationError = false
    @State private var showFilters = false
    /// 深链只消费一次——聚焦完成后不再抢镜头，之后这张图归用户自己操纵。
    @State private var focusConsumed = false

    /// 当前 visible region；onMapCameraChange 实时刷新。clustering 依赖它推 cell 大小。
    /// 初值与 camera 初值一致（Eindhoven 60km）。
    @State private var currentRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 51.4416, longitude: 5.4697),
        span: MKCoordinateSpan(latitudeDelta: 0.55, longitudeDelta: 0.55))

    /// 当前 cluster 列表（由 listings + currentRegion 决定）。
    /// **@State 缓存**：之前是 computed property，任何 MapStore 字段变化（包括
    /// selectedID 切换等无关项）都会触发 body 重算 → 重 cluster 2000 pin。
    /// 现在只在 `.onChange(of: store.listings.count)` 和跨 bucket 时刷新。
    @State private var clusters: [ListingCluster] = []

    /// 聚类后台任务句柄；新一轮 recompute 前取消上一轮，避免快速跨桶时
    /// 叠加多个 detached 任务、用旧 region 的结果覆盖新结果。
    @State private var clusterTask: Task<Void, Never>?

    private func recomputeClusters() {
        // 在主线程取值类型快照（store.listings 是 [MapListing] Sendable，
        // region 只取两个 Double delta），聚类计算丢到后台 detached 跑——
        // 2000 条的 grid 分桶 + 排序不再阻塞首屏/拖动那一帧。算完回主线程
        // 赋值 @State 触发渲染。
        let snapshot = store.visibleListings
        let latDelta = currentRegion.span.latitudeDelta
        let lngDelta = currentRegion.span.longitudeDelta

        clusterTask?.cancel()
        // @MainActor in：计算在 detached 后台跑，但任务体本身锚在主 actor，
        // 末尾 `clusters = result` 是主线程上的 @State 写入，并发安全。
        clusterTask = Task { @MainActor in
            let result = await Task.detached(priority: .userInitiated) {
                MapClustering.cluster(
                    listings: snapshot, latDelta: latDelta, lngDelta: lngDelta
                )
            }.value
            if Task.isCancelled { return }
            clusters = result
        }
    }

    /// 判断两个 region 是否跨过 log2 量化桶边界。
    /// 同桶内 cluster 不会变 → 不需要 withAnimation 包裹 currentRegion 更新，
    /// 避免每秒 60 次 withAnimation 带来的开销。
    private static func bucketsDiffer(
        _ a: MKCoordinateRegion, _ b: MKCoordinateRegion
    ) -> Bool {
        let qa = MapClustering.quantizeSpan(a.span.latitudeDelta)
        let qb = MapClustering.quantizeSpan(b.span.latitudeDelta)
        return qa != qb
    }

    var body: some View {
        @Bindable var store = store

        // 不再自带 NavigationStack；外层 BrowseView 提供。
        ZStack(alignment: .top) {
                Map(position: $camera, selection: $store.selectedID) {
                    ForEach(clusters) { cluster in
                        if cluster.isSingle, let l = cluster.single {
                            Annotation(l.name, coordinate: l.displayCoordinate) {
                                pinView(for: l)
                                    .transition(.asymmetric(
                                        insertion: .scale(scale: 0.4).combined(with: .opacity),
                                        removal: .scale(scale: 0.4).combined(with: .opacity)))
                            }
                            .tag(l.id)
                        } else {
                            Annotation("\(cluster.count) listings",
                                       coordinate: cluster.coordinate) {
                                clusterBubble(for: cluster)
                                    .transition(.asymmetric(
                                        insertion: .scale(scale: 0.5).combined(with: .opacity),
                                        removal: .scale(scale: 0.5).combined(with: .opacity)))
                            }
                            .annotationTitles(.hidden)
                        }
                    }
                }
                .onMapCameraChange(frequency: .continuous) { context in
                    // 关键：**只在跨 log2 桶时更新 currentRegion**。
                    //
                    // 为什么不更新 same-bucket：
                    // 1. cluster 计算只依赖 cellSize（同桶内不变）和房源绝对坐标
                    //    （永远不变）—— 中心点移动不影响 grid 分桶
                    // 2. 拖动时每帧更新 currentRegion → body 重算 → ForEach
                    //    迭代触发 SwiftUI 内部 diff，即便 cluster id 没变也可能
                    //    让 .transition 误触发动画 → 拖动时无关 pin 闪烁
                    // 3. 同桶时根本不更新就根本不重算，零开销零闪烁
                    if Self.bucketsDiffer(currentRegion, context.region) {
                        withAnimation(.easeInOut(duration: 0.22)) {
                            currentRegion = context.region
                        }
                        recomputeClusters()
                    }
                }
                .onAppear { recomputeClusters() }
                .onChange(of: store.listings.count) { _, _ in
                    recomputeClusters()
                }
                // 筛选是本地的，改一下就要立刻重画。visibleCount 变化能覆盖
                // 状态 chip / 城市 / 平台 / 租金 / 面积任意一项的改动。
                .onChange(of: store.visibleCount) { _, _ in
                    recomputeClusters()
                }
                .onChange(of: store.focusExtra?.id) { _, _ in
                    recomputeClusters()
                }
                .onChange(of: clusters.count) { _, _ in
                    focusIfNeeded()
                }
                // .realistic 会把地形高程画出来——深色模式下整张图变成一片
                // 诡异的蓝绿，房源图钉全被淹没。找房子跟地形没有关系。
                // pointsOfInterest 全关：截图里「Clown Juju」「Dental Clinics」
                // 「Global Dance Centre」这些跟找房子毫无关系，却在跟房源图钉
                // 抢注意力，密度还比图钉高得多。
                .mapStyle(.standard(elevation: .flat, pointsOfInterest: .excludingAll))
                .mapControls {
                    MapCompass()
                    MapScaleView()
                }
                .ignoresSafeArea(edges: .bottom)
                // 左上角：避开右上的 MapUserLocationButton/Compass/ScaleView

                .sheet(item: Binding(
                    get: { store.selected },
                    set: { _ in store.selectedID = nil }
                )) { l in
                    // 卡片高度随房源变：标题可能换行，同址提示可能占两行。
                    // 写死 detent 必然一边留白、一边裁掉——0.4 时下半截是空的，
                    // 收到 0.34 又把标题切了。套一层 ScrollView：装得下就不滚
                    // （scrollBounceBehavior(.basedOnSize)），装不下也丢不了内容。
                    ScrollView {
                        listingCard(l)
                    }
                    .scrollBounceBehavior(.basedOnSize)
                        // 收紧到贴着内容。0.4 时卡片下半截是空的——空白本身不是
                        // 留白，是"这里本来还该有东西"。
                        .presentationDetents([.fraction(0.42), .large])
                        // 小尺寸下地图仍可拖动：这是张地图，卡片弹出来不该把
                        // 它锁死。
                        .presentationBackgroundInteraction(
                            .enabled(upThrough: .fraction(0.42)))
                        .presentationDragIndicator(.visible)
                }

                if store.isLoading && store.listings.isEmpty {
                    ProgressView("Loading map…")
                        .padding(.top, 80)
                } else if let err = store.errorMessage, store.listings.isEmpty {
                    let apiErr = store.lastError
                    ContentUnavailableView {
                        Label(
                            apiErr?.errorDescription ?? "Unable to Load Map",
                            systemImage: apiErr?.systemImage ?? "map.slash")
                    } description: {
                        Text(err)
                    } actions: {
                        Button("Try Again") {
                            Task { await store.refresh() }
                        }
                    }
                }
            }
        .navigationBarTitleDisplayMode(.inline)
        // 控件全部沉到底部。浮在地图中上部时它们既盖住内容、又离拇指最远——
        // 单手拿 iPhone 时够不着。用 safeAreaInset 而不是 overlay：它会自动落在
        // tab bar 之上，不必去猜 tab bar 有多高，换机型/换系统版本也不会错位。
        .safeAreaInset(edge: .bottom, spacing: 0) { bottomBar }
        .overlay(alignment: .top) { topNotice }
        // 必须**居中**。放进上面那个 ZStack(alignment: .top) 的话会跟 topBar
        // 叠在一起——真机上第一版就是这样：计数、三个圆钮、chip 条全部压在
        // 说明卡上面，一个字都读不清。
        .overlay(alignment: .center) {
            if !store.listings.isEmpty && store.visibleCount == 0 {
                emptyFilterNotice
                    .padding(.horizontal, 24)
                    .transition(.opacity.combined(with: .scale(scale: 0.96)))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: store.visibleCount)
        .sheet(isPresented: $showFilters) { MapFilterSheet() }
        .task {
            if store.listings.isEmpty {
                await store.fetch()
            }
            await consumePendingFocus()
        }
        // 地图已经挂载时再点「在地图上查看」，.task 不会重跑，靠这个接住。
        .onChange(of: coord.pendingMapFocusID) { _, _ in
            Task { await consumePendingFocus() }
        }
        .onChange(of: store.focusID) { _, id in
            focusConsumed = (id == nil)
            focusIfNeeded()
        }
        .onDisappear {
            // 离开地图就把深链状态清掉：留着的话下次进来会莫名其妙又飞过去，
            // 而那次进入跟那条链接已经没有关系了。
            store.clearFocus()
            focusConsumed = false
        }
        .onChange(of: store.errorMessage) { _, new in
            showRefreshError = new != nil && !store.listings.isEmpty
        }
        .alert(
            store.lastError?.errorDescription ?? "Refresh Failed",
            isPresented: $showRefreshError
        ) {
            Button("OK") {}
        } message: {
            Text(store.errorMessage ?? "")
        }
        .alert("Location Unavailable", isPresented: $showLocationError) {
            Button("OK") {}
        } message: {
            Text("Allow location access in Settings to center the map on your current position.")
        }
    }

    private func mapControlButton(
        systemName: String,
        label: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 17, weight: .semibold))
                // 44×44 命中 iOS HIG 最小可点击区域，之前 42×42 差 2pt。
                .frame(width: 44, height: 44)
                .liquidGlass(Circle(), interactive: true)
        }
        .buttonStyle(.plain)
        // VoiceOver: SF symbol 自带 a11y label 但是英文符号名（如 "location fill"），
        // 这里覆盖成用户可理解的动作描述。
        .accessibilityLabel(label)
    }

    // MARK: - Pin

    @ViewBuilder
    private func pinView(for l: MapListing) -> some View {
        let color = pinColor(for: l.status)
        let selected = l.id == store.selectedID
        let size: CGFloat = selected ? 32 : 24

        ZStack {
            // 主彩色实心圆
            Circle()
                .fill(color.gradient)
                .frame(width: size, height: size)
                .shadow(color: .black.opacity(0.25),
                        radius: selected ? 6 : 3,
                        x: 0, y: selected ? 3 : 1)
            // 白色描边
            Circle()
                .stroke(.white, lineWidth: 2.5)
                .frame(width: size, height: size)
            // 中心房屋图标，区分点击对象
            Image(systemName: "house.fill")
                .font(.system(size: selected ? 14 : 10, weight: .bold))
                .foregroundStyle(.white)
        }
        .scaleEffect(selected ? 1.15 : 1.0)
        .animation(.spring(duration: 0.25), value: selected)
        // VoiceOver: 把 pin 当单个元素朗读"地址 · 状态"，hint 给打开详情。
        // 不依赖外层 Annotation(title) 的默认行为——显式更稳定。
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(l.name), \(l.status)")
        .accessibilityHint("Tap to view listing")
    }

    // MARK: - Cluster bubble

    /// 簇气泡：白边大圆 + 数字。颜色按簇内主导状态决定（available > lottery > other）。
    /// 点击 → ``zoomIn(to:)`` 把镜头缩到该簇 bounding 区域。
    @ViewBuilder
    private func clusterBubble(for cluster: ListingCluster) -> some View {
        let color = clusterColor(for: cluster)
        // 簇大小按 count log 缓增，避免一簇 50 套时气泡占满屏
        let size: CGFloat = clusterSize(count: cluster.count)
        Button {
            zoomIn(to: cluster)
        } label: {
            ZStack {
                Circle()
                    .fill(color.opacity(0.25))
                    .frame(width: size + 12, height: size + 12)
                Circle()
                    .fill(color.gradient)
                    .frame(width: size, height: size)
                    .shadow(color: .black.opacity(0.25), radius: 3, y: 1)
                Circle()
                    .stroke(.white, lineWidth: 2.5)
                    .frame(width: size, height: size)
                Text("\(cluster.count)")
                    .font(.system(size: size * 0.42, weight: .bold))
                    .foregroundStyle(.white)
            }
            // 2-3 套小簇视觉直径 34（halo 46）已经够，但显式拍 44×44 命中
            // 框 + Circle 形状命中，保证 HIG 合规 + 圆形精准点击（不会误触
            // 矩形角落）。视觉气泡仍按 clusterSize 渲染，不被撑大。
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        // VoiceOver: 簇当单个元素朗读"N 套房源"，hint 提示放大查看。
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(cluster.count) listings")
        .accessibilityHint("Tap to zoom in")
    }

    private func clusterSize(count: Int) -> CGFloat {
        // 2-3 套 → 34；4-9 套 → 40；10-24 → 46；25+ → 54
        switch count {
        case ..<4:  return 34
        case 4..<10: return 40
        case 10..<25: return 46
        default: return 54
        }
    }

    /// 簇颜色取簇内**最值得看**的那一档，优先级见 ``ListingStatus.byPriority``。
    ///
    /// 此前只认 available / lottery 两档，Reserved 和 Occupied 一起落进 `.blue`
    /// 兜底——既和 App 其它页面的配色对不上，又让「暂时没了」和「彻底没了」长得
    /// 一模一样。
    private func clusterColor(for cluster: ListingCluster) -> Color {
        var best = ListingStatus.occupied
        for l in cluster.listings where l.statusKind.priority < best.priority {
            best = l.statusKind
        }
        return best.color
    }

    /// 点击簇：相机动画到该簇 bounding 区域，触发自动 zoom-in。
    /// 下一次 onMapCameraChange 会用新 region 重算 clusters，自动展开成更细的簇 / 单 pin。
    private func zoomIn(to cluster: ListingCluster) {
        let region = cluster.boundingRegion()
        withAnimation(.easeInOut(duration: 0.4)) {
            camera = .region(region)
        }
    }

    private func centerOnUserLocation() {
        locationProvider.requestLocation(
            onUpdate: { coordinate in
                withAnimation(.easeInOut(duration: 0.35)) {
                    camera = .region(MKCoordinateRegion(
                        center: coordinate,
                        span: MKCoordinateSpan(latitudeDelta: 0.08, longitudeDelta: 0.08)
                    ))
                }
            },
            onDenied: {
                showLocationError = true
            }
        )
    }

    private func pinColor(for status: String) -> Color {
        ListingStatus.from(status).color
    }

    // MARK: - Bottom bar

    /// 状态 chip 条 + 计数 + 三个圆钮，全部贴着 tab bar 上方。
    ///
    /// 原先它们浮在地图中上部：既盖住地图内容，又离拇指最远——单手拿 iPhone
    /// 时根本够不着。地图类 App 把常用控件放底部是通例，原因就是这个。
    ///
    /// 定位提示条仍留在**顶部**：它是需要读的一段话，不是要点的控件，放在
    /// 底部会跟这一堆按钮挤在一起。
    private var bottomBar: some View {
        GlassGroup(spacing: 12) {
        VStack(alignment: .leading, spacing: 10) {
            // 顺序：圆钮在上、chip 条在下、再下面是 tab bar。
            //
            // chip 是这里最常动的一格（切个状态看一眼），贴着 tab bar 放在最下
            // 就是离拇指最近的位置；三个圆钮用得少，让一层给它。
            HStack(spacing: 10) {
                countBadge
                Spacer(minLength: 8)
                mapControlButton(systemName: "line.3.horizontal.decrease.circle",
                                 label: "Filter listings") { showFilters = true }
                mapControlButton(systemName: "location.fill",
                                 label: "Center on my location") { centerOnUserLocation() }
                mapControlButton(systemName: "arrow.clockwise",
                                 label: "Refresh listings") {
                    Task { await store.refresh() }
                }
                .disabled(store.isLoading)
            }
            .padding(.horizontal, MapLayout.horizontalInset)

            MapStatusChips()
        }
        }
        .padding(.bottom, 8)
    }

    /// 定位提示条单独留在顶部——它是要读的文字，不是要点的控件。
    private var topNotice: some View {
        Group {
            if let notice = store.focusNotice {
                focusNoticeBar(notice)
                    .padding(.top, overlayTopPadding + 54)
            }
        }
    }

    /// 筛完一套不剩时的说明卡。
    ///
    /// 原因**从实际数据算出来**，不写死。此前这里硬写了一句「多数已出租或预留」，
    /// 那是猜的：空图若是城市或租金条件筛出来的，这句话就是错的，而界面上看不出来。
    private var emptyFilterNotice: some View {
        let breakdown = store.emptyBreakdown
        return VStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(0.20))
                    .frame(width: 46, height: 46)
                Image(systemName: "line.3.horizontal.decrease")
                    .font(.system(size: 19, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
            }

            VStack(spacing: 5) {
                Text("No listings match")
                    .font(.headline)
                Text("\(breakdown.total) hidden by the current filter")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            // 各档实际藏了几套——按数量降序，用各自的颜色，和 chip 条对得上。
            if !breakdown.byStatus.isEmpty {
                HStack(spacing: 6) {
                    ForEach(breakdown.byStatus.prefix(3), id: \.status) { item in
                        HStack(spacing: 5) {
                            Circle()
                                .fill(item.status.color)
                                .frame(width: 7, height: 7)
                            Text("\(item.count)")
                                .fontWeight(.semibold)
                                .monospacedDigit()
                            Text(item.status.label)
                                .foregroundStyle(.secondary)
                        }
                        .font(.caption)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 5)
                        .background(Color.primary.opacity(0.06), in: Capsule())
                    }
                }
            }

            // 状态之外还有别的条件在起作用时才提——不提的话，用户会以为
            // 只要打开那几档就够了。
            if breakdown.byOtherFilters > 0 {
                Text("\(breakdown.byOtherFilters) more excluded by city, platform, rent or area")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            Button {
                store.showEverything()
            } label: {
                Text("Show all \(breakdown.total)")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
            }
            .glassProminentButtonStyle()
            .controlSize(.regular)
        }
        .padding(.horizontal, 20)
        .padding(.top, 22)
        .padding(.bottom, 18)
        .frame(maxWidth: 300)
        // 这张卡**不用玻璃**。
        //
        // Liquid Glass 是给悬浮小控件的：面积小、内容少、背景透过来是加分。
        // 一整张说明卡糊在地图上时，玻璃的折射和模糊会把卡片自己的内容也搅浑
        // ——真机上看就是"多加了一层模糊"。这里要的是把地图挡住、把字读清楚，
        // 所以用不透明度更高的 thickMaterial。
        .background(.thickMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.08), lineWidth: 0.5))
        .shadow(color: .black.opacity(0.22), radius: 20, y: 8)
    }

    /// 深链没能直接落到图上时，说明是**哪一种**没落上。
    ///
    /// 三种原因用户能做的事完全不同：等地址被解析 / 改一下筛选 / 这条链接作废了。
    /// 合并成一句「没找到」的话，三种情况在界面上长得一模一样。
    private func focusNoticeBar(_ notice: MapStore.FocusNotice) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: notice.systemImage)
                .font(.system(size: 13, weight: .semibold))
            Text(notice.text)
                .font(.footnote)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            Button {
                store.focusNotice = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .frame(width: 28, height: 28)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .liquidGlass(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .padding(.horizontal, 12)
    }

    private var countBadge: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Image(systemName: "house.circle.fill")
                    .foregroundStyle(.blue)
                // 「1 / 9」没人看得懂——真机上第一反应就是「这什么意思」。
                // 写清楚是「显示 N / 共 M」，并且只在真的筛掉了东西时才显示分母。
                if store.visibleCount == store.listings.count {
                    Text("\(store.listings.count)")
                        .font(.subheadline).fontWeight(.medium).monospacedDigit()
                } else {
                    Text("\(store.visibleCount)")
                        .font(.subheadline).fontWeight(.semibold).monospacedDigit()
                    Text("/ \(store.listings.count)")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            if store.uncached > 0 {
                Text("\(store.uncached) without coords")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        // 不开 interactive：它只是个读数，按下去会形变的话看起来像能点。
        .liquidGlass(Capsule())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(store.visibleCount) of \(store.listings.count) listings shown")
    }

    // MARK: - Deep link focus

    /// 把镜头移到深链指定的那一套，并弹出它的卡片。
    ///
    /// 要等 clusters 算完才能确认那枚 pin 真的在图上——所以这个函数会被数据、
    /// 筛选、聚类三处变化各调一次，靠 ``focusConsumed`` 保证只生效一次。
    /// 取走 coordinator 上挂着的待聚焦 id。
    ///
    /// 中转一道而不是让详情页直接调 MapStore：点「在地图上查看」的那一刻，
    /// 地图视图可能还没挂载（iPhone 上它在 Browse 的另一个模式里）。
    private func consumePendingFocus() async {
        guard let id = coord.pendingMapFocusID else { return }
        coord.pendingMapFocusID = nil
        focusConsumed = false
        await store.focus(on: id)
        focusIfNeeded()
    }

    private func focusIfNeeded() {
        guard !focusConsumed, let id = store.focusID else { return }
        let target = store.visibleListings.first { $0.id == id }
        guard let l = target else { return }
        focusConsumed = true
        withAnimation(.easeInOut(duration: 0.45)) {
            camera = .region(MKCoordinateRegion(
                center: l.displayCoordinate,
                span: MKCoordinateSpan(latitudeDelta: 0.004, longitudeDelta: 0.004)))
        }
        // **不**自动弹卡片。
        //
        // 用户是从这套房的详情页点过来的——他已经知道是哪套了，弹一张卡片把刚看过
        // 的信息再念一遍，还盖住半张他专门来看的地图。这里要回答的问题只有一个：
        // 它在哪。图钉自己有加粗描边+放大（见 makeIcon 的 isFocus），够了。
        //
        // 想看详情点那个图钉就行，和地图上其它房源一样。
    }

    // MARK: - Bottom card

    @ViewBuilder
    private func listingCard(_ l: MapListing) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            // ── 标题 ──────────────────────────────────────────────
            // 平台徽标从标题旁边挪到了下面那行。挤在标题右边时，长房源名会被它
            // 顶得换行，而"Occupied"又在更右边——一行里塞三样东西，谁都不突出。
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(l.name)
                    .font(.title3).fontWeight(.semibold)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                statusBadge(l.status)
            }

            // 平台 + 地点。地点为空、或者已经被房源名包含时就不重复——
            // Xior 的 city 字段是楼盘名（"Amsterdam Naritaweg"），而房源名是
            // "Amsterdam Naritaweg 155C"，照原样并排会念两遍同一件事。
            HStack(spacing: 8) {
                PlatformBadge(source: l.source, size: .medium)
                if let place = placeText(for: l) {
                    Text(place)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(.top, -8)

            // ── 三格数据 ──────────────────────────────────────────
            // 等宽 + 分隔线。原先是 Label 横排，间距由文字长短决定，三样东西
            // 疏密不一，看着像没对齐。
            HStack(spacing: 0) {
                statCell(l.priceRaw.isEmpty ? "—" : l.priceRaw, caption: "per month")
                statDivider
                statCell(l.area.isEmpty ? "—" : l.area, caption: "area")
                statDivider
                // 哨兵日期（2050-01-01 =「未定」）显示成 "—"，不冒充成日期。
                statCell(availableText(l), caption: "available")
            }
            .padding(.vertical, 10)
            .background(Color.primary.opacity(0.05),
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))

            // 同址散开之后位置是**近似值**。不写这一句的话，用户会以为图钉就是
            // 门牌号——而这几套其实只是共用一个街道地址。
            if l.stackCount > 1 {
                Label {
                    // 完整句子、首字母大写。原文 "5 units at this address, spread
                    // out; positions are approximate" 以数字开头、用分号拼接，
                    // 读起来是个残句；而这句话是在更正图钉的位置，必须说清楚。
                    Text("This address has \(l.stackCount) listings. Their pins are spread apart so that each one can be tapped, so the positions shown are approximate.")
                } icon: {
                    Image(systemName: "circle.grid.2x2")
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }

            // ── 操作 ──────────────────────────────────────────────
            // 主操作整宽独占一行；两个次要动作各占一半。原先是"整宽+圆钮"再
            // 叠一个整宽，三种宽度堆在一起，没有一条边是对齐的。
            VStack(spacing: 10) {
                Button {
                    let id = l.id
                    let title = l.name
                    store.selectedID = nil   // close sheet
                    if UIDevice.current.userInterfaceIdiom == .pad {
                        coord.openListing(id: id, titleHint: title)
                    } else {
                        coord.listingsPath.append(.byId(id, titleHint: title))
                    }
                } label: {
                    Label("View Details", systemImage: "arrow.right.circle.fill")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)

                HStack(spacing: 10) {
                    Button {
                        // 传**真实坐标**：同址那几套在图上被摆成一圈只是为了能
                        // 分别点到，圈上的点谁都不是真的门牌位置。
                        AppleMaps.openDirections(to: l.coordinate, name: l.name)
                    } label: {
                        // ⚠️ 必须带 .circle.fill：裸的 arrow.triangle.turn.up.right
                        // 不是一个存在的 SF Symbol。名字写错不报错、不崩溃，
                        // 只是**什么都不画**——上一版就这么把图标弄丢了。
                        Label("Directions",
                              systemImage: "arrow.triangle.turn.up.right.circle.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)

                    if let url = URL(string: l.url), !l.url.isEmpty {
                        Link(destination: url) {
                            Label("Website", systemImage: "safari")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }
            .controlSize(.large)
        }
        .padding(20)
    }

    /// 一格数据：数值在上，说明在下。
    private func statCell(_ value: String, caption: LocalizedStringKey) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.subheadline).fontWeight(.semibold)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Text(caption)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity)
    }

    private var statDivider: some View {
        Rectangle()
            .fill(Color.primary.opacity(0.08))
            .frame(width: 1, height: 26)
    }

    /// 地点行的文字。实现在 PlaceSummary——日历行用的是同一份。
    private func placeText(for l: MapListing) -> String? {
        PlaceSummary.text(name: l.name, parts: [l.neighborhood, l.city])
    }

    private func availableText(_ l: MapListing) -> String {
        guard !l.availableFrom.isEmpty,
              !ServerTime.isSentinelDate(l.availableFrom) else { return "—" }
        return ServerTime.displayDate(l.availableFrom)
    }

    private func statusBadge(_ status: String) -> some View {
        let kind = ListingStatus.from(status)
        return Text(kind.label)
            .font(.caption).fontWeight(.semibold)
            .lineLimit(1)
            .padding(.horizontal, 9)
            .padding(.vertical, 4)
            .background(kind.color.opacity(0.18), in: Capsule())
            .foregroundStyle(kind.color)
    }

}

private final class UserLocationProvider: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var pendingUpdate: ((CLLocationCoordinate2D) -> Void)?
    private var pendingDenied: (() -> Void)?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func requestLocation(
        onUpdate: @escaping (CLLocationCoordinate2D) -> Void,
        onDenied: @escaping () -> Void
    ) {
        pendingUpdate = onUpdate
        pendingDenied = onDenied

        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        case .denied, .restricted:
            finishDenied()
        @unknown default:
            finishDenied()
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        case .denied, .restricted:
            finishDenied()
        case .notDetermined:
            break
        @unknown default:
            finishDenied()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let coordinate = locations.last?.coordinate else {
            finishDenied()
            return
        }
        pendingUpdate?(coordinate)
        clearPending()
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        finishDenied()
    }

    private func finishDenied() {
        pendingDenied?()
        clearPending()
    }

    private func clearPending() {
        pendingUpdate = nil
        pendingDenied = nil
    }
}
