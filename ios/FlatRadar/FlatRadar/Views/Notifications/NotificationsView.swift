import SwiftUI

/// V2 · Card-style inbox
///
/// 设计要点
/// --------
/// - 灰底（systemGroupedBackground）+ 圆角白卡（plain list 行）
/// - 大标题 "Alerts" + 未读计数（≥1 时）
/// - 没有 SSE Live 指示器（按用户要求）
/// - TODAY / YESTERDAY / EARLIER 三段 mono caps 分节
/// - 右上角 "Read all" 绿勾药丸按钮（替代旧的 "Mark All Read" 文字按钮）
struct NotificationsView: View {
    @Environment(NotificationsStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @State private var showRefreshError = false
    /// "Mark all read" 触觉反馈 trigger —— 每按一次 +1，驱动 `.sensoryFeedback`。
    /// 单条左滑标已读不触发（那个动作系统 swipe 自带触觉）。
    @State private var markAllReadTick = 0

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
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Alerts")
            // Read all 不再放 toolbar —— iOS 26 toolbar item 在大标题下方
            // 显得位置过高，且容易被系统折叠成纯 icon。改成放在第一个分节
            // header 同一行（设计稿 V1 的位置）。
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
            // 全部标已读触觉确认：批量正向操作用 .success，比 .selection 更明确。
            .sensoryFeedback(.success, trigger: markAllReadTick)
        }
    }

    // MARK: - List

    private var listContent: some View {
        List {
            // 未读计数副标题（如果有）
            if store.unreadCount > 0 {
                Section {
                    unreadStripe
                }
                .listRowSeparator(.hidden)
                .listRowInsets(EdgeInsets(top: 4, leading: 20, bottom: 0, trailing: 20))
                .listRowBackground(Color.clear)
            }

            if !todayItems.isEmpty {
                section(title: "TODAY · \(todayItems.count)",
                        items: todayItems,
                        showReadAll: isFirstSection(.today))
            }
            if !yesterdayItems.isEmpty {
                section(title: "YESTERDAY",
                        items: yesterdayItems,
                        showReadAll: isFirstSection(.yesterday))
            }
            if !earlierItems.isEmpty {
                section(title: "EARLIER",
                        items: earlierItems,
                        showReadAll: isFirstSection(.earlier))
            }

            if store.isLoadingMore {
                HStack {
                    Spacer()
                    ProgressView()
                    Spacer()
                }
                .listRowSeparator(.hidden)
                .listRowBackground(Color.clear)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Color(.systemGroupedBackground))
        .refreshable { await store.refresh() }
    }

    @ViewBuilder
    private func section(title: String,
                         items: [NotificationItem],
                         showReadAll: Bool) -> some View {
        Section {
            ForEach(items) { notification in
                NotificationRow(notification: notification)
                    .listRowSeparator(.hidden)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
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
        } header: {
            HStack(alignment: .firstTextBaseline) {
                Text(title)
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .tracking(0.7)
                    .foregroundStyle(.secondary)
                    .textCase(nil)
                Spacer()
                if showReadAll {
                    // 已读 → 0 时不要直接 if 隐藏控件，否则整列上跳。
                    // 用 opacity/hit-testing 保留布局占位，视觉上"渐隐"。
                    let hasUnread = store.unreadCount > 0
                    Button("Mark all read") {
                        markAllReadTick &+= 1   // 触发 .sensoryFeedback(.success, …)
                        Task { await store.markAllRead() }
                    }
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.green)
                    .textCase(nil)
                    .buttonStyle(.plain)
                    .opacity(hasUnread ? 1 : 0)
                    .allowsHitTesting(hasUnread)
                    .animation(.easeInOut(duration: 0.2), value: hasUnread)
                }
            }
            .padding(.top, 8)
            .padding(.horizontal, 4)
        }
    }

    /// 把 "Mark all read" 链接挂在第一个非空分节的 header 同一行。
    private func isFirstSection(_ bucket: NotificationItem.DayBucket) -> Bool {
        if !todayItems.isEmpty { return bucket == .today }
        if !yesterdayItems.isEmpty { return bucket == .yesterday }
        return bucket == .earlier
    }

    @ViewBuilder
    private var unreadStripe: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(Color.green)
                .frame(width: 6, height: 6)
            (Text("\(store.unreadCount)")
                .font(.system(size: 12, weight: .bold, design: .monospaced))
             + Text(" unread")
                .font(.system(size: 12)))
                .foregroundStyle(.primary)
            Spacer()
        }
        .padding(.vertical, 2)
    }

    // MARK: - Day grouping

    private var todayItems: [NotificationItem] {
        store.notifications.filter { $0.dayBucket == .today }
    }

    private var yesterdayItems: [NotificationItem] {
        store.notifications.filter { $0.dayBucket == .yesterday }
    }

    private var earlierItems: [NotificationItem] {
        store.notifications.filter { $0.dayBucket == .earlier }
    }
}
