import XCTest
@testable import FlatRadar

final class AuthModelsTests: XCTestCase {

    // MARK: - LoginRequest encoding

    func test_loginRequest_encodes_correctly() throws {
        let req = LoginRequest(username: "alice", password: "secret", deviceName: "iPhone", ttlDays: 90)
        let data = try JSONEncoder().encode(req)
        let dict = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        XCTAssertEqual(dict?["username"] as? String, "alice")
        XCTAssertEqual(dict?["password"] as? String, "secret")
        XCTAssertEqual(dict?["device_name"] as? String, "iPhone")
        XCTAssertEqual(dict?["ttl_days"] as? Int, 90)
    }

    // MARK: - LoginResponse decoding

    func test_loginResponse_decodes() throws {
        let json = """
        {
            "token": "tok_abc123", "token_id": 42, "role": "user",
            "user_id": "user_x", "device_name": "iPhone15,2", "ttl_days": 90
        }
        """
        let resp = try JSONDecoder().decode(LoginResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.token, "tok_abc123")
        XCTAssertEqual(resp.tokenID, 42)
        XCTAssertEqual(resp.role, "user")
        XCTAssertEqual(resp.userID, "user_x")
        XCTAssertEqual(resp.ttlDays, 90)
    }

    // MARK: - MeResponse decoding

    func test_meResponse_with_user() throws {
        let json = """
        {"role": "user", "user_id": "u1", "user": {"id": "u1", "name": "Alice", "enabled": true}}
        """
        let resp = try JSONDecoder().decode(MeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.role, "user")
        XCTAssertEqual(resp.userID, "u1")
        XCTAssertNotNil(resp.user)
        XCTAssertEqual(resp.user?.name, "Alice")
    }

    func test_meResponse_without_user() throws {
        let json = """
        {"role": "admin", "user_id": null, "user": null}
        """
        let resp = try JSONDecoder().decode(MeResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.role, "admin")
        XCTAssertNil(resp.userID)
        XCTAssertNil(resp.user)
    }

    // MARK: - LegalResponse decoding

    func test_legalResponse_decodes() throws {
        let json = """
        {"terms": "These are terms", "privacy": "This is privacy", "updated_at": "2026-05-25"}
        """
        let resp = try JSONDecoder().decode(LegalResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.terms, "These are terms")
        XCTAssertEqual(resp.privacy, "This is privacy")
        XCTAssertEqual(resp.updatedAt, "2026-05-25")
    }
}
