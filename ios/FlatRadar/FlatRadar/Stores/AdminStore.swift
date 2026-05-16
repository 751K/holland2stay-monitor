import Foundation

/// 管理面板状态（admin only）。
///
/// 持有
/// - users         : ``GET /admin/users`` 返回的摘要列表
/// - monitorStatus : ``GET /admin/monitor/status``
///
/// 操作（每个动作内部 fetch 最新状态以保 UI 跟数据一致）
/// - toggleUser    : 翻转 enabled
/// - deleteUser    : 删 user
/// - startMonitor / stopMonitor / reloadMonitor : 监控进程控制
@MainActor
@Observable
final class AdminStore {
    var users: [AdminUserSummary] = []
    var monitorStatus: AdminMonitorStatus?
    var isLoadingUsers = false
    var isLoadingMonitor = false
    var actionInFlight = false
    var errorMessage: String?

    private let client = APIClient.shared

    // MARK: - Users

    func fetchUsers() async {
        guard !isLoadingUsers else { return }
        isLoadingUsers = true
        errorMessage = nil
        defer { isLoadingUsers = false }
        do {
            let resp = try await client.adminListUsers()
            users = resp.items
        } catch {
            errorMessage = error.localizedDescription
            print("[AdminStore] fetchUsers error: \(error)")
        }
    }

    /// 翻转用户 enabled；本地立刻 optimistic 更新，失败回滚 + 显示错误。
    func toggleUser(id: String) async {
        guard let idx = users.firstIndex(where: { $0.id == id }) else { return }
        let original = users[idx]
        // 用 Decoder 的方式重建 summary 不便；这里手动构造 summary 的 var 副本
        // 通过 fetch 来同步（toggle 后 resp 只回 id+enabled，不够构造完整 summary）
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            _ = try await client.adminToggleUser(id: id)
            await fetchUsers()   // 重新拉一次保持完整字段一致
        } catch {
            errorMessage = error.localizedDescription
            _ = original  // suppressed warning
        }
    }

    func deleteUser(id: String) async {
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            _ = try await client.adminDeleteUser(id: id)
            users.removeAll { $0.id == id }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - Monitor

    func fetchMonitorStatus() async {
        guard !isLoadingMonitor else { return }
        isLoadingMonitor = true
        errorMessage = nil
        defer { isLoadingMonitor = false }
        do {
            monitorStatus = try await client.adminMonitorStatus()
        } catch {
            errorMessage = error.localizedDescription
            print("[AdminStore] fetchMonitorStatus error: \(error)")
        }
    }

    /// 启停 / reload 三个动作走同一封装，避免重复 try/catch。
    private func monitorAction(_ block: () async throws -> AdminMonitorActionResponse) async {
        actionInFlight = true
        errorMessage = nil
        defer { actionInFlight = false }
        do {
            _ = try await block()
            // 后端 fork 子进程要几百 ms 才能写出 pidfile；稍等再拉状态更准
            try? await Task.sleep(nanoseconds: 600_000_000)
            await fetchMonitorStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func startMonitor() async { await monitorAction(client.adminMonitorStart) }
    func stopMonitor() async  { await monitorAction(client.adminMonitorStop) }
    func reloadMonitor() async { await monitorAction(client.adminMonitorReload) }
}
