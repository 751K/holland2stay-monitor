import Foundation

/// 当前 user 的 ``ListingFilter`` 缓存 + 编辑保存能力。
///
/// 数据来源
/// --------
/// - 登录成功后 ``AuthStore.applyMe`` 已经把 ``user.listing_filter`` 写进
///   ``UserInfo``，进入 Settings/Edit 时直接读那个就行
/// - 但编辑提交后服务端会校验/规范化字段（比如 ALLOWED 字段大小写、energy
///   白名单），返回标准形态——这里就要把 ``AuthStore.userInfo.listing_filter``
///   也同步更新，否则 Dashboard 的 matched 计数会跟实际不一致
///
/// 因此 save 路径会：
/// 1. ``PUT /me/filter`` 提交客户端构造的新 filter
/// 2. 拿后端 round-trip 返回的标准化版本
/// 3. 让 caller 决定是否调 ``AuthStore`` 把 ``UserInfo`` 替换
@MainActor
@Observable
final class MeFilterStore {
    var isSaving = false
    var errorMessage: String?
    var lastResponse: MeFilterResponse?

    private let client = APIClient.shared

    func save(_ filter: ListingFilter) async -> MeFilterResponse? {
        guard !isSaving else { return nil }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let resp = try await client.updateMeFilter(filter)
            lastResponse = resp
            return resp
        } catch {
            errorMessage = error.localizedDescription
            #if DEBUG
            print("[MeFilterStore] save error: \(error)")
            #endif
            return nil
        }
    }
}
