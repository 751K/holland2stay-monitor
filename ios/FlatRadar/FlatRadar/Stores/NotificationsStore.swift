import Foundation
import UserNotifications

@MainActor
@Observable
final class NotificationsStore {
    var notifications: [NotificationItem] = []
    /// 未读计数；每次写入自动同步到 App 图标 badge。
    var unreadCount = 0 {
        didSet { syncAppBadge() }
    }
    var total = 0
    var isLoading = false
    var isLoadingMore = false
    var errorMessage: String?
    var lastError: APIError?

    // SSE 实时流状态
    var isStreamConnected = false
    var streamError: String?

    private let client = APIClient.shared
    private let pageSize = 50
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        return d
    }()

    // SSE 后台任务句柄；登出 / 切后台时取消
    private var streamTask: Task<Void, Never>?

    var hasMore: Bool { notifications.count < total }

    func fetch() async {
        isLoading = true
        errorMessage = nil
        do {
            let resp = try await client.getNotifications(limit: pageSize, offset: 0)
            notifications = resp.items
            total = resp.total
            unreadCount = resp.unread
        } catch {
            lastError = error as? APIError
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    func loadMore() async {
        guard hasMore, !isLoadingMore else { return }
        isLoadingMore = true
        do {
            let resp = try await client.getNotifications(
                limit: pageSize, offset: notifications.count)
            notifications.append(contentsOf: resp.items)
            total = resp.total
        } catch {
            // Silently fail; pull-to-refresh recovers
        }
        isLoadingMore = false
    }

    func refresh() async {
        await fetch()
    }

    func markRead(ids: [Int]) async {
        guard !ids.isEmpty else { return }
        do {
            _ = try await client.markNotificationsRead(ids: ids)
            let idSet = Set(ids)
            // 翻状态时**当场算这次新增了多少 "from unread to read"**，
            // 直接拿来减 unreadCount。避免之前每次 O(n) 重扫整张列表，也对
            // SSE 并发更稳：SSE handleSSEData 是用 += 增量更新 unreadCount 的，
            // 这里用 -= 增量减，跟它对称，不会用 filter().count 覆盖 SSE 期间
            // 的增量。
            var transitioned = 0
            for i in notifications.indices where idSet.contains(notifications[i].id) {
                if !notifications[i].isRead {
                    transitioned += 1
                }
                notifications[i] = notifications[i].markedRead()
            }
            unreadCount = max(0, unreadCount - transitioned)
        } catch {
            // Non-critical; user can retry
        }
    }

    func markAllRead() async {
        do {
            _ = try await client.markNotificationsRead(ids: nil)
            // Optimistic local update
            for i in notifications.indices {
                notifications[i] = notifications[i].markedRead()
            }
            unreadCount = 0
        } catch {
            // Non-critical
        }
    }

    // MARK: - SSE 实时流

    /// 启动 SSE 连接（幂等：已连/任务在跑时直接返回）。
    /// guest 角色不该调（没 token，stream 会 401）。
    func connectStream() {
        guard streamTask == nil else { return }
        guard client.currentToken() != nil else {
            #if DEBUG
            print("[SSE] no token, skip connect")
            #endif
            return
        }
        streamTask = Task { [weak self] in
            await self?.streamLoop()
        }
    }

    /// 登出时清空：先断 SSE 再清数据，避免断开期间还有 handleSSEData 写入旧值。
    /// 注意 unreadCount = 0 会触发 didSet → syncAppBadge → 同步 App 角标到 0。
    func clear() {
        disconnectStream()
        notifications = []
        total = 0
        unreadCount = 0
        isLoading = false
        isLoadingMore = false
        errorMessage = nil
        lastError = nil
        streamError = nil
    }

    /// 主动停掉 SSE（登出 / 切后台）。
    func disconnectStream() {
        streamTask?.cancel()
        streamTask = nil
        isStreamConnected = false
    }

    /// 重连退避循环。后端单连接 300s 主动关闭让浏览器自然重连——
    /// iOS 这边一样：throw 后等几秒再连。指数退避，上限 60s。
    private func streamLoop() async {
        var backoff: UInt64 = 2_000_000_000   // 2s
        while !Task.isCancelled {
            do {
                try await runStreamOnce()
                // 正常返回（服务端 maxage 到了）→ 短暂等待再连
                backoff = 2_000_000_000
                isStreamConnected = false
                streamError = nil
                try? await Task.sleep(nanoseconds: 500_000_000)
            } catch is CancellationError {
                break
            } catch {
                isStreamConnected = false
                streamError = error.localizedDescription
                #if DEBUG
                print("[SSE] stream error: \(error); reconnect in \(backoff / 1_000_000_000)s")
                #endif
                try? await Task.sleep(nanoseconds: backoff)
                backoff = min(backoff * 2, 60_000_000_000)
            }
        }
    }

    private func runStreamOnce() async throws {
        // 进函数立刻 snapshot lastId；之前 maxId 是 computed property，
        // 在 runStreamOnce 内部多处读会随 notifications 变化（handleSSEData
        // 边塞数据 边可能撞）。snapshot 一次保证本次连接生命周期内 lastId
        // 始终是建立连接时刻的值。
        let lastId = notifications.first?.id ?? 0
        let url = client.notificationsStreamURL(lastId: lastId)
        let token = client.currentToken()
        let sse = SSEClient(url: url, bearerToken: token)
        #if DEBUG
        print("[SSE] connecting \(url.absoluteString)")
        #endif
        isStreamConnected = true
        streamError = nil

        for try await event in sse.events() {
            try Task.checkCancellation()
            switch event {
            case .data(let payload):
                handleSSEData(payload)
            case .keepalive:
                continue   // 保活心跳，无操作
            case .retry:
                continue   // 服务端建议重连间隔，我们的退避已自行处理
            }
        }
    }

    /// 后端推过来的 ``data:`` payload 是 ``list[NotificationItem]`` JSON。
    /// 多条按 id 升序，我们要插到本地列表顶部（新的在前）。
    // MARK: - App icon badge

    /// 把 ``unreadCount`` 同步到 App 图标右上角的红点数字。
    /// 要求用户已授予 ``.badge`` 权限（PushStore.requestPermissionAndRegister 已经申请过）。
    /// 失败安静吞（权限被撤是常见情况，UI 上 tab badge 仍正常显示）。
    private func syncAppBadge() {
        let n = unreadCount
        // 显式 @MainActor —— setBadgeCount 是 MainActor-isolated API。
        // 不写 @MainActor 在 Swift 6 strict concurrency 下报错；
        // 写了在 Swift 5 模式下也不会有负面影响。
        Task { @MainActor in
            do {
                try await UNUserNotificationCenter.current().setBadgeCount(n)
            } catch {
                // 用户拒了 badge 权限 / iOS < 16 → 静默
            }
        }
    }

    private func handleSSEData(_ payload: String) {
        guard let bytes = payload.data(using: .utf8) else { return }
        do {
            let incoming = try decoder.decode([NotificationItem].self, from: bytes)
            if incoming.isEmpty { return }
            let existing = Set(notifications.map(\.id))
            let fresh = incoming.filter { !existing.contains($0.id) }
            if fresh.isEmpty { return }
            // 后端按 id ASC 排；本地列表按时间 DESC，所以反转
            notifications.insert(contentsOf: fresh.reversed(), at: 0)
            total += fresh.count
            unreadCount += fresh.filter { !$0.isRead }.count
            #if DEBUG
            print("[SSE] +\(fresh.count) new notifications (total=\(total))")
            #endif
        } catch {
            // Schema 漂移 / 后端发出畸形 JSON → 写到 streamError 让 UI 能展示，
            // 不再仅 debug print 静默吞。生产 release 模式下也会进 Logger。
            streamError = "Notification stream parse error"
            #if DEBUG
            print("[SSE] decode error: \(error); raw=\(payload.prefix(200))")
            #endif
        }
    }
}
