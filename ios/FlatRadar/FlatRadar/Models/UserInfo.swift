import Foundation

/// From /auth/me -> user field (null for admin)
struct UserInfo: Decodable, Sendable {
    let id: String
    let name: String
    let enabled: Bool
    let notificationsEnabled: Bool
    let listingFilter: ListingFilter

    enum CodingKeys: String, CodingKey {
        case id, name, enabled
        case notificationsEnabled = "notifications_enabled"
        case listingFilter = "listing_filter"
    }
}
