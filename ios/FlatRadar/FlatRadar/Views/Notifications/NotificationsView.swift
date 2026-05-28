import SwiftUI

/// V3 · 与 Dashboard / Browse 视觉语言对齐的 Alerts 屏
///
/// 来自 Claude Design "FlatRadar Alerts.html" V1。核心变化（vs 老 V2）：
/// - 删掉每行独立 card → 整段共享 `.insetGrouped` 白色大圆角容器 + hairline
/// - 加顶部双药丸 toolbar：左 "All types ⌄" type filter + 右 "Mark all read"
/// - 加 Live pill（绿点 + halo + mono 数字），与 Browse / Dashboard 同款
/// - 删除 emoji、删除 32×32 彩色 icon tile（改成 8pt 小色点）
struct NotificationsView: View {
    @Environment(NotificationsStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let scrollTopID = "notifications-scroll-top"

    @State private var showRefreshError = false
    @State private var markAllReadTick = 0
    @State private var notificationsPath: [ListingRoute] = []
    /// nil = "All types"
    @State private var typeFilter: NotificationItem.Kind? = nil
    private var typeFilterBinding: Binding<NotificationItem.Kind?> {
        Binding(
            get: { typeFilter },
            set: { newValue in
                guard typeFilter != newValue else { return }
                typeFilter = newValue
            }
        )
    }
    /// Live pill 绿点呼吸动画相位
    @State private var liveDotBreathing = false

    // 分桶 + 分类计数缓存——一次 O(n) 扫描产出所有 UI 需要的数据
    @State private var todayItems: [NotificationItem] = []
    @State private var yesterdayItems: [NotificationItem] = []
    @State private var earlierItems: [NotificationItem] = []
    @State private var kindCounts: [NotificationItem.Kind: Int] = [:]

    var body: some View {
        NavigationStack(path: $notificationsPath) {
            Group {
                if store.isLoading && store.notifications.isEmpty {
                    ProgressView().padding(.top, 60)
                } else if let err = store.errorMessage, store.notifications.isEmpty {
                    errorState(err)
                } else if store.notifications.isEmpty {
                    emptyState
                } else {
                    listContent
                }
            }
            .background(Color(.systemGroupedBackground))
            // 不用 iOS 原生 large title——与 Dashboard 一致，自定义 28pt heavy
            // header 放在 content 区内，能跟 Live capsule + 工具条对齐到同一栏。
            // `.inline` 让 nav bar 缩成 44pt 高的小条（仍占位、保留 status bar
            // 适应），原生 title 区留空。自定义 "Alerts" 标题在 list 第一行渲染。
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: ListingRoute.self) { route in
                ListingDetailView(route: route)
            }
            .task {
                if store.notifications.isEmpty { await store.fetch() }
                rebucketDayGroups()
                if !reduceMotion { liveDotBreathing = true }
            }
            .onChange(of: store.errorMessage) { _, new in
                showRefreshError = new != nil && !store.notifications.isEmpty
            }
            .onChange(of: store.revision) { _, _ in
                rebucketDayGroups()
            }
            .onChange(of: typeFilter) { _, _ in
                if reduceMotion {
                    rebucketDayGroups()
                } else {
                    withAnimation(.easeInOut(duration: 0.22)) {
                        rebucketDayGroups()
                    }
                }
            }
            .alert(
                store.lastError?.errorDescription ?? "Refresh Failed",
                isPresented: $showRefreshError
            ) {
                Button("OK") {}
            } message: {
                Text(store.errorMessage ?? "")
            }
            .sensoryFeedback(.success, trigger: markAllReadTick)
        }
    }

    // MARK: - States

    @ViewBuilder
    private func errorState(_ err: String) -> some View {
        let apiErr = store.lastError
        ContentUnavailableView {
            Label(
                apiErr?.errorDescription ?? "Unable to Load",
                systemImage: apiErr?.systemImage ?? "wifi.slash")
        } description: {
            Text(err)
        } actions: {
            Button("Try Again") { Task { await store.refresh() } }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView(
            "No Notifications",
            systemImage: "bell.slash",
            description: Text("New listings and status changes will appear here."))
        .refreshable { await store.refresh() }
    }

    // MARK: - List

    private var listContent: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                    VStack(alignment: .leading, spacing: 10) {
                        headerRow
                        livePill
                    }
                    .id(scrollTopID)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)

                    Section {
                        VStack(alignment: .leading, spacing: 20) {
                            if !todayItems.isEmpty {
                                section(title: "TODAY · \(todayItems.count)", items: todayItems)
                            }
                            if !yesterdayItems.isEmpty {
                                section(title: "YESTERDAY · \(yesterdayItems.count)", items: yesterdayItems)
                            }
                            if !earlierItems.isEmpty {
                                section(title: "EARLIER · \(earlierItems.count)", items: earlierItems)
                            }

                            if store.isLoadingMore {
                                HStack { Spacer(); ProgressView(); Spacer() }
                                    .padding(.vertical, 12)
                            }
                        }
                        .padding(.horizontal, 16)
                        .padding(.top, 10)
                        .padding(.bottom, 28)
                    } header: {
                        stickyToolbar
                    }
                }
            }
            .onChange(of: typeFilter) { _, _ in
                let scroll = {
                    proxy.scrollTo(scrollTopID, anchor: .top)
                }
                if reduceMotion {
                    scroll()
                } else {
                    withAnimation(.easeInOut(duration: 0.28)) {
                        scroll()
                    }
                }
            }
            .refreshable { await store.refresh() }
            .background(Color(.systemGroupedBackground))
        }
    }

    private var stickyToolbar: some View {
        toolbarPills
            .padding(.horizontal, 16)
            .padding(.top, 6)
            .padding(.bottom, 8)
            .frame(maxWidth: .infinity)
            .background {
                Rectangle()
                    .fill(Color.black.opacity(0.001))
                    .contentShape(Rectangle())
                    .onTapGesture {}
            }
    }

    @ViewBuilder
    private func section(title: String, items: [NotificationItem]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .tracking(0.8)
                .foregroundStyle(.blue)
                .textCase(nil)
                .padding(.horizontal, 16)

            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, n in
                    NotificationRow(notification: n)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .contentShape(Rectangle())
                        .onTapGesture { handleTap(n) }
                        .contextMenu {
                            if !n.isRead {
                                Button("Mark as read") {
                                    Task { await store.markRead(ids: [n.id]) }
                                }
                            }
                        }
                        .onAppear {
                            if n.id == visibleLastNotificationID {
                                Task { await store.loadMore() }
                            }
                        }
                    if index < items.count - 1 {
                        Divider()
                            .padding(.leading, 14)
                    }
                }
            }
            .background(Color(.secondarySystemGroupedBackground), in: sectionCardShape)
            .overlay(sectionCardShape.strokeBorder(.separator.opacity(0.45), lineWidth: 0.5))
        }
    }

    private var visibleLastNotificationID: Int? {
        earlierItems.last?.id ?? yesterdayItems.last?.id ?? todayItems.last?.id
    }

    private var sectionCardShape: RoundedRectangle {
        RoundedRectangle(cornerRadius: 22, style: .continuous)
    }

    // MARK: - Header (custom, mirrors Dashboard's 28pt heavy title)

    /// "Alerts" 大标题。与 iOS 原生 large title 字号一致（34pt），但放在
    /// content 区内（navigationBarTitleDisplayMode=.inline，无系统 large title），
    /// 这样底下的 Live / toolbar / 列表可以紧贴标题排版，不被原生 large title
    /// 的固定留白推下去。
    private var headerRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("Alerts")
                .font(.system(size: 34, weight: .heavy))
                .tracking(-1.0)
            if store.unreadCount > 0 {
                Text("· \(store.unreadCount) new")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(.blue)
            }
            Spacer()
        }
    }

    // MARK: - Toolbar pills (top of scrollable area)

    private var toolbarPills: some View {
        // 横向 padding 由父 VStack 的 padding(.horizontal, 20) 统一给——
        // 这样两药丸的**外左缘**正好跟下方 inset-grouped 卡片的外左缘对齐。
        Group {
            if #available(iOS 26.0, *) {
                GlassEffectContainer(spacing: 10) {
                    toolbarPillsContent
                }
            } else {
                toolbarPillsContent
            }
        }
    }

    private var toolbarPillsContent: some View {
        HStack(spacing: 10) {
            typeFilterPill
            Spacer(minLength: 8)
            markAllReadPill
        }
    }

    /// 左药丸：type filter Menu。色点取当前 filter 对应颜色，count 显示该 filter 下的总数。
    ///
    /// Menu 选项里**不再包含 `.test`** —— Test 通知是开发期调试用，user 角度不
    /// 该把它当作可筛选的常规类别（实际很少出现 + 出现时一般也用不着 filter）。
    private var typeFilterPill: some View {
        let scope = currentFilterScope()
        return Menu {
            Picker(selection: typeFilterBinding) {
                Text("All types").tag(NotificationItem.Kind?.none)
                Divider()
                Text("New · Book").tag(NotificationItem.Kind?.some(.book))
                Text("New · Lottery").tag(NotificationItem.Kind?.some(.lottery))
                Text("Status change").tag(NotificationItem.Kind?.some(.status))
                Text("Alerts").tag(NotificationItem.Kind?.some(.alert))
                Text("System").tag(NotificationItem.Kind?.some(.system))
            } label: { Text("Filter type") }
        } label: {
            ZStack(alignment: .leading) {
                typeFilterPillContent(
                    label: "Lottery",
                    count: max(scope.count, store.notifications.count),
                    dot: .statusLottery,
                    fillsAvailableWidth: false
                )
                .hidden()
                .overlay(alignment: .leading) {
                    typeFilterPillContent(
                        label: scope.label,
                        count: scope.count,
                        dot: scope.dot,
                        fillsAvailableWidth: true
                    )
                }
            }
            // padding 与 Live capsule 完全相同 (14h / 9v)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .fixedSize(horizontal: true, vertical: false)
            .alertLiquidGlassCapsule()
            .transaction { tx in
                tx.animation = nil
                tx.disablesAnimations = true
            }
        }
        .accessibilityLabel("Filter alerts by type")
        .accessibilityValue("\(scope.label), \(scope.count) items")
    }

    private func typeFilterPillContent(
        label: String,
        count: Int,
        dot: Color,
        fillsAvailableWidth: Bool
    ) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(dot)
                .frame(width: 8, height: 8)
            Text(label)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            if fillsAvailableWidth {
                Spacer(minLength: 12)
            }
            Text("\(count)")
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .foregroundStyle(.blue)
                .lineLimit(1)
                .monospacedDigit()
                .fixedSize(horizontal: true, vertical: false)
            Image(systemName: "chevron.down")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
        }
    }

    /// 右药丸：Mark all read。有未读时展开；无未读时收成单 checkmark。
    private var markAllReadPill: some View {
        let hasUnread = store.unreadCount > 0
        return Button {
            guard hasUnread else { return }
            markAllReadTick &+= 1
            Task { await store.markAllRead() }
        } label: {
            HStack(spacing: 7) {
                Image(systemName: "checkmark")
                    .font(.system(size: 13, weight: .bold))
                if hasUnread {
                    Text("Mark all read")
                        .font(.system(size: 14, weight: .semibold))
                        .lineLimit(1)
                }
            }
            .foregroundStyle(.blue)
            // 与 Live capsule + typeFilterPill 同款 padding (14h / 9v)
            .padding(.horizontal, hasUnread ? 14 : 12)
            .padding(.vertical, 9)
            .frame(minWidth: hasUnread ? nil : 44, minHeight: 44)
            .fixedSize(horizontal: true, vertical: false)
            .contentShape(Capsule())
            .alertLiquidGlassCapsule()
        }
        .buttonStyle(.plain)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: hasUnread)
        .accessibilityLabel(hasUnread ? "Mark all alerts as read" : "All alerts read")
    }

    // MARK: - Live pill (matches Dashboard/Browse)

    /// Live capsule —— 视觉与 DashboardView.liveBadge 完全对齐：
    /// - Capsule 形状（不是 RoundedRectangle）
    /// - `Color(.systemBackground)` 底色 + secondary 15% stroke 1pt
    /// - padding 14h / 9v
    /// - 文字字号 .subheadline（15pt），mono 数字加 .bold
    /// - 左 10pt 绿点 + 2.4× ripple + 1.12× 核心呼吸（DashboardView 同款）
    /// **左对齐**：与 "Alerts" 标题左缘对齐，HStack 用 trailing Spacer
    /// 占满剩余空间，capsule 紧贴左侧（不居中）
    private var livePill: some View {
        let count = todayItems.count
        let ago = livePillAgo
        return HStack {
            HStack(spacing: 7) {
                liveDot
                (Text("\(count)")
                    .font(.system(size: 15, weight: .bold, design: .monospaced))
                    .foregroundColor(.primary)
                 + Text(" today")
                    .font(.subheadline)
                    .foregroundColor(.primary)
                 + Text(" · updated \(ago)")
                    .font(.subheadline)
                    .foregroundColor(.secondary))
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
            .background(Color(.systemBackground), in: Capsule())
            .overlay(Capsule().strokeBorder(.secondary.opacity(0.15), lineWidth: 1))
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(count) alerts today, updated \(ago)")
    }

    /// 与 DashboardView.liveBadge 完全对齐：10pt 核心 + 2.4× ripple + 1.12× 核心呼吸。
    /// `.frame(width: 10, height: 10)` 锁定布局尺寸，ripple 仅视觉上溢出。
    @ViewBuilder
    private var liveDot: some View {
        ZStack {
            if !reduceMotion {
                Circle()
                    .fill(Color.green)
                    .frame(width: 10, height: 10)
                    // 减小动画幅度：ripple 1.7×（之前 2.4×）+ opacity 起始 0.35
                    // （之前 0.45）—— 整体光晕收紧，不再有"扩散到旁边文字"的感觉
                    .scaleEffect(liveDotBreathing ? 1.7 : 1.0)
                    .opacity(liveDotBreathing ? 0.0 : 0.35)
                    .animation(
                        .easeOut(duration: 1.6).repeatForever(autoreverses: false),
                        value: liveDotBreathing
                    )
            }
            Circle()
                .fill(Color.green)
                .frame(width: 10, height: 10)
                // 核心呼吸缩放也收紧：1.06× (之前 1.12×)，更微妙的"在原地"呼吸感
                .scaleEffect(reduceMotion ? 1.0 : (liveDotBreathing ? 1.06 : 1.0))
                .animation(
                    reduceMotion
                        ? nil
                        : .easeInOut(duration: 1.6).repeatForever(autoreverses: true),
                    value: liveDotBreathing
                )
                .shadow(color: .green.opacity(0.4), radius: 7, x: 0, y: 0)
        }
        .frame(width: 10, height: 10)
    }

    private var livePillAgo: String {
        // 最近一条通知的 ageText 当作"最后更新"——SSE 推送到时就刷
        guard let newest = store.notifications.first else { return "just now" }
        let age = newest.ageText
        if age.isEmpty || age == "now" { return "just now" }
        return age + " ago"
    }

    // MARK: - Filter scope helper

    private struct FilterScope {
        let label: String
        let dot: Color
        let count: Int
    }

    private func currentFilterScope() -> FilterScope {
        guard let kind = typeFilter else {
            return FilterScope(
                label: "All",
                dot: .blue,
                count: store.notifications.count
            )
        }
        let count = kindCounts[kind] ?? 0
        switch kind {
        case .book:    return FilterScope(label: "Book",    dot: .statusBook,    count: count)
        case .lottery: return FilterScope(label: "Lottery", dot: .statusLottery, count: count)
        case .status:  return FilterScope(label: "Status",  dot: .blue,          count: count)
        case .alert:   return FilterScope(label: "Alerts",  dot: .red,           count: count)
        case .test:    return FilterScope(label: "Test",    dot: .blue,          count: count)
        case .system:  return FilterScope(label: "System",  dot: Color(.tertiaryLabel), count: count)
        }
    }

    // MARK: - Tap handling

    private func handleTap(_ notification: NotificationItem) {
        if !notification.isRead {
            Task { await store.markRead(ids: [notification.id]) }
        }
        let id = notification.listingID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty else { return }
        let titleHint = notification.listingTitleHint
        notificationsPath.append(.byId(id, titleHint: titleHint.isEmpty ? nil : titleHint))
    }

    // MARK: - Day grouping (cached)

    /// 单次 O(n) 扫 notifications，同时产出：
    /// - 三个时间桶（today / yesterday / earlier）
    /// - 每种 kind 的总数（``kindCounts``，供 filter pill 用，避免二次扫描）
    private func rebucketDayGroups() {
        var today: [NotificationItem] = []
        var yesterday: [NotificationItem] = []
        var earlier: [NotificationItem] = []
        var counts: [NotificationItem.Kind: Int] = [:]
        let filter = typeFilter
        for n in store.notifications {
            counts[n.kind, default: 0] += 1
            if let f = filter, n.kind != f { continue }
            switch n.dayBucket {
            case .today:     today.append(n)
            case .yesterday: yesterday.append(n)
            case .earlier:   earlier.append(n)
            }
        }
        todayItems = today
        yesterdayItems = yesterday
        earlierItems = earlier
        kindCounts = counts
    }
}

private extension View {
    @ViewBuilder
    func alertLiquidGlassCapsule() -> some View {
        if #available(iOS 26.0, *) {
            self.glassEffect(
                .regular.interactive(),
                in: Capsule()
            )
        } else {
            self
                .background(Color(.systemBackground), in: Capsule())
                .overlay(
                    Capsule()
                        .strokeBorder(.white.opacity(0.42), lineWidth: 0.6)
                )
                .overlay(
                    Capsule()
                        .strokeBorder(.black.opacity(0.05), lineWidth: 0.35)
                        .padding(0.5)
                )
                .shadow(color: .black.opacity(0.07), radius: 10, x: 0, y: 4)
        }
    }
}
