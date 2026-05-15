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

    // 初始视野：Eindhoven 中心，约 60km 直径
    @State private var camera = MapCameraPosition.region(
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 51.4416, longitude: 5.4697),
            span: MKCoordinateSpan(latitudeDelta: 0.55, longitudeDelta: 0.55)))
    @State private var showRefreshError = false

    var body: some View {
        @Bindable var store = store

        // 不再自带 NavigationStack；外层 BrowseView 提供。
        ZStack(alignment: .top) {
                Map(position: $camera, selection: $store.selectedID) {
                    ForEach(store.listings) { l in
                        Annotation(l.name, coordinate: l.coordinate) {
                            pinView(for: l)
                        }
                        .tag(l.id)
                    }
                }
                .mapStyle(.standard(elevation: .realistic))
                .mapControls {
                    MapUserLocationButton()
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
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await store.refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(store.isLoading)
            }
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
        .padding(.top, 8)
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
                    Label(l.availableFrom, systemImage: "calendar")
                }
            }
            .font(.footnote)
            .foregroundStyle(.secondary)

            // Action
            HStack(spacing: 8) {
                Button {
                    let id = l.id
                    store.selectedID = nil   // close sheet
                    coord.openListing(id: id)
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
