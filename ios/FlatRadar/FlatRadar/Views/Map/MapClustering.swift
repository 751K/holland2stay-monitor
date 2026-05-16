import CoreLocation
import Foundation
import MapKit

/// MapView 的"单个点 or 一簇"统一抽象。
///
/// SwiftUI 的 `Map { Annotation }` 不原生支持 clustering——zoom 出去时密集房源
/// pin 会糊成一团。这里在 Swift 侧用最简单的 **grid bucket** 算法做手动聚合：
/// 把当前可见区域按 lat/lng 等距切成网格，落在同一格的房源归并成一个簇，
/// 视图渲染时单条画 pin、多条画带数字的气泡。
struct ListingCluster: Identifiable, Hashable {
    /// 形如 ``"single:<listingId>"`` 或 ``"cluster:<lat>,<lng>"``，
    /// `Map(selection:)` 用同一个 string 选中（单 pin 才有效）。
    let id: String
    let coordinate: CLLocationCoordinate2D
    let listings: [MapListing]

    var isSingle: Bool { listings.count == 1 }
    var count: Int { listings.count }
    var single: MapListing? { listings.first }

    static func == (lhs: ListingCluster, rhs: ListingCluster) -> Bool {
        lhs.id == rhs.id
    }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }

    /// 把这一簇所有点的 bounding 加 padding 得出 zoom-in 用的 region。
    func boundingRegion(paddingFactor: Double = 1.6) -> MKCoordinateRegion {
        let lats = listings.map(\.lat)
        let lngs = listings.map(\.lng)
        let minLat = lats.min() ?? coordinate.latitude
        let maxLat = lats.max() ?? coordinate.latitude
        let minLng = lngs.min() ?? coordinate.longitude
        let maxLng = lngs.max() ?? coordinate.longitude
        // 至少给一个 minSpan 避免单点 bounding 是 0
        let minSpan = 0.005
        let latSpan = max(maxLat - minLat, minSpan) * paddingFactor
        let lngSpan = max(maxLng - minLng, minSpan) * paddingFactor
        return MKCoordinateRegion(
            center: CLLocationCoordinate2D(
                latitude: (minLat + maxLat) / 2,
                longitude: (minLng + maxLng) / 2),
            span: MKCoordinateSpan(latitudeDelta: latSpan, longitudeDelta: lngSpan))
    }
}

/// Grid-bucket clustering。
///
/// 参数
/// ----
/// - listings   : 所有有坐标的房源
/// - region     : 当前可见 ``MKCoordinateRegion``；用来推 cell 大小
/// - targetCells: 目标横向格子数；默认 12，约等于"60pt 像素一格"在 iPhone 14
///                竖屏 (≈400pt 宽) 上的密度。值越大簇越细，越小聚得越狠。
///
/// 算法 O(N)：
///   cellLat = region.span.latitudeDelta / targetCells
///   cellLng = region.span.longitudeDelta / targetCells
///   for l in listings:
///       key = (floor(l.lat / cellLat), floor(l.lng / cellLng))
///       grid[key].append(l)
///   归并：cell 内点数 ≥ 2 → cluster；==1 → single
///
/// 复杂度对 listings < 千级数据完全够用，每次 region change 重算 < 1ms。
enum MapClustering {

    /// 把连续 ``span`` 量化到 log2 桶（默认 step=0.5 → 每 √2 倍缩放 = 1 桶）。
    ///
    /// 为什么要量化
    /// ------------
    /// 连续缩放过程中 ``region.span`` 每帧都在变；如果不量化，cellSize 也每
    /// 帧在变，桶网格 → cluster ID → SwiftUI annotation 集合都在抖 → 闪烁。
    ///
    /// 量化后，**桶内任意缩放映射到同一个 cellSize**，grid 稳定 → cluster ID
    /// 稳定 → 视图复用 → 零闪烁。只有跨桶边界（约 1.4× 缩放）才重算一次。
    static func quantizeSpan(_ span: Double, step: Double = 0.5) -> Double {
        let safe = max(span, 1e-6)
        let snapped = (log2(safe) / step).rounded() * step
        return pow(2.0, snapped)
    }

    static func cluster(
        listings: [MapListing],
        region: MKCoordinateRegion,
        targetCells: Double = 12
    ) -> [ListingCluster] {
        guard !listings.isEmpty else { return [] }

        // 各自量化 lat/lng span → 缩放过程中 grid 大部分时间不变（除了
        // 跨桶边界那一刻）。这是消除闪烁的关键。
        let qLat = Self.quantizeSpan(region.span.latitudeDelta)
        let qLng = Self.quantizeSpan(region.span.longitudeDelta)
        let cellLat = max(qLat / targetCells, 1e-6)
        let cellLng = max(qLng / targetCells, 1e-6)

        struct Key: Hashable { let lat: Int; let lng: Int }
        var grid: [Key: [MapListing]] = [:]
        grid.reserveCapacity(listings.count)
        for l in listings {
            let k = Key(
                lat: Int((l.lat / cellLat).rounded(.down)),
                lng: Int((l.lng / cellLng).rounded(.down)))
            grid[k, default: []].append(l)
        }

        var out: [ListingCluster] = []
        out.reserveCapacity(grid.count)
        for (k, items) in grid {
            if items.count == 1, let one = items.first {
                out.append(ListingCluster(
                    id: "single:\(one.id)",
                    coordinate: one.coordinate,
                    listings: items))
            } else {
                // 簇中心 = 几何平均
                let avgLat = items.map(\.lat).reduce(0, +) / Double(items.count)
                let avgLng = items.map(\.lng).reduce(0, +) / Double(items.count)
                // ID 用 **grid 坐标** 而不是 firstId / count——这样只要 cellSize 不变、
                // 桶里点的归属不变，cluster ID 就稳定，SwiftUI 复用 annotation view，
                // 不会出现"看似同一团但闪一下"。
                out.append(ListingCluster(
                    id: "cluster:\(k.lat):\(k.lng)",
                    coordinate: CLLocationCoordinate2D(latitude: avgLat, longitude: avgLng),
                    listings: items))
            }
        }
        return out
    }
}
