import XCTest
@testable import FlatRadar

final class APIResponseTests: XCTestCase {

    // MARK: - Success envelope

    func test_ok_with_data() throws {
        let json = """
        {"ok": true, "data": {"id": "abc", "name": "Test"}, "error": null}
        """
        let resp = try JSONDecoder().decode(APIResponse<Listing>.self, from: Data(json.utf8))
        XCTAssertTrue(resp.ok)
        XCTAssertNotNil(resp.data)
        XCTAssertEqual(resp.data?.id, "abc")
        XCTAssertNil(resp.error)
    }

    func test_error_envelope() throws {
        let json = """
        {"ok": false, "data": null, "error": {"code": "unauthorized", "message": "Invalid token"}}
        """
        let resp = try JSONDecoder().decode(APIResponse<Listing>.self, from: Data(json.utf8))
        XCTAssertFalse(resp.ok)
        XCTAssertNil(resp.data)
        XCTAssertNotNil(resp.error)
        XCTAssertEqual(resp.error?.code, "unauthorized")
        XCTAssertEqual(resp.error?.message, "Invalid token")
    }

    func test_ok_without_data() throws {
        let json = """
        {"ok": true, "data": null, "error": null}
        """
        let resp = try JSONDecoder().decode(APIResponse<Listing>.self, from: Data(json.utf8))
        XCTAssertTrue(resp.ok)
        XCTAssertNil(resp.data)
    }
}
