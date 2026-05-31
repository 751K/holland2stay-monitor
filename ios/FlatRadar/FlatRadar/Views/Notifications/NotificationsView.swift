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

    // 当前**已按 typeFilter 过滤**的三个时间桶（直接喂给 List 渲染）
    @State private var todayItems: [NotificationItem] = []
    @State private var yesterdayItems: [NotificationItem] = []
    @State private var earlierItems: [NotificationItem] = []
    @State private var kindCounts: [NotificationItem.Kind: Int] = [:]

    // **未过滤**的全量三桶——只在 notifications 数据变化时算一次（含唯一的
    // 日期解析）。typeFilter 切换时直接从这里按 kind（O(1) 存储字段）过滤，
    // 不再重复解析日期、不再触发动画式整列重建 → 切类型瞬时完成。
    @State private var allToday: [NotificationItem] = []
    @State private var allYesterday: [NotificationItem] = []
    @State private var allEarlier: [NotificationItem] = []

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
                // 只做按 kind 的廉价过滤（无日期解析）。不用 withAnimation 包：
                // 给整列 diff 加 0.22s 动画会让上百行逐行进出 + 与下面的 scrollTo
                // 动画叠加 → 卡顿。过滤本身瞬时，列表直接换数据即可。
                applyTypeFilter()
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
                        // 行作为 LazyVStack 的**直接 ForEach 子节点**——真正懒加载，
                        // 只渲染可视区行。卡片视觉由每行自绘 UnevenRoundedRectangle
                        // 切片承担（首行圆上角 / 尾行圆下角），不再靠一个把所有行
                        // 关进非懒 VStack 的统一卡片容器。
                        if !todayItems.isEmpty {
                            lazyGroup(title: "TODAY · \(todayItems.count)", items: todayItems)
                        }
                        if !yesterdayItems.isEmpty {
                            lazyGroup(title: "YESTERDAY · \(yesterdayItems.count)", items: yesterdayItems)
                        }
                        if !earlierItems.isEmpty {
                            lazyGroup(title: "EARLIER · \(earlierItems.count)", items: earlierItems)
                        }

                        if store.isLoadingMore {
                            HStack { Spacer(); ProgressView(); Spacer() }
                                .padding(.vertical, 12)
                        }
                        Color.clear.frame(height: 28)   // 底部留白
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

    /// 一个时间分组（TODAY / YESTERDAY / EARLIER）。返回「标题 + ForEach 行」，
    /// 直接铺在 LazyVStack 的 Section 里 → ForEach 保持懒加载（只建可视行）。
    ///
    /// 卡片观感：每行自绘 ``UnevenRoundedRectangle`` 切片，首行圆上两角、尾行
    /// 圆下两角、中间方角，0 间距堆叠成一张连续卡。去掉了原来整组外描边
    /// （逐切片描边会在接缝出双线）；靠 grouped 背景与卡片填充的对比承载卡感，
    /// 即 iOS 设置那种 inset-grouped 风格。
    @ViewBuilder
    private func lazyGroup(title: String, items: [NotificationItem]) -> some View {
        Text(title)
            .font(.system(size: 11, weight: .bold, design: .monospaced))
            .tracking(0.8)
            .foregroundStyle(.blue)
            .textCase(nil)
            .padding(.horizontal, 16)
            .padding(.top, 20)
            .padding(.bottom, 8)

        let firstID = items.first?.id
        let lastID = items.last?.id
        ForEach(items) { n in
            VStack(spacing: 0) {
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
                if n.id != lastID {
                    Divider().padding(.leading, 14)
                }
            }
            .background(
                UnevenRoundedRectangle(
                    topLeadingRadius: n.id == firstID ? 22 : 0,
                    bottomLeadingRadius: n.id == lastID ? 22 : 0,
                    bottomTrailingRadius: n.id == lastID ? 22 : 0,
                    topTrailingRadius: n.id == firstID ? 22 : 0,
                    style: .continuous
                )
                .fill(Color(.secondarySystemGroupedBackground))
            )
            .padding(.horizontal, 16)
        }
    }

    private var visibleLastNotificationID: Int? {
        earlierItems.last?.id ?? yesterdayItems.last?.id ?? todayItems.last?.id
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
            // 直接按真实内容自适应宽度。之前用"隐藏 Lottery 模板撑宽 + overlay
            // 真内容"的写法：模板用 fillsAvailableWidth:false（无 spacer）而真内容
            // 用 true（有 Spacer(min:12)）→ 真内容恒比模板宽；且 "System"/"Status"
            // 比硬编码的 "Lottery" 渲染还宽 → chevron 溢出胶囊。去掉模板，胶囊随
            // 内容收缩，永不溢出。切类型时宽度会微调，但用 transaction 关了动画，
            // 是干脆的 snap，不跳。
            typeFilterPillContent(
                label: scope.label,
                count: scope.count,
                dot: scope.dot,
                fillsAvailableWidth: false
            )
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

    /// 数据变化时调用（首次加载 / store.revision）。**唯一**做日期解析的地方：
    /// 单次 O(n) 扫 notifications，产出未过滤的全量三桶 + 每 kind 计数，然后
    /// 套用当前 typeFilter。日期解析每条只发生一次/数据轮。
    private func rebucketDayGroups() {
        var today: [NotificationItem] = []
        var yesterday: [NotificationItem] = []
        var earlier: [NotificationItem] = []
        var counts: [NotificationItem.Kind: Int] = [:]
        for n in store.notifications {
            counts[n.kind, default: 0] += 1
            switch n.dayBucket {   // ← 日期解析仅此一处
            case .today:     today.append(n)
            case .yesterday: yesterday.append(n)
            case .earlier:   earlier.append(n)
            }
        }
        allToday = today
        allYesterday = yesterday
        allEarlier = earlier
        kindCounts = counts
        applyTypeFilter()
    }

    /// typeFilter 切换时调用：只按 kind（存储字段，O(1)）从全量三桶里筛，
    /// **零日期解析**。这是切类型不再卡的关键。
    private func applyTypeFilter() {
        guard let f = typeFilter else {
            todayItems = allToday
            yesterdayItems = allYesterday
            earlierItems = allEarlier
            return
        }
        todayItems = allToday.filter { $0.kind == f }
        yesterdayItems = allYesterday.filter { $0.kind == f }
        earlierItems = allEarlier.filter { $0.kind == f }
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
