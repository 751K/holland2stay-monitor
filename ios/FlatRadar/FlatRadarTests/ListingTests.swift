import XCTest
@testable import FlatRadar

final class ListingTests: XCTestCase {

    // MARK: - Decoding

    func test_decode_basic_fields() throws {
        let json = """
        {
            "id": "abc123", "name": "Test Listing", "status": "Available to book",
            "city": "Eindhoven", "source": "holland2stay", "url": "https://example.com",
            "price_raw": "€707/mo", "price_value": 707.0,
            "features": [], "feature_map": {}
        }
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.id, "abc123")
        XCTAssertEqual(listing.name, "Test Listing")
        XCTAssertEqual(listing.status, "Available to book")
        XCTAssertEqual(listing.city, "Eindhoven")
        XCTAssertEqual(listing.priceRaw, "€707/mo")
        XCTAssertEqual(listing.priceValue, 707.0)
    }

    func test_decode_with_featureMap() throws {
        let json = """
        {
            "id": "x", "name": "x", "status": "x", "city": "x",
            "features": [], "feature_map": {"area": "26 m²", "energy_label": "A++", "floor": "5"}
        }
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.featureMap["area"], "26 m²")
        XCTAssertEqual(listing.featureMap["energy_label"], "A++")
        XCTAssertEqual(listing.featureMap["floor"], "5")
    }

    func test_decode_defaults_for_optional_fields() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.source, nil)
        XCTAssertEqual(listing.priceRaw, nil)
        XCTAssertEqual(listing.features, [])
        XCTAssertEqual(listing.featureMap, [:])
        XCTAssertEqual(listing.url, "")
        XCTAssertEqual(listing.city, "")
    }

    // MARK: - displayPrice

    func test_displayPrice_uses_priceRaw() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S", "price_raw": "€1200/mo"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.displayPrice, "€1200/mo")
    }

    func test_displayPrice_uses_priceValue_fallback() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S", "price_value": 850.0}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertTrue(listing.displayPrice.contains("850"))
    }

    // MARK: - displayArea

    func test_displayArea_from_featureMap() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S", "feature_map": {"area": "45 m²"}}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.displayArea, "45 m²")
    }

    func test_displayArea_missing_returns_dash() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S", "feature_map": {}}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.displayArea, "—")
    }

    // MARK: - displayAvailableFrom

    func test_displayAvailableFrom_shortens_date() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S", "available_from": "2026-06-15 00:00:00"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.displayAvailableFrom, "06-15")
    }

    func test_displayAvailableFrom_missing_returns_dash() throws {
        let json = """
        {"id": "1", "name": "N", "status": "S"}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.displayAvailableFrom, "—")
    }

    // MARK: - statusKind

    func test_statusKind_book() throws {
        let json = """
        {"id": "1", "name": "N", "status": "Available to book", "features": [], "feature_map": {}}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.statusKind, .book)
    }

    func test_statusKind_lottery() throws {
        let json = """
        {"id": "1", "name": "N", "status": "Available in lottery", "features": [], "feature_map": {}}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.statusKind, .lottery)
    }

    func test_statusKind_reserved() throws {
        let json = """
        {"id": "1", "name": "N", "status": "Rented", "features": [], "feature_map": {}}
        """
        let listing = try JSONDecoder().decode(Listing.self, from: Data(json.utf8))
        XCTAssertEqual(listing.statusKind, .reserved)
    }

    // MARK: - Hashable / Equatable

    func test_equality_by_id() {
        let a = Listing(id: "x", name: "A", status: "S", features: [], featureMap: [:], url: "", city: "")
        let b = Listing(id: "x", name: "B", status: "T", features: [], featureMap: [:], url: "", city: "")
        XCTAssertEqual(a, b)
        XCTAssertEqual(a.hashValue, b.hashValue)
    }

    func test_inequality_by_id() {
        let a = Listing(id: "a", name: "A", status: "S", features: [], featureMap: [:], url: "", city: "")
        let b = Listing(id: "b", name: "A", status: "S", features: [], featureMap: [:], url: "", city: "")
        XCTAssertNotEqual(a, b)
    }
}
