import CoreLocation
import MapKit

/// 唤起「地图」App 做导航。
///
/// 为什么单独放一处
/// ----------------
/// 地图弹卡和房源详情页都会用到，而**用哪个坐标**这件事一旦写错不会报错，只会
/// 把人导到偏 20 米的地方——必须只有一份判断。
enum AppleMaps {

    /// 导航到某套房源。
    ///
    /// - Parameters:
    ///   - coordinate: **真实坐标**，不是散开后的显示坐标。
    ///     同址的几套在图上被摆成一圈只是为了能分别点到，圈上那些点谁都不是
    ///     真的门牌位置；导航必须回到 geocode 出来的那一个点。
    ///   - name: 地图 App 里目的地大头针的标题。
    static func openDirections(to coordinate: CLLocationCoordinate2D, name: String) {
        guard CLLocationCoordinate2DIsValid(coordinate) else { return }
        let item = MKMapItem(placemark: MKPlacemark(coordinate: coordinate))
        item.name = name
        // 用 Default 而不是写死驾车：荷兰这边骑车/公交才是常态，
        // 而用户在地图 App 里的偏好本来就该由他自己定。
        item.openInMaps(launchOptions: [
            MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeDefault
        ])
    }
}
