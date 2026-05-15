import Foundation

/// Central HTTP client for /api/v1/* endpoints.
/// Actor ensures baseURL and token mutations are data-race safe.
actor APIClient {
    static let shared = APIClient()

    private var baseURL = URL(string: "http://127.0.0.1:8088")!
    private var token: String?
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    // MARK: - Configuration

    func configure(baseURL url: URL) { baseURL = url }
    func setToken(_ t: String?) { token = t }

    func currentBaseURL() -> URL { baseURL }
    func hasToken() -> Bool { token != nil }

    // MARK: - Core request helper

    private func buildURL(_ path: String) -> URL {
        // Split query string from path — appendingPathComponent percent-encodes ? and &
        guard let qIndex = path.firstIndex(of: "?") else {
            return baseURL.appendingPathComponent(path)
        }
        let pathOnly = String(path[..<qIndex])
        let query = String(path[path.index(after: qIndex)...])
        guard var comps = URLComponents(
            url: baseURL.appendingPathComponent(pathOnly),
            resolvingAgainstBaseURL: false)
        else {
            return baseURL.appendingPathComponent(path)
        }
        comps.percentEncodedQuery = query
        return comps.url ?? baseURL.appendingPathComponent(path)
    }

    private func request<T: Decodable>(
        _ method: String,
        _ path: String,
        body: (any Encodable)? = nil,
        authenticated: Bool = true
    ) async throws -> T {
        let url = buildURL(path)
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 15

        if authenticated, let tok = token {
            req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization")
        }

        if let body {
            req.httpBody = try encoder.encode(AnyEncodable(body))
        }

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.network(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.badResponse(0)
        }

        // Decode envelope
        let envelope: APIResponse<T>
        do {
            envelope = try decoder.decode(APIResponse<T>.self, from: data)
        } catch {
            let raw = String(data: data, encoding: .utf8) ?? "<not utf8>"
            print("[APIClient] decode error for \(method) \(path): \(error)")
            print("[APIClient] raw: \(raw.prefix(500))")
            print("[APIClient] HTTP status: \(http.statusCode)")
            throw APIError.decoding(error)
        }

        if !envelope.ok, let err = envelope.error {
            throw APIError.fromPayload(code: err.code, message: err.message)
        }

        guard let payload = envelope.data else {
            throw APIError.decoding(
                NSError(domain: "API", code: http.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "Response missing data field"]))
        }

        return payload
    }

    // MARK: - Auth (Phase 1)

    func login(username: String, password: String,
               deviceName: String, ttlDays: Int = 90) async throws -> LoginResponse {
        let body = LoginRequest(username: username, password: password,
                                deviceName: deviceName, ttlDays: ttlDays)
        print("[APIClient] login body: username=\(username) device=\(deviceName)")
        let resp: LoginResponse = try await request("POST", "api/v1/auth/login", body: body, authenticated: false)
        print("[APIClient] login ok: role=\(resp.role) token=\(resp.token.prefix(8))...")
        return resp
    }

    func logout() async throws -> RevokePayload {
        try await request("POST", "api/v1/auth/logout")
    }

    func getMe() async throws -> MeResponse {
        try await request("GET", "api/v1/auth/me")
    }

    // MARK: - Public Stats (Phase 1, no auth)

    func getPublicSummary() async throws -> MonitorStatus {
        try await request("GET", "api/v1/stats/public/summary", authenticated: false)
    }

    func getPublicCharts() async throws -> [String] {
        let resp: ChartKeysList = try await request(
            "GET", "api/v1/stats/public/charts", authenticated: false)
        return resp.charts
    }

    func getPublicChart(key: String, days: Int = 30) async throws -> ChartData {
        try await request(
            "GET", "api/v1/stats/public/charts/\(key)?days=\(days)", authenticated: false)
    }

    // MARK: - Listings (Phase 2)

    func getListings(city: String? = nil, status: String? = nil,
                     query: String? = nil, limit: Int = 50,
                     offset: Int = 0) async throws -> ListingsResponse {
        var parts = ["api/v1/listings?limit=\(limit)&offset=\(offset)"]
        if let city { parts.append("city=\(urlEncode(city))") }
        if let status { parts.append("status=\(urlEncode(status))") }
        if let query, !query.isEmpty { parts.append("q=\(urlEncode(query))") }
        return try await request("GET", parts.joined(separator: "&"))
    }

    func getListing(id: String) async throws -> Listing {
        try await request("GET", "api/v1/listings/\(id)")
    }

    // MARK: - Notifications (Phase 2)

    func getNotifications(limit: Int = 50, offset: Int = 0) async throws -> NotificationsResponse {
        try await request("GET", "api/v1/notifications?limit=\(limit)&offset=\(offset)")
    }

    func markNotificationsRead(ids: [Int]? = nil) async throws -> MarkReadResponse {
        struct MarkReadBody: Encodable {
            let ids: [Int]?
        }
        return try await request("POST", "api/v1/notifications/read", body: MarkReadBody(ids: ids))
    }

    // MARK: - Me (Phase 2)

    func getMeSummary() async throws -> MeSummary {
        try await request("GET", "api/v1/me/summary")
    }

    func getMeFilter() async throws -> MeFilterResponse {
        try await request("GET", "api/v1/me/filter")
    }

    // MARK: - Devices / APNs (Phase 3)

    /// 注册或刷新一台设备的 APNs token。
    /// - Parameters:
    ///   - token: APNs hex token（`didRegisterForRemoteNotifications` 拿到）
    ///   - env: "sandbox"（Debug 构建 / Xcode 运行）或 "production"（TestFlight / App Store）
    ///   - model: 显示用，例如 "iPhone15,2"
    ///   - bundleId: 防 Bundle ID 配错；上报实际运行的 bundle id
    func registerDevice(token: String, env: String,
                        model: String, bundleId: String) async throws -> DeviceRegisterResponse {
        let body = DeviceRegisterRequest(
            deviceToken: token, env: env,
            platform: "ios", model: model, bundleId: bundleId)
        return try await request("POST", "api/v1/devices/register", body: body)
    }

    func listDevices() async throws -> DeviceListResponse {
        try await request("GET", "api/v1/devices")
    }

    func deleteDevice(id: Int) async throws -> DeviceDeleteResponse {
        try await request("DELETE", "api/v1/devices/\(id)")
    }

    // MARK: - Helpers

    private func urlEncode(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? s
    }
}

// MARK: - Helper types

/// Wrapper so we can encode arbitrary Encodable values via JSONEncoder
private struct AnyEncodable: Encodable {
    let value: any Encodable
    init(_ value: any Encodable) { self.value = value }
    func encode(to encoder: any Encoder) throws {
        try value.encode(to: encoder)
    }
}

struct RevokePayload: Decodable {
    let revoked: Bool
}

/// For decoding /stats/public/charts response: {"charts": [...], "ok": true, "data": {...}}
private struct ChartKeysList: Decodable {
    let charts: [String]
}
