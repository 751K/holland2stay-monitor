import Foundation

/// User's listing filter config, from /auth/me -> user.listing_filter.
/// Mirrors backend config.ListingFilter fields exactly.
struct ListingFilter: Decodable, Equatable, Sendable {
    let maxRent: Double?
    let minArea: Double?
    let minFloor: Int?
    let allowedOccupancy: [String]
    let allowedTypes: [String]
    let allowedNeighborhoods: [String]
    let allowedCities: [String]
    let allowedContract: [String]
    let allowedTenant: [String]
    let allowedOffer: [String]
    let allowedFinishing: [String]
    let allowedEnergy: String

    enum CodingKeys: String, CodingKey {
        case maxRent = "max_rent"
        case minArea = "min_area"
        case minFloor = "min_floor"
        case allowedOccupancy = "allowed_occupancy"
        case allowedTypes = "allowed_types"
        case allowedNeighborhoods = "allowed_neighborhoods"
        case allowedCities = "allowed_cities"
        case allowedContract = "allowed_contract"
        case allowedTenant = "allowed_tenant"
        case allowedOffer = "allowed_offer"
        case allowedFinishing = "allowed_finishing"
        case allowedEnergy = "allowed_energy"
    }
}
