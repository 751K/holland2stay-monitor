import Foundation

@MainActor
@Observable
final class NotificationsStore {
    var notifications: [NotificationItem] = []
    var unreadCount = 0
    var total = 0
    var isLoading = false
    var isLoadingMore = false
    var errorMessage: String?

    private let client = APIClient.shared
    private let pageSize = 50

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
            for i in notifications.indices {
                if idSet.contains(notifications[i].id) {
                    notifications[i] = notifications[i].markedRead()
                }
            }
            unreadCount = notifications.filter { !$0.isRead }.count
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
}
