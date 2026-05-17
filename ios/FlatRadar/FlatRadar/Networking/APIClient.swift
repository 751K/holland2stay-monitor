import Foundation

/// Central HTTP client for /api/v1/* endpoints.
///
/// 设计变更（Swift 6 strict concurrency）
/// --------------------------------------
/// 原本是 ``actor APIClient``，但项目 ``SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor``
/// 让所有未标注类型默认 ``@MainActor``，与 ``actor`` 跨 actor 共用 Decodable
/// conformance 在 Swift 6 严格并发下会报 main actor-isolated 错误。
///
/// 改为 ``@MainActor final class``：
/// - 与所有 Store / View / Model 同处主 actor，conformance 共享无冲突
/// - 异步 ``request`` 内部的 ``URLSession.shared.data`` 在 background 任务跑，
///   await 期间主线程不阻塞，与 actor 隔离的性能等价
/// - ``setToken`` / ``configure`` 等同步方法在主线程上调用，零开销
@MainActor
final class APIClient {
    /// Posted on MainActor when an API call fails with 401/403.
    /// AuthStore listens and triggers logout.
    static let authFailedNotification = Notification.Name("APIClient.authFailed")
    static let shared = APIClient()

    private var baseURL: URL
    private var token: String?
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    init() {
        // 启动时立刻读 server_url，避免第一次 restoreSession 用错 URL 撞 connection refused。
        // Settings 里 Save 后会再次调 configure(baseURL:) 覆盖。
        self.baseURL = Self.resolveBaseURL()
        #if DEBUG
        print("[APIClient] init baseURL = \(self.baseURL.absoluteString)")
        #endif
        #if DEBUG
        print("[APIClient] UserDefaults[server_url] = \(UserDefaults.standard.string(forKey: "server_url") ?? "<nil>")")
        #endif
    }

    /// 把 UserDefaults["server_url"] 解析成完整 URL；未设置时回退到默认生产环境。
    /// 与 SettingsView.buildBaseURL 保持同步——localhost/127. 走 http，其它一律 https。
    static let defaultServerHost = "flatradar.app"

    static func resolveBaseURL() -> URL {
        let raw = UserDefaults.standard.string(forKey: "server_url") ?? defaultServerHost
        let clean = raw.trimmingCharacters(in: ["/", " "])
        let scheme = clean.hasPrefix("localhost") || clean.hasPrefix("127.")
            ? "http" : "https"
        return URL(string: "\(scheme)://\(clean)")
            ?? URL(string: "https://\(defaultServerHost)")!
    }

    // MARK: - Configuration

    func configure(baseURL url: URL) { baseURL = url }
    func setToken(_ t: String?) { token = t }

    func currentBaseURL() -> URL { baseURL }

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
        // 30s 给后端 cold-start 留余地；之前 15s 在网络抖动 / 冷启动场景下经常
        // 直接 URLError.timedOut，dashboard 下拉刷新就显示"连接失败"。
        req.timeoutInterval = 30

        if authenticated, let tok = token {
            req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization")
        }

        if let body {
            req.httpBody = try encoder.encode(AnyEncodable(body))
        }

        #if DEBUG
        print("[APIClient] \(method) \(url.absoluteString) auth=\(authenticated && token != nil)")
        #endif

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            if let urlError = error as? URLError, urlError.code == .cancelled {
                #if DEBUG
                print("[APIClient] cancelled: \(method) \(url.absoluteString)")
                #endif
                throw CancellationError()
            }
            #if DEBUG
            print("[APIClient] network error: \(error)")
            #endif
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
            #if DEBUG
            print("[APIClient] decode error for \(method) \(path): \(error)")
            #endif
            #if DEBUG
            print("[APIClient] raw: \(raw.prefix(500))")
            #endif
            #if DEBUG
            print("[APIClient] HTTP status: \(http.statusCode)")
            #endif
            throw APIError.decoding(error)
        }

        if !envelope.ok, let err = envelope.error {
            let apiErr = APIError.fromPayload(code: err.code, message: err.message)
            if apiErr.isAuthError {
                NotificationCenter.default.post(name: Self.authFailedNotification, object: nil)
            }
            throw apiErr
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
        #if DEBUG
        print("[APIClient] login body: username=\(username) device=\(deviceName)")
        #endif
        let resp: LoginResponse = try await request("POST", "api/v1/auth/login", body: body, authenticated: false)
        #if DEBUG
        print("[APIClient] login ok: role=\(resp.role) token=\(resp.token.prefix(8))...")
        #endif
        return resp
    }

    func register(username: String, password: String,
                  deviceName: String, ttlDays: Int = 90) async throws -> LoginResponse {
        let body = LoginRequest(username: username, password: password,
                                deviceName: deviceName, ttlDays: ttlDays)
        #if DEBUG
        print("[APIClient] register: username=\(username) device=\(deviceName)")
        #endif
        let resp: LoginResponse = try await request("POST", "api/v1/auth/register", body: body, authenticated: false)
        #if DEBUG
        print("[APIClient] register ok: role=\(resp.role) token=\(resp.token.prefix(8))...")
        #endif
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
                     offset: Int = 0,
                     cities: [String]? = nil,
                     types: [String]? = nil,
                     contract: String? = nil,
                     energy: String? = nil) async throws -> ListingsResponse {
        var parts = ["api/v1/listings?limit=\(limit)&offset=\(offset)"]
        if let city { parts.append("city=\(urlEncode(city))") }
        if let status { parts.append("status=\(urlEncode(status))") }
        if let query, !query.isEmpty { parts.append("q=\(urlEncode(query))") }
        if let cities, !cities.isEmpty {
            parts.append("cities=\(cities.map(urlEncode).joined(separator: ","))")
        }
        if let types, !types.isEmpty {
            parts.append("types=\(types.map(urlEncode).joined(separator: ","))")
        }
        if let contract, !contract.isEmpty {
            parts.append("contract=\(urlEncode(contract))")
        }
        if let energy, !energy.isEmpty {
            parts.append("energy=\(urlEncode(energy))")
        }
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

    /// SSE 流端点 URL（含 last_id）。token 走 Authorization header（URLSession 支持）。
    /// 调用方：NotificationsStore.connectStream。
    func notificationsStreamURL(lastId: Int) -> URL {
        let comp = URLComponents(
            url: baseURL.appendingPathComponent("api/v1/notifications/stream"),
            resolvingAgainstBaseURL: false)
        var c = comp ?? URLComponents()
        c.queryItems = [URLQueryItem(name: "last_id", value: String(lastId))]
        return c.url ?? baseURL
    }

    /// 当前持有的 Bearer token（SSE Client 需要直接挂 header）。
    func currentToken() -> String? { token }

    // MARK: - Map (Phase 2)

    func getMap() async throws -> MapResponse {
        try await request("GET", "api/v1/map")
    }

    // MARK: - Calendar (Phase 2)

    /// 后端是 bearer_optional：未登录 guest 看全量，登录的 user 走 listing_filter。
    /// authenticated 用默认 true：有 token 就带上，没 token 也能拿数据。
    func getCalendar() async throws -> CalendarResponse {
        try await request("GET", "api/v1/calendar")
    }

    // MARK: - Me (Phase 2)

    func getMeSummary() async throws -> MeSummary {
        try await request("GET", "api/v1/me/summary")
    }

    func getMeFilter() async throws -> MeFilterResponse {
        try await request("GET", "api/v1/me/filter")
    }

    /// PUT 完整 ListingFilter（覆盖式更新）。仅 user 角色可调；admin 调会 403。
    /// 后端 ``_coerce_filter_payload`` 做白名单过滤 + 边界校验，多/少字段都安全。
    func updateMeFilter(_ filter: ListingFilter) async throws -> MeFilterResponse {
        try await request("PUT", "api/v1/me/filter", body: filter)
    }

    /// 各筛选维度的候选值（cities/types/contract/tenant/...）。
    /// FilterEditView 进入时调一次，结果可复用。
    func getFilterOptions() async throws -> FilterOptions {
        try await request("GET", "api/v1/filter/options")
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

    /// 发送测试推送给当前会话所有活跃设备。
    /// 用于验证 APNs 链路通畅，绕过 push.dispatch 的 user_id/throttle 限制。
    func testPush(title: String? = nil,
                  body: String? = nil) async throws -> DeviceTestPushResponse {
        struct TestPushBody: Encodable {
            let title: String?
            let body: String?
        }
        return try await request(
            "POST", "api/v1/devices/test",
            body: TestPushBody(title: title, body: body))
    }

    // MARK: - Me (account management)

    /// DELETE /me — 注销当前用户账号，删除 users.json 中的数据并撤销所有 token。
    func deleteAccount() async throws -> AccountDeleteResponse {
        try await request("DELETE", "api/v1/me")
    }

    // MARK: - Feedback

    struct FeedbackBody: Encodable {
        let kind: String      // "bug" | "suggestion" | "other"
        let message: String
        let user_name: String
        let app_version: String
    }

    struct FeedbackResponse: Decodable {
        let submitted: Bool
    }

    func submitFeedback(kind: String, message: String,
                        userName: String = "",
                        appVersion: String = "") async throws -> FeedbackResponse {
        try await request("POST", "api/v1/feedback",
                          body: FeedbackBody(kind: kind, message: message,
                                            user_name: userName, app_version: appVersion))
    }

    // MARK: - Me export (GDPR)

    /// GET /me/export — 返回用户的完整个人数据 JSON（GDPR 数据导出）。
    func meExport() async throws -> Data {
        let url = buildURL("api/v1/me/export")
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 30
        if let tok = token {
            req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization")
        }

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.badResponse(0)
        }

        guard (200...299).contains(http.statusCode) else {
            if let shell = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let err = shell["error"] as? [String: Any],
               let code = err["code"] as? String,
               let message = err["message"] as? String {
                throw APIError.fromPayload(code: code, message: message)
            }
            throw APIError.badResponse(http.statusCode)
        }

        // 拆 API 壳 {ok, data} → 只保留 data 字段
        guard let shell = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ok = shell["ok"] as? Bool, ok,
              let inner = shell["data"] else {
            throw APIError.serverError("invalid export response")
        }
        return try JSONSerialization.data(withJSONObject: inner,
                                          options: [.prettyPrinted, .sortedKeys])
    }

    // MARK: - Admin (Phase 5 Part 2) — admin role only

    func adminListUsers() async throws -> AdminUsersResponse {
        try await request("GET", "api/v1/admin/users")
    }

    func adminToggleUser(id: String) async throws -> AdminUserToggleResponse {
        try await request("POST", "api/v1/admin/users/\(id)/toggle")
    }

    func adminDeleteUser(id: String) async throws -> AdminUserDeleteResponse {
        try await request("DELETE", "api/v1/admin/users/\(id)")
    }

    func adminMonitorStatus() async throws -> AdminMonitorStatus {
        try await request("GET", "api/v1/admin/monitor/status")
    }

    func adminMonitorStart() async throws -> AdminMonitorActionResponse {
        try await request("POST", "api/v1/admin/monitor/start")
    }

    func adminMonitorStop() async throws -> AdminMonitorActionResponse {
        try await request("POST", "api/v1/admin/monitor/stop")
    }

    func adminMonitorReload() async throws -> AdminMonitorActionResponse {
        try await request("POST", "api/v1/admin/monitor/reload")
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
