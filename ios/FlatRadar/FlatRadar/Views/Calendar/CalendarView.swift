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

    private static let cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC") ?? .current
        return c
    }()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if store.isLoading && store.listings.isEmpty {
                        ProgressView().padding(.top, 80)
                    } else if let err = store.errorMessage, store.listings.isEmpty {
                        ContentUnavailableView(
                            "Unable to Load",
                            systemImage: "calendar.badge.exclamationmark",
                            description: Text(err))
                    } else {
                        monthHeader
                        weekdayHeader
                        daysGrid
                        if let day = selectedDay {
                            Divider().padding(.horizontal)
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
            .navigationTitle("Calendar")
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
        }
    }

    // MARK: - Header

    private var monthHeader: some View {
        HStack(spacing: 8) {
            Button { shiftMonth(-1) } label: {
                Image(systemName: "chevron.left").font(.title3)
            }
            .disabled(!canShiftMonth(-1))

            Text(monthTitle(for: anchor))
                .font(.title2.weight(.semibold))
                .frame(maxWidth: .infinity)

            Button { shiftMonth(1) } label: {
                Image(systemName: "chevron.right").font(.title3)
            }
            .disabled(!canShiftMonth(1))
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
            ForEach(days, id: \.self) { item in
                cell(for: item)
            }
        }
        .padding(.horizontal)
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
                        .foregroundStyle(
                            selected ? .white :
                                (count == 0 ? Color.secondary : Color.primary))
                    if count > 0 {
                        Text("\(count)")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(selected ? .white : .blue)
                    } else {
                        Text(" ")
                            .font(.caption2)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: 50)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill(selected ? Color.blue :
                              (count > 0 ? Color.blue.opacity(0.08) : Color.clear))
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(isToday ? Color.blue : .clear, lineWidth: 1.5)
                )
            }
            .buttonStyle(.plain)
        }
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
                        coord.openListing(id: l.id)
                    } label: {
                        listingRow(l)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder
    private func listingRow(_ l: CalendarListing) -> some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(l.name)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(l.city)
                    if !l.building.isEmpty {
                        Text("·")
                        Text(l.building)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
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
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private func statusColor(for status: String) -> Color {
        let s = status.lowercased()
        if s.contains("available to book") { return .green }
        if s.contains("lottery") { return .orange }
        if s.contains("not available") { return .gray }
        return .secondary
    }

    // MARK: - Helpers

    private func shiftMonth(_ delta: Int) {
        guard let next = Self.cal.date(byAdding: .month, value: delta, to: anchor) else { return }
        anchor = Self.startOfMonth(for: next)
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
        let f = DateFormatter()
        f.calendar = Self.cal
        f.timeZone = Self.cal.timeZone
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "MMMM yyyy"
        return f.string(from: date)
    }

    private func longDateLabel(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Self.cal
        f.timeZone = Self.cal.timeZone
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateStyle = .full
        return f.string(from: date)
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
        var out: [CalendarCell] = Array(repeating: .empty, count: leadingEmpty)
        for d in range {
            if let date = Self.cal.date(byAdding: .day, value: d - 1, to: monthStart) {
                out.append(.day(date))
            }
        }
        // 尾部补到 7 的倍数（视觉对齐）
        let pad = (7 - out.count % 7) % 7
        out.append(contentsOf: Array(repeating: .empty, count: pad))
        return out
    }

    private static func startOfMonth(for date: Date) -> Date {
        let comps = cal.dateComponents([.year, .month], from: date)
        return cal.date(from: comps) ?? date
    }
}

private enum CalendarCell: Hashable {
    case empty
    case day(Date)
}
