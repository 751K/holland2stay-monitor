import SwiftUI

/// 入住日历视图。
///
/// 布局
/// ----
/// 1. 顶部月份切换条（← 当前月 → / 跳到今天）
/// 2. 7 列 weekday 表头（Mon..Sun，使用 ``Calendar.current`` 的 firstWeekday）
/// 3. 月格：每天显示数字 + 该日可入住数（小气泡 badge）
///    - 今天高亮蓝边
///    - 选中日填充蓝色背景
///    - 该日 0 套 → 数字灰
/// 4. 选中日的房源列表（点单条进 ListingDetailView via deep link）
///
/// 与 Map 共享一个交互模式：点元素弹底层 sheet，从 sheet 进详情走
/// ``NavigationCoordinator.openListing`` 复用 Listings tab 的 NavigationStack。
struct CalendarView: View {
    @Environment(CalendarStore.self) private var store
    @Environment(NavigationCoordinator.self) private var coord

    @State private var anchor: Date = Self.startOfMonth(for: Date())
    @State private var selectedDay: Date?
    @State private var showRefreshError = false
    /// 月份切换方向：-1 向前翻 / 1 向后翻 / 0 初始。
    /// 驱动 daysGrid 的 asymmetric transition 实现空间连续感。
    @State private var monthShift: Int = 0

    private static let cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = ServerTime.timeZone
        return c
    }()

    /// VoiceOver 日期朗读格式器——每格一个，DateFormatter 创建昂贵，static 复用。
    private static let a11yDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = .autoupdatingCurrent
        f.dateStyle = .full
        f.timeStyle = .none
        return f
    }()

    /// 月份标题 "May 2026"
    private static let monthTitleFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = cal
        f.timeZone = cal.timeZone
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMMM yyyy"
        return f
    }()

    /// 完整日期 "Wednesday, May 14, 2026"
    private static let longDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = cal
        f.timeZone = cal.timeZone
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateStyle = .full
        return f
    }()

    var body: some View {
        // 不再自带 NavigationStack；外层 BrowseView 提供。
        ScrollView {
                VStack(spacing: 16) {
                    if store.isLoading && store.listings.isEmpty {
                        ProgressView().padding(.top, 80)
                    } else if let err = store.errorMessage, store.listings.isEmpty {
                        let apiErr = store.lastError
                        ContentUnavailableView {
                            Label(
                                apiErr?.errorDescription ?? "Unable to Load",
                                systemImage: apiErr?.systemImage ?? "calendar.badge.exclamationmark")
                        } description: {
                            Text(err)
                        } actions: {
                            Button("Try Again") {
                                Task { await store.refresh() }
                            }
                        }
                    } else {
                        // 月份标题 / 星期行 / 日期格合成一张卡。原先三者各自
                        // 平铺在页面上，没有边界，读起来是一堆散元素而不是
                        // 「一个月历」。大面板用实体表面而不是玻璃——玻璃在大
                        // 面积上会把自己的内容也搅浑（地图那张说明卡踩过）。
                        VStack(spacing: 14) {
                            monthHeader
                            weekdayHeader
                            daysGrid
                        }
                        .padding(.vertical, 16)
                        .background(Color(.secondarySystemGroupedBackground),
                                    in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .padding(.horizontal, 16)
                        if let day = selectedDay {
                            dayListings(for: day)
                                .padding(.horizontal)
                        } else if store.listings.isEmpty {
                            ContentUnavailableView(
                                "No Move-In Dates",
                                systemImage: "calendar",
                                description: Text("Listings with available dates will appear here."))
                            .padding(.top, 40)
                        } else {
                            Text("Tap a day to view available listings.")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .padding(.top, 20)
                        }
                    }
                }
                .padding(.vertical)
            }
        .refreshable { await store.refresh() }
        .background(Color(.systemGroupedBackground))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    anchor = Self.startOfMonth(for: Date())
                    selectedDay = Date()
                } label: {
                    Text("Today").font(.subheadline.weight(.medium))
                }
                .disabled(Self.cal.isDate(anchor, equalTo: Self.startOfMonth(for: Date()),
                                          toGranularity: .month))
            }
        }
        .task {
            if store.listings.isEmpty {
                await store.fetch()
            }
        }
        .onChange(of: store.errorMessage) { _, new in
            showRefreshError = new != nil && !store.listings.isEmpty
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

    // MARK: - Header

    private var monthHeader: some View {
        HStack(spacing: 8) {
            Button { shiftMonth(-1) } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 36, height: 36)
                    .liquidGlass(Circle(), interactive: true)
            }
            .buttonStyle(.plain)
            .disabled(!canShiftMonth(-1))
            // icon-only：补 VoiceOver / Voice Control 用的语义化标签
            .accessibilityLabel("Previous month")

            Text(monthTitle(for: anchor))
                .font(.title2.weight(.semibold))
                .frame(maxWidth: .infinity)
                // 月份标题作为一个完整 a11y 元素朗读，避免 VO 把它和左右按钮
                // 错位关联
                .accessibilityAddTraits(.isHeader)

            Button { shiftMonth(1) } label: {
                Image(systemName: "chevron.right")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 36, height: 36)
                    .liquidGlass(Circle(), interactive: true)
            }
            .buttonStyle(.plain)
            .disabled(!canShiftMonth(1))
            .accessibilityLabel("Next month")
        }
        .padding(.horizontal)
    }

    private var weekdayHeader: some View {
        let names = orderedWeekdaySymbols()
        return LazyVGrid(columns: Self.gridColumns, spacing: 4) {
            ForEach(names, id: \.self) { name in
                Text(name)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
        }
        .padding(.horizontal)
    }

    // MARK: - Grid

    private static let gridColumns: [GridItem] = Array(
        repeating: GridItem(.flexible(), spacing: 4),
        count: 7)

    private var daysGrid: some View {
        let days = daysOfMonthWithPadding()
        return LazyVGrid(columns: Self.gridColumns, spacing: 6) {
            ForEach(days) { item in
                cell(for: item)
            }
        }
        .padding(.horizontal)
        // 月份切换时整格沿水平方向滑动进出：
        // 向前翻（←）→ grid 从右滑入 / 向左滑出
        // 向后翻（→）→ grid 从左滑入 / 向右滑出
        .transition(.asymmetric(
            insertion: .move(edge: monthShift > 0 ? .trailing : .leading).combined(with: .opacity),
            removal: .move(edge: monthShift < 0 ? .trailing : .leading).combined(with: .opacity)
        ))
        .id(anchor)   // anchor 是 Date，同月份 id 相同不会触发 transition
                       // 真正触发的是 monthShift 切换 → withAnimation body 重算
    }

    @ViewBuilder
    private func cell(for item: CalendarCell) -> some View {
        switch item {
        case .empty:
            Color.clear.frame(height: 50)
        case .day(let date):
            let count = store.listings(on: date).count
            let selected = selectedDay.flatMap {
                Self.cal.isDate($0, inSameDayAs: date)
            } ?? false
            let isToday = Self.cal.isDateInToday(date)
            Button {
                selectedDay = date
            } label: {
                VStack(spacing: 2) {
                    Text("\(Self.cal.component(.day, from: date))")
                        .font(.subheadline)
                        .fontWeight(selected ? .bold : .regular)
                        // 选中态**不用白字**。玻璃是透光的，白字的对比度会随底下
                        // 内容变——筛选 chip 上已经踩过。改成强调色加粗：色相
                        // 表示"选中"，对比度交给系统的中性玻璃保证。
                        .foregroundStyle(
                            selected ? Color.accentColor :
                                (count == 0 ? Color.secondary : Color.primary))
                    if count > 0 {
                        Text("\(count)")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(Color.accentColor)
                        .opacity(selected ? 1 : 0.85)
                    } else {
                        Text(" ")
                            .font(.caption2)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 50)
                // 选中态用玻璃，但**不给玻璃着色**——色相由上面的文字承担。
                // 有房源但未选中的那些用一层很淡的强调色底，和空格子区分开。
                .background(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .fill(!selected && count > 0
                              ? Color.accentColor.opacity(0.12) : Color.clear)
                )
                .modifier(SelectedDayGlass(active: selected))
                .overlay(
                    RoundedRectangle(cornerRadius: 14, style: .continuous)
                        .strokeBorder(isToday && !selected ? Color.accentColor : .clear,
                                      lineWidth: 1.5)
                )
                // 选中态平滑过渡：foregroundStyle / background / overlay 的颜色切换
                // 加上 spring 消除瞬间跳变的生硬感。
                .animation(.spring(duration: 0.2), value: selected)
            }
            .buttonStyle(.plain)
            // VoiceOver: 把整个格子当单个元素，朗读"<日期> · N listing(s) available"。
            // 不然 VO 会分别朗读里面的两个 Text（日期数字 + 计数），缺乏上下文。
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(accessibilityDescription(date: date, count: count, isToday: isToday))
            .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        }
    }

    /// VoiceOver 朗读串：完整日期 + 房源数 + Today 标识。
    private func accessibilityDescription(date: Date, count: Int, isToday: Bool) -> String {
        var parts: [String] = [Self.a11yDateFormatter.string(from: date)]
        if isToday { parts.append("Today") }
        switch count {
        case 0: parts.append("No listings available")
        case 1: parts.append("1 listing available")
        default: parts.append("\(count) listings available")
        }
        return parts.joined(separator: ", ")
    }

    // MARK: - Selected day listings

    @ViewBuilder
    private func dayListings(for date: Date) -> some View {
        let listings = store.listings(on: date)
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(longDateLabel(date)).font(.headline)
                Spacer()
                Text("\(listings.count) listings")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if listings.isEmpty {
                Text("No move-in on this day.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
            } else {
                ForEach(listings) { l in
                    Button {
                        if UIDevice.current.userInterfaceIdiom == .pad {
                            coord.openListing(id: l.id, titleHint: l.name)
                        } else {
                            coord.listingsPath.append(.byId(l.id, titleHint: l.name))
                        }
                    } label: {
                        listingRow(l)
                    }
                    .buttonStyle(ScaleButtonStyle())
                }
            }
        }
    }

    @ViewBuilder
    private func listingRow(_ l: CalendarListing) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(l.name)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(2)
                    PlatformBadge(source: l.source, size: .small)
                }
                // OurCampus 的 city 和 building 是同一个值，而标题里也有它——
                // 原来这里会把同一件事念三遍。去重逻辑见 PlaceSummary。
                if let place = PlaceSummary.text(name: l.name,
                                                 parts: [l.building, l.city]) {
                    Text(place)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Text(l.status)
                    .font(.caption2)
                    .foregroundStyle(statusColor(for: l.status))
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 4) {
                if !l.priceRaw.isEmpty {
                    Text(l.priceRaw)
                        .font(.subheadline.weight(.semibold))
                }
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .liquidGlass(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private func statusColor(for status: String) -> Color {
        ListingStatus.from(status).color
    }


    // MARK: - Helpers

    private func shiftMonth(_ delta: Int) {
        guard let next = Self.cal.date(byAdding: .month, value: delta, to: anchor) else { return }
        monthShift = delta
        withAnimation(.easeInOut(duration: 0.28)) {
            anchor = Self.startOfMonth(for: next)
        }
        selectedDay = nil
    }

    /// 仅当数据范围允许时才能切换；防止用户翻到没数据的月份。
    private func canShiftMonth(_ delta: Int) -> Bool {
        guard let range = store.dateRange else { return false }
        guard let target = Self.cal.date(byAdding: .month, value: delta, to: anchor) else { return false }
        let targetStart = Self.startOfMonth(for: target)
        let limitStart = Self.startOfMonth(for: delta < 0 ? range.start : range.end)
        return delta < 0
            ? targetStart >= limitStart
            : targetStart <= limitStart
    }

    private func monthTitle(for date: Date) -> String {
        Self.monthTitleFormatter.string(from: date)
    }

    private func longDateLabel(_ date: Date) -> String {
        Self.longDateFormatter.string(from: date)
    }

    /// 周一在前 / 周日在前等顺序符号；本地化无关，统一英文短名。
    private func orderedWeekdaySymbols() -> [String] {
        let symbols = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        let first = Self.cal.firstWeekday - 1   // 1..7 → 0..6
        return Array(symbols[first...] + symbols[..<first])
    }

    /// 生成当前 anchor 月所有日期 + 月首前后的占位单元（保证 7 列对齐）。
    private func daysOfMonthWithPadding() -> [CalendarCell] {
        guard let range = Self.cal.range(of: .day, in: .month, for: anchor) else { return [] }
        let monthStart = anchor
        let firstWeekday = Self.cal.component(.weekday, from: monthStart)  // 1=Sun
        let leadingEmpty = (firstWeekday - Self.cal.firstWeekday + 7) % 7
        var emptyIndex = 0
        func emptyCell() -> CalendarCell {
            defer { emptyIndex += 1 }
            return .empty(emptyIndex)
        }

        var out: [CalendarCell] = (0..<leadingEmpty).map { _ in emptyCell() }
        for d in range {
            if let date = Self.cal.date(byAdding: .day, value: d - 1, to: monthStart) {
                out.append(.day(date))
            }
        }
        // 尾部补到 7 的倍数（视觉对齐）
        let pad = (7 - out.count % 7) % 7
        out.append(contentsOf: (0..<pad).map { _ in emptyCell() })
        return out
    }

    private static func startOfMonth(for date: Date) -> Date {
        let comps = cal.dateComponents([.year, .month], from: date)
        return cal.date(from: comps) ?? date
    }
}

private enum CalendarCell: Identifiable, Hashable {
    case empty(Int)
    case day(Date)

    var id: String {
        switch self {
        case .empty(let index):
            return "empty-\(index)"
        case .day(let date):
            return "day-\(Int(date.timeIntervalSince1970))"
        }
    }
}


/// 选中那一天的玻璃底。
///
/// 单独抽成 ViewModifier，是因为 `liquidGlass` 只能在选中时加——写成行内的
/// `if` 会把 background 链拆成两条分支，SwiftUI 会当成两个不同的视图，选中/
/// 取消时整格重建，动画跳变。
private struct SelectedDayGlass: ViewModifier {
    let active: Bool

    func body(content: Content) -> some View {
        if active {
            content.liquidGlass(RoundedRectangle(cornerRadius: 14, style: .continuous),
                                interactive: true)
        } else {
            content
        }
    }
}
