import XCTest
@testable import FlatRadar

final class NotificationItemTests: XCTestCase {

    // MARK: - Decoding

    func test_decodes_all_fields() throws {
        let json = """
        {
            "id": 1, "created_at": "2026-06-01T12:00:00Z", "type": "new_listing",
            "title": "Test Title", "body": "Test Body", "url": "https://x.com",
            "listing_id": "abc123", "read": 0
        }
        """
        let item = try JSONDecoder().decode(NotificationItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.id, 1)
        XCTAssertEqual(item.type, "new_listing")
        XCTAssertEqual(item.title, "Test Title")
        XCTAssertEqual(item.body, "Test Body")
        XCTAssertEqual(item.listingID, "abc123")
        XCTAssertEqual(item.read, 0)
        XCTAssertFalse(item.isRead)
    }

    func test_isRead_returns_true_when_read_nonzero() throws {
        let json = """
        {"id": 1, "created_at": "x", "type": "x", "title": "x", "body": "x", "url": "x", "listing_id": "x", "read": 1}
        """
        let item = try JSONDecoder().decode(NotificationItem.self, from: Data(json.utf8))
        XCTAssertTrue(item.isRead)
    }

    // MARK: - listingTitleHint

    func test_listingTitleHint_strips_colon_prefix() throws {
        let json = """
        {"id": 1, "created_at": "x", "type": "x", "title": "NEW：Some Listing", "body": "x", "url": "x", "listing_id": "x", "read": 0}
        """
        let item = try JSONDecoder().decode(NotificationItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.listingTitleHint, "Some Listing")
    }

    func test_listingTitleHint_no_separator_returns_full() throws {
        let json = """
        {"id": 1, "created_at": "x", "type": "x", "title": "Just a listing", "body": "x", "url": "x", "listing_id": "x", "read": 0}
        """
        let item = try JSONDecoder().decode(NotificationItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.listingTitleHint, "Just a listing")
    }

    // MARK: - NotificationKind classification

    func test_kind_book() throws {
        let item = makeItem(type: "new_listing", title: "New listing", body: "Available")
        XCTAssertEqual(NotificationKind.classify(item), .book)
    }

    func test_kind_lottery() throws {
        let item = makeItem(type: "new_listing", title: "New listing", body: "lottery available")
        XCTAssertEqual(NotificationKind.classify(item), .lottery)
    }

    func test_kind_status_change() throws {
        let item = makeItem(type: "status_change", title: "Status change", body: "lottery → available")
        XCTAssertEqual(NotificationKind.classify(item), .status)
    }

    func test_kind_test() throws {
        let item = makeItem(type: "test", title: "SSE test", body: "test push")
        XCTAssertEqual(NotificationKind.classify(item), .test)
    }

    func test_kind_alert() throws {
        let item = makeItem(type: "error", title: "Error", body: "block detected")
        XCTAssertEqual(NotificationKind.classify(item), .alert)
    }

    // MARK: - Helper

    private func makeItem(type: String, title: String, body: String) -> NotificationItem {
        NotificationItem(
            id: 1, createdAt: "2026-01-01T00:00:00Z",
            type: type, title: title, body: body,
            url: "", listingID: "", read: 0
        )
    }
}
