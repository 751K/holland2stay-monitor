import SwiftUI

struct NotificationsView: View {
    @Environment(NotificationsStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @State private var showRefreshError = false

    var body: some View {
        NavigationStack {
            Group {
                if store.isLoading && store.notifications.isEmpty {
                    ProgressView().padding(.top, 60)
                } else if let err = store.errorMessage, store.notifications.isEmpty {
                    let apiErr = store.lastError
                    ContentUnavailableView {
                        Label(
                            apiErr?.errorDescription ?? "Unable to Load",
                            systemImage: apiErr?.systemImage ?? "wifi.slash")
                    } description: {
                        Text(err)
                    } actions: {
                        Button("Try Again") {
                            Task { await store.refresh() }
                        }
                    }
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
                // 左上：实时连接小指示器
                ToolbarItem(placement: .topBarLeading) {
                    streamStatusIndicator
                }
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
            .onChange(of: store.errorMessage) { _, new in
                showRefreshError = new != nil && !store.notifications.isEmpty
            }
            .alert(
                store.lastError?.errorDescription ?? "Refresh Failed",
                isPresented: $showRefreshError
            ) {
                Button("OK") {}
            } message: {
                Text(store.errorMessage ?? "")
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

    /// 标题栏左侧的实时连接小指示器：
    /// - 绿点 = SSE 连着，新通知会自动到达
    /// - 灰点 = 未连（后台 / 登出 / 错误）
    @ViewBuilder
    private var streamStatusIndicator: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(store.isStreamConnected ? Color.green : Color.gray)
                .frame(width: 6, height: 6)
            Text(store.isStreamConnected ? "Live" : "Idle")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}
