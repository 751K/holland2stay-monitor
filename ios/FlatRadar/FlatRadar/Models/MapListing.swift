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

    enum CodingKeys: String, CodingKey {
        case id, name, status, url, city, neighborhood, building, area, address, lat, lng
        case priceRaw = "price_raw"
        case availableFrom = "available_from"
    }

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lng)
    }
}

/// `GET /api/v1/map` 响应包络。
struct MapResponse: Decodable, Sendable {
    let listings: [MapListing]
    let uncached: Int
}
