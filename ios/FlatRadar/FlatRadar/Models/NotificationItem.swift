import Foundation

struct NotificationItem: Decodable, Identifiable, Sendable {
    let id: Int
    let createdAt: String
    let type: String
    let title: String
    let body: String
    let url: String
    let listingID: String
    let read: Int

    enum CodingKeys: String, CodingKey {
        case id, type, title, body, url, read
        case createdAt = "created_at"
        case listingID = "listing_id"
    }

    var isRead: Bool { read != 0 }

    func markedRead() -> NotificationItem {
        NotificationItem(id: id, createdAt: createdAt, type: type,
                         title: title, body: body, url: url,
                         listingID: listingID, read: 1)
    }
}
