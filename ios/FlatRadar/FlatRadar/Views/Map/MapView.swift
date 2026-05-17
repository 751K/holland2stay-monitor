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
/// - 选中状态用 ``.mapStyle(.standard(elevation:.realistic))``——美观且性能可接受
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

    /// 当前 visible region；onMapCameraChange 实时刷新。clustering 依赖它推 cell 大小。
    /// 初值与 camera 初值一致（Eindhoven 60km）。
    @State private var currentRegion = MKCoordinateRegion(
        center: CLLocationCoordinate2D(latitude: 51.4416, longitude: 5.4697),
        span: MKCoordinateSpan(latitudeDelta: 0.55, longitudeDelta: 0.55))

    /// 当前 cluster 列表（由 listings + currentRegion 决定）。
    private var clusters: [ListingCluster] {
        MapClustering.cluster(listings: store.listings, region: currentRegion)
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
                            Annotation(l.name, coordinate: l.coordinate) {
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
                    }
                }
                .mapStyle(.standard(elevation: .realistic))
                .mapControls {
                    MapCompass()
                    MapScaleView()
                }
                .ignoresSafeArea(edges: .bottom)
                // 左上角：避开右上的 MapUserLocationButton/Compass/ScaleView
                .overlay(alignment: .topLeading) {
                    countBadge
                }
                .sheet(item: Binding(
                    get: { store.selected },
                    set: { _ in store.selectedID = nil }
                )) { l in
                    listingCard(l)
                        .presentationDetents([.fraction(0.32), .medium])
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
        .overlay(alignment: .topTrailing) {
            VStack(spacing: 10) {
                mapTopButton(systemName: "location.fill",
                             label: "Center on my location") {
                    centerOnUserLocation()
                }
                mapTopButton(systemName: "arrow.clockwise",
                             label: "Refresh listings") {
                    Task { await store.refresh() }
                }
                .disabled(store.isLoading)
            }
            .padding(.trailing, 16)
            .padding(.top, overlayTopPadding + 54)
        }
        .task {
            if store.listings.isEmpty {
                await store.fetch()
            }
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

    private func mapTopButton(
        systemName: String,
        label: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 17, weight: .semibold))
                // 44×44 命中 iOS HIG 最小可点击区域，之前 42×42 差 2pt。
                .frame(width: 44, height: 44)
                .background(.regularMaterial, in: Circle())
                .shadow(color: .black.opacity(0.10), radius: 5, y: 2)
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

    /// 簇颜色取簇内最高优先级状态：Available > Lottery > 其它。
    private func clusterColor(for cluster: ListingCluster) -> Color {
        var hasAvailable = false
        var hasLottery = false
        for l in cluster.listings {
            let s = l.status.lowercased()
            if s.contains("available to book") { hasAvailable = true }
            else if s.contains("lottery") { hasLottery = true }
        }
        if hasAvailable { return .green }
        if hasLottery { return .orange }
        return .blue
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
        let s = status.lowercased()
        if s.contains("available to book") { return .green }
        if s.contains("lottery") { return .orange }
        if s.contains("not available") { return .gray }
        return .blue
    }

    // MARK: - Top badge

    private var countBadge: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Image(systemName: "house.circle.fill")
                    .foregroundStyle(.blue)
                Text("\(store.listings.count) listings")
                    .font(.subheadline)
                    .fontWeight(.medium)
            }
            if store.uncached > 0 {
                Text("\(store.uncached) without coords")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .shadow(color: .black.opacity(0.08), radius: 4, y: 2)
        .padding(.top, overlayTopPadding + 54)
        .padding(.leading, 12)
    }

    // MARK: - Bottom card

    @ViewBuilder
    private func listingCard(_ l: MapListing) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            // Title row
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(l.name)
                        .font(.headline)
                        .lineLimit(2)
                    HStack(spacing: 6) {
                        Text(l.city)
                        if !l.neighborhood.isEmpty {
                            Text("·")
                            Text(l.neighborhood)
                        }
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                Spacer()
                statusBadge(l.status)
            }

            // Stats row
            HStack(spacing: 16) {
                if !l.priceRaw.isEmpty {
                    Label(l.priceRaw + "/mo", systemImage: "eurosign.circle")
                }
                if !l.area.isEmpty {
                    Label(l.area, systemImage: "square.dashed")
                }
                if !l.availableFrom.isEmpty {
                    Label(ServerTime.displayDate(l.availableFrom), systemImage: "calendar")
                }
            }
            .font(.footnote)
            .foregroundStyle(.secondary)

            // Action
            HStack(spacing: 8) {
                Button {
                    let id = l.id
                    store.selectedID = nil   // close sheet
                    if UIDevice.current.userInterfaceIdiom == .pad {
                        coord.openListing(id: id)
                    } else {
                        coord.listingsPath.append(.byId(id))
                    }
                } label: {
                    Label("View Details", systemImage: "arrow.right.circle.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)

                if let url = URL(string: l.url), !l.url.isEmpty {
                    Link(destination: url) {
                        Image(systemName: "safari")
                    }
                    .buttonStyle(.bordered)
                }
            }
        }
        .padding()
    }

    @ViewBuilder
    private func statusBadge(_ status: String) -> some View {
        let color = pinColor(for: status)
        Text(shortStatus(status))
            .font(.caption)
            .fontWeight(.medium)
            .lineLimit(1)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }

    private func shortStatus(_ s: String) -> String {
        let lower = s.lowercased()
        if lower.contains("available to book") { return String(localized: "Available") }
        if lower.contains("lottery") { return String(localized: "Lottery") }
        if lower.contains("not available") { return String(localized: "Unavailable") }
        return s
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
