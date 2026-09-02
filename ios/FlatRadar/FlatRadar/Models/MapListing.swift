import CoreLocation
import Foundation

/// 地图视图中的单个房源；后端 `/api/v1/map` `listings[]` 数组的元素。
///
/// 与 ``Listing`` 的区别
/// --------------------
/// MapListing 是地图专用 DTO：含 ``lat`` / ``lng`` 坐标，不含完整 feature 列表。
/// 点击 pin → 弹卡 → 点详情按钮时，再走 ``ListingRoute.byId`` 让 ListingDetailView
/// 自己 ``getListing(id:)`` 拉全字段。
struct MapListing: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let status: String
    let source: String?
    let priceRaw: String
    let availableFrom: String
    let url: String
    let city: String
    let neighborhood: String
    let building: String
    let area: String
    let address: String
    let lat: Double
    let lng: Double

    /// 画图钉用的坐标，以及这个地址上一共有几套。
    ///
    /// 一栋楼的每个单元共用同一个街道地址，geocode 出来是**完全相同**的坐标。
    /// 网格聚类对重合点在任何 cell 大小下都归同一格，点击展开又会被
    /// ``ListingCluster.boundingRegion`` 的 minSpan 兜成固定视野——于是同址的
    /// 那几套**在任何缩放下都碰不到**。服务端已经把它们摆成一圈
    /// （`app/services/listing_service.spread_stacked_coords`），这里只负责取值。
    ///
    /// 可选是为了兼容还没更新的服务端：缺字段时退回真实坐标，而不是丢掉这个点。
    let displayLat: Double?
    let displayLng: Double?
    let stackN: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, status, source, url, city, neighborhood, building, area, address, lat, lng
        case priceRaw = "price_raw"
        case availableFrom = "available_from"
        case displayLat = "display_lat"
        case displayLng = "display_lng"
        case stackN = "stack_n"
    }

    /// 真实坐标。用于「这套房到底在哪」——比如日后接入导航。
    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lng)
    }

    /// 画在地图上的坐标。同址散开后是**近似值**，``stackCount`` > 1 时
    /// 界面必须说明这一点——不说的话用户会以为图钉就是门牌号。
    var displayCoordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(
            latitude: displayLat ?? lat,
            longitude: displayLng ?? lng)
    }

    /// 这个地址上一共几套。服务端没给时按 1 处理。
    var stackCount: Int { max(1, stackN ?? 1) }

    /// 归一化后的状态档。判据见 ``ListingStatus``，与 Web 同一份。
    var statusKind: ListingStatus { ListingStatus.from(status) }

    /// 见 ``Platform``——全 App 唯一一份映射。此前这里只认得 H2S 和 OD，
    /// 其余五个平台在地图弹卡上显示成大写的 source key。
    var sourceShortText: String { Platform.shortName(source ?? "holland2stay") }

    var sourceDisplayText: String { Platform.displayName(source ?? "holland2stay") }
}

/// `GET /api/v1/map` 响应包络。
struct MapResponse: Decodable, Sendable {
    let listings: [MapListing]
    let uncached: Int
}

/// `GET /api/v1/map/locate` 结果。
///
/// 三种「看不到」必须分开报——合并成一句「没找到」的话，「等管理员解析地址」
/// 「这个链接作废了」「改一下筛选就能看到」在界面上长得一模一样，而用户能做的
/// 事完全不同。
struct MapLocateResult: Decodable, Sendable {
    let ok: Bool
    let reason: String?
    let listing: MapListing?

    enum Reason: String {
        case notFound = "not_found"
        case noCoords = "no_coords"
    }

    var parsedReason: Reason? { reason.flatMap(Reason.init(rawValue:)) }
}
