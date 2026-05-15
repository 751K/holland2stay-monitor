import Foundation

struct Listing: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let status: String
    let priceRaw: String?
    let priceValue: Double?
    let availableFrom: String?
    let features: [String]
    let featureMap: [String: String]
    let url: String
    let city: String
    let firstSeen: String?
    let lastSeen: String?

    enum CodingKeys: String, CodingKey {
        case id, name, status, features, url, city
        case priceRaw = "price_raw"
        case priceValue = "price_value"
        case availableFrom = "available_from"
        case featureMap = "feature_map"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }
}
