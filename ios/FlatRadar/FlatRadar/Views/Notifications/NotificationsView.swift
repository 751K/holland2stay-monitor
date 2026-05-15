import SwiftUI

struct NotificationsView: View {
    @Environment(NotificationsStore.self) private var store
    @Environment(AuthStore.self) private var auth

    var body: some View {
        NavigationStack {
            Group {
                if store.isLoading && store.notifications.isEmpty {
                    ProgressView().padding(.top, 60)
                } else if let err = store.errorMessage, store.notifications.isEmpty {
                    ContentUnavailableView(
                        "Unable to Load",
                        systemImage: "wifi.slash",
                        description: Text(err))
                    .refreshable { await store.refresh() }
                } else if store.notifications.isEmpty {
                    ContentUnavailableView(
                        "No Notifications",
                        systemImage: "bell.slash",
                        description: Text("New listings and status changes will appear here."))
                    .refreshable { await store.refresh() }
                } else {
                    listContent
                }
            }
            .navigationTitle("Notifications")
            .toolbar {
                if store.unreadCount > 0 {
                    ToolbarItem(placement: .automatic) {
                        Button("Mark All Read") {
                            Task { await store.markAllRead() }
                        }
                        .font(.subheadline)
                    }
                }
            }
            .task {
                if store.notifications.isEmpty {
                    await store.fetch()
                }
            }
        }
    }

    private var listContent: some View {
        List {
            ForEach(store.notifications) { notification in
                NotificationRow(notification: notification)
                    .swipeActions(edge: .trailing) {
                        if !notification.isRead {
                            Button("Read") {
                                Task { await store.markRead(ids: [notification.id]) }
                            }
                            .tint(.blue)
                        }
                    }
                    .onAppear {
                        if notification.id == store.notifications.last?.id {
                            Task { await store.loadMore() }
                        }
                    }
            }

            if store.isLoadingMore {
                HStack {
                    Spacer()
                    ProgressView()
                    Spacer()
                }
            }
        }
        .refreshable { await store.refresh() }
    }
}
