import SwiftUI

/// Listings 视图 —— BrowseView 内嵌的"列表"模式。
///
/// 不再持有 NavigationStack；外层 BrowseView 提供 NavigationStack(path:) +
/// navigationDestination，本视图只贡献内容 + 自己的 toolbar item。
struct ListingsView: View {
    @Environment(ListingsStore.self) private var store
    @Environment(NavigationCoordinator.self) private var coord
    @State private var searchText = ""
    @State private var searchDraft = ""
    @State private var showSearch = false
    @State private var showFilters = false
    @State private var showRefreshError = false
    /// Filter Apply 触觉反馈 trigger —— 每按一次 Apply 自增，驱动 `.sensoryFeedback`
    @State private var filterApplyTick = 0
    @State private var selectedStatus = ""
    @State private var sort = ListingSort.newest
    @State private var selectedCities: [String] = []
    @State private var selectedTypes: [String] = []
    @State private var selectedContract = ""
    @State private var selectedEnergy = ""

    var body: some View {
        Group {
            if store.isLoading && store.listings.isEmpty {
                ProgressView().padding(.top, 60)
            } else if let err = store.errorMessage, store.listings.isEmpty {
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
            } else if store.listings.isEmpty {
                ContentUnavailableView(
                    "No Listings",
                    systemImage: "house",
                    description: Text(store.isFiltered
                        ? "No listings match your filter."
                        : "No listings found."))
                .refreshable { await store.refresh() }
            } else {
                listContent
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button {
                    searchDraft = searchText
                    withAnimation(.spring(duration: 0.28, bounce: 0.12)) {
                        showSearch.toggle()
                    }
                } label: {
                    Label(searchButtonTitle, systemImage: searchText.isEmpty
                        ? "magnifyingglass"
                        : "magnifyingglass.circle.fill")
                }
                .tint(searchText.isEmpty ? nil : .blue)

                Menu {
                    Picker("Sort", selection: $sort) {
                        ForEach(ListingSort.allCases) { option in
                            Label(option.title, systemImage: option.systemImage)
                                .tag(option)
                        }
                    }
                } label: {
                    Label("Sort", systemImage: "arrow.up.arrow.down")
                }

                Button {
                    showFilters = true
                } label: {
                    Label(filterButtonTitle, systemImage: activeFilterCount > 0
                        ? "line.3.horizontal.decrease.circle.fill"
                        : "line.3.horizontal.decrease.circle")
                }
                .tint(activeFilterCount > 0 ? .blue : nil)
            }
        }
        .sheet(isPresented: $showFilters) {
            ListingFilterSheet(
                selectedStatus: $selectedStatus,
                selectedCities: $selectedCities,
                selectedTypes: $selectedTypes,
                selectedContract: $selectedContract,
                selectedEnergy: $selectedEnergy,
                activeFilterCount: activeFilterCount,
                apply: {
                    filterApplyTick &+= 1   // 触发 .sensoryFeedback(.selection, …)
                    showFilters = false
                    Task { await fetchWithCurrentFilters() }
                },
                reset: {
                    selectedStatus = ""
                    selectedCities = []
                    selectedTypes = []
                    selectedContract = ""
                    selectedEnergy = ""
                    showFilters = false
                    Task { await fetchWithCurrentFilters() }
                })
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
        // Filter Apply 轻触反馈 —— 用 .selection 比 .success 更合适：
        // 应用过滤器是 UI 选择确认动作，不是成功完成型操作。
        .sensoryFeedback(.selection, trigger: filterApplyTick)
    }

    private var listContent: some View {
        // 单次 O(n) 排序 + 分桶：之前是排序 + 两次 filter 扫表（3 × O(n)）。
        // 现在排序完一次 walk-through 直接分到 new / earlier，省两次 filter。
        let sorted = displayedListings
        var new: [Listing] = []
        var earlier: [Listing] = []
        new.reserveCapacity(sorted.count)
        earlier.reserveCapacity(sorted.count)
        for l in sorted {
            if l.isNew { new.append(l) } else { earlier.append(l) }
        }

        return List {
            // —— Live 心跳条 + 活跃 filter chips
            Section {
                if showSearch { inlineSearchRow }
                heartbeatRow
                if !activeFilterChips.isEmpty { filterChipsRow }
            }
            .listRowSeparator(.hidden)

            let lastID = sorted.last?.id

            // —— NEW TODAY · N
            if !new.isEmpty {
                Section {
                    ForEach(new) { listing in
                        row(for: listing, lastID: lastID)
                    }
                } header: {
                    sectionHeader(title: "NEW TODAY · \(new.count)",
                                  color: Color(red: 52/255, green: 199/255, blue: 89/255))
                }
            }

            // —— EARLIER
            if !earlier.isEmpty {
                Section {
                    ForEach(earlier) { listing in
                        row(for: listing, lastID: lastID)
                    }
                } header: {
                    sectionHeader(title: new.isEmpty ? "ALL LISTINGS" : "EARLIER",
                                  color: .secondary)
                }
            }

            if store.isLoadingMore {
                HStack {
                    Spacer()
                    ProgressView()
                    Spacer()
                }
                .listRowSeparator(.hidden)
            }
        }
        // .insetGrouped（默认）：灰底 + 白色 inset section 卡片，跟
        // Settings / Notifications / Dashboard 风格一致。
        .listStyle(.insetGrouped)
        .refreshable { await store.refresh() }
    }

    @ViewBuilder
    private func row(for listing: Listing, lastID: String?) -> some View {
        Button {
            coord.listingsPath.append(ListingRoute.known(listing))
        } label: {
            HStack(spacing: 0) {
                ListingRow(listing: listing)
                Spacer(minLength: 10)
                Image(systemName: "chevron.right")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        .buttonStyle(ScaleButtonStyle())
        .onAppear {
            if listing.id == lastID {
                Task { await store.loadMore() }
            }
        }
    }

    @ViewBuilder
    private func sectionHeader(title: String, color: Color) -> some View {
        Text(title)
            .font(.system(size: 11, weight: .bold, design: .monospaced))
            .tracking(0.7)
            .foregroundStyle(color)
            .textCase(nil)
            .padding(.top, 4)
    }

    // MARK: - Heartbeat + chips

    @ViewBuilder
    private var heartbeatRow: some View {
        HStack(spacing: 7) {
            Circle()
                .fill(Color.green)
                .frame(width: 6, height: 6)
            (Text("\(store.total)").font(.system(size: 12, weight: .bold, design: .monospaced))
                + Text(" listings").font(.system(size: 12)))
                .foregroundStyle(.primary)
            Text("·")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            Text("updated \(updatedAgoText)")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.vertical, 2)
    }

    private var inlineSearchRow: some View {
        HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
            TextField("Search by name or address", text: $searchDraft)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .onSubmit {
                    applySearch()
                }
            if !searchDraft.isEmpty {
                Button {
                    searchDraft = ""
                    if !searchText.isEmpty {
                        searchText = ""
                        Task { await fetchWithCurrentFilters() }
                    }
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }
            Button("Search") {
                applySearch()
            }
            .font(.subheadline.weight(.semibold))
            .disabled(searchDraft.trimmingCharacters(in: .whitespacesAndNewlines) == searchText)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 14))
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    @ViewBuilder
    private var filterChipsRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(activeFilterChips) { chip in
                    Button {
                        chip.remove()
                    } label: {
                        HStack(spacing: 5) {
                            Text(chip.label)
                                .font(.system(size: 12, weight: .semibold,
                                              design: chip.mono ? .monospaced : .default))
                            Image(systemName: "xmark")
                                .font(.system(size: 8, weight: .bold))
                        }
                        .padding(.leading, 11)
                        .padding(.trailing, 9)
                        .padding(.vertical, 5)
                        .background(
                            Capsule().fill(chip.active ? Color.accentColor : Color(.secondarySystemBackground))
                        )
                        .foregroundStyle(chip.active ? Color.white : Color.primary)
                        .overlay(
                            Capsule().stroke(Color.primary.opacity(0.08), lineWidth: chip.active ? 0 : 0.5)
                        )
                        // 视觉 chip 高约 22pt（保留紧凑设计），用 minHeight + contentShape
                        // 把按钮的命中区上下补到 44pt 满足 HIG，不让 chip 视觉变高。
                        .frame(minHeight: 44)
                        .contentShape(Capsule())
                        .shadow(color: chip.active ? Color.accentColor.opacity(0.25) : .clear,
                                radius: 4, x: 0, y: 2)
                    }
                    .buttonStyle(.plain)
                }
                if activeFilterChips.count > 1 {
                    Button("Clear all", role: .destructive) {
                        clearAllFilters()
                    }
                    .font(.system(size: 12, weight: .semibold))
                    .padding(.leading, 4)
                }
            }
        }
    }

    // MARK: - Derived data

    private var displayedListings: [Listing] {
        store.listings.sorted(using: sort)
    }

    private var updatedAgoText: String {
        guard let last = store.lastUpdated else { return "just now" }
        let interval = Date().timeIntervalSince(last)
        if interval < 5 { return "just now" }
        if interval < 60 { return "\(Int(interval))s ago" }
        if interval < 3600 { return "\(Int(interval / 60))m ago" }
        if interval < 86400 { return "\(Int(interval / 3600))h ago" }
        return "\(Int(interval / 86400))d ago"
    }

    private struct FilterChipModel: Identifiable {
        let id = UUID()
        let label: String
        let active: Bool
        let mono: Bool
        let remove: () -> Void
    }

    private var activeFilterChips: [FilterChipModel] {
        var chips: [FilterChipModel] = []
        if !searchText.isEmpty {
            chips.append(.init(label: "Search: \(searchText)", active: true, mono: false) {
                searchText = ""
                searchDraft = ""
                Task { await fetchWithCurrentFilters() }
            })
        }
        if !selectedStatus.isEmpty {
            chips.append(.init(label: shortStatusLabel(selectedStatus), active: true, mono: false) {
                selectedStatus = ""
                Task { await fetchWithCurrentFilters() }
            })
        }
        for city in selectedCities {
            chips.append(.init(label: city, active: false, mono: false) {
                selectedCities.removeAll { $0 == city }
                Task { await fetchWithCurrentFilters() }
            })
        }
        for t in selectedTypes {
            chips.append(.init(label: t, active: false, mono: false) {
                selectedTypes.removeAll { $0 == t }
                Task { await fetchWithCurrentFilters() }
            })
        }
        if !selectedContract.isEmpty {
            chips.append(.init(label: selectedContract, active: false, mono: false) {
                selectedContract = ""
                Task { await fetchWithCurrentFilters() }
            })
        }
        if !selectedEnergy.isEmpty {
            chips.append(.init(label: "Energy ≥ \(selectedEnergy)", active: false, mono: true) {
                selectedEnergy = ""
                Task { await fetchWithCurrentFilters() }
            })
        }
        return chips
    }

    private func shortStatusLabel(_ raw: String) -> String {
        let s = raw.lowercased()
        if s.contains("available to book") { return "Book" }
        if s.contains("lottery") { return "Lottery" }
        if s.contains("reserved") { return "Reserved" }
        if s.contains("rented") { return "Rented" }
        if s.contains("not available") { return "Unavailable" }
        return raw
    }

    private func clearAllFilters() {
        selectedStatus = ""
        selectedCities = []
        selectedTypes = []
        selectedContract = ""
        selectedEnergy = ""
        searchText = ""
        searchDraft = ""
        Task { await fetchWithCurrentFilters() }
    }

    private func applySearch() {
        let trimmed = searchDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed != searchText else { return }
        searchText = trimmed
        Task { await fetchWithCurrentFilters() }
    }

    private var activeFilterCount: Int {
        ([selectedStatus].filter { !$0.isEmpty }.count
         + (searchText.isEmpty ? 0 : 1)
         + (selectedCities.isEmpty ? 0 : 1)
         + (selectedTypes.isEmpty ? 0 : 1)
         + (selectedContract.isEmpty ? 0 : 1)
         + (selectedEnergy.isEmpty ? 0 : 1))
    }

    private var searchButtonTitle: String {
        searchText.isEmpty ? "Search" : "Search: \(searchText)"
    }

    private var filterButtonTitle: String {
        activeFilterCount > 0 ? "Filters (\(activeFilterCount))" : "Filters"
    }

    private func fetchWithCurrentFilters() async {
        // Backend treats single-city cities= as SQL level; multi-city as Python filter
        let citiesParam = selectedCities.isEmpty ? nil : selectedCities
        await store.fetch(
            city: (selectedCities.count == 1 ? selectedCities[0] : nil),
            status: selectedStatus.nilIfEmpty,
            query: searchText.nilIfEmpty,
            cities: citiesParam,
            types: selectedTypes.isEmpty ? nil : selectedTypes,
            contract: selectedContract.nilIfEmpty,
            energy: selectedEnergy.nilIfEmpty)
    }
}

private enum ListingSort: String, CaseIterable, Identifiable {
    case newest
    case priceLow
    case priceHigh
    case availableSoon
    case city
    case name

    var id: String { rawValue }

    var title: String {
        switch self {
        case .newest: return "Newest"
        case .priceLow: return "Price: Low to High"
        case .priceHigh: return "Price: High to Low"
        case .availableSoon: return "Available Soon"
        case .city: return "City"
        case .name: return "Name"
        }
    }

    var systemImage: String {
        switch self {
        case .newest: return "clock.arrow.circlepath"
        case .priceLow: return "eurosign.arrow.circlepath"
        case .priceHigh: return "eurosign.circle"
        case .availableSoon: return "calendar.badge.clock"
        case .city: return "building.2"
        case .name: return "textformat.abc"
        }
    }
}

private extension Array where Element == Listing {
    func sorted(using sort: ListingSort) -> [Listing] {
        switch sort {
        case .newest:
            return sorted { ($0.firstSeen ?? "") > ($1.firstSeen ?? "") }
        case .priceLow:
            return sorted { ($0.priceValue ?? .greatestFiniteMagnitude) < ($1.priceValue ?? .greatestFiniteMagnitude) }
        case .priceHigh:
            return sorted { ($0.priceValue ?? -.greatestFiniteMagnitude) > ($1.priceValue ?? -.greatestFiniteMagnitude) }
        case .availableSoon:
            return sorted { ($0.availableDayKey ?? "9999-99-99") < ($1.availableDayKey ?? "9999-99-99") }
        case .city:
            return sorted { lhs, rhs in
                if lhs.city == rhs.city { return lhs.name.localizedCaseInsensitiveCompare(rhs.name) == .orderedAscending }
                return lhs.city.localizedCaseInsensitiveCompare(rhs.city) == .orderedAscending
            }
        case .name:
            return sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        }
    }
}

private extension String {
    var nilIfEmpty: String? {
        let trimmed = trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

private struct ListingFilterSheet: View {
    @Binding var selectedStatus: String
    @Binding var selectedCities: [String]
    @Binding var selectedTypes: [String]
    @Binding var selectedContract: String
    @Binding var selectedEnergy: String

    let activeFilterCount: Int
    let apply: () -> Void
    let reset: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var options = FilterOptions.empty
    @State private var isLoadingOptions = false

    /// Expandable sections
    @State private var showCities = false
    @State private var showTypes = false

    // ── 未保存变更追踪 ───────────────────────────────────────────────
    // 打开 sheet 时快照初始值；后续比对得 hasUnsavedChanges；
    // dirty 时阻止下滑 dismiss，Cancel 弹 confirmation。
    @State private var initialStatus = ""
    @State private var initialCities: [String] = []
    @State private var initialTypes: [String] = []
    @State private var initialContract = ""
    @State private var initialEnergy = ""
    @State private var snapshotTaken = false
    @State private var showDiscardConfirm = false

    private var hasUnsavedChanges: Bool {
        guard snapshotTaken else { return false }
        return selectedStatus != initialStatus
            || selectedCities != initialCities
            || selectedTypes != initialTypes
            || selectedContract != initialContract
            || selectedEnergy != initialEnergy
    }

    var body: some View {
        NavigationStack {
            Form {
                // Cities: multi-select
                Section {
                    if isLoadingOptions && options.cities.isEmpty {
                        ProgressView()
                    } else if options.cities.isEmpty {
                        Text("No cities available").font(.subheadline).foregroundStyle(.secondary)
                    } else if options.cities.count > 6 {
                        DisclosureGroup(isExpanded: $showCities) {
                            multiSelectRows(choices: options.cities, selection: $selectedCities)
                        } label: {
                            HStack {
                                Text(selectedCities.isEmpty ? "All Cities" : "\(selectedCities.count) selected")
                                Spacer()
                            }
                        }
                    } else {
                        multiSelectRows(choices: options.cities, selection: $selectedCities)
                    }
                } header: {
                    Label("Cities", systemImage: "building.2.fill")
                }

                // Status: single picker
                Section {
                    Picker("Status", selection: $selectedStatus) {
                        Text("All Statuses").tag("")
                        ForEach(availableStatusesFromOptions, id: \.self) { s in
                            Text(s).tag(s)
                        }
                    }
                } header: {
                    Label("Status", systemImage: "tag.fill")
                }

                // Types: multi-select
                Section {
                    if isLoadingOptions && options.types.isEmpty {
                        ProgressView()
                    } else if options.types.isEmpty {
                        Text("No types available").font(.subheadline).foregroundStyle(.secondary)
                    } else if options.types.count > 6 {
                        DisclosureGroup(isExpanded: $showTypes) {
                            multiSelectRows(choices: options.types, selection: $selectedTypes)
                        } label: {
                            HStack {
                                Text(selectedTypes.isEmpty ? "All Types" : "\(selectedTypes.count) selected")
                                Spacer()
                            }
                        }
                    } else {
                        multiSelectRows(choices: options.types, selection: $selectedTypes)
                    }
                } header: {
                    Label("Type", systemImage: "house.lodge")
                }

                // Contract: single picker
                Section {
                    Picker("Contract", selection: $selectedContract) {
                        Text("Any").tag("")
                        ForEach(options.contract, id: \.self) { c in
                            Text(c).tag(c)
                        }
                    }
                } header: {
                    Label("Contract", systemImage: "calendar")
                }

                // Energy: min level picker
                Section {
                    Picker("Min energy label", selection: $selectedEnergy) {
                        Text("Any").tag("")
                        ForEach(options.energy.isEmpty ? energyLabels : options.energy, id: \.self) { label in
                            Text(label).tag(label)
                        }
                    }
                    .pickerStyle(.menu)
                } header: {
                    Label("Energy", systemImage: "bolt.fill")
                } footer: {
                    Text("Min B = A/A+/A++/A+++ also accepted; C and worse filtered out.")
                }

                // Reset
                if activeFilterCount > 0 {
                    Section {
                        Button("Reset All Filters", role: .destructive, action: reset)
                    }
                }
            }
            .navigationTitle("Filters")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        if hasUnsavedChanges {
                            showDiscardConfirm = true
                        } else {
                            dismiss()
                        }
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply", action: apply)
                }
            }
            // 脏数据时禁用下滑关闭——防止用户误操作丢掉刚改的筛选条件。
            // 强制走 Cancel/Apply 按钮路径（Cancel 会再弹 confirmation）。
            .interactiveDismissDisabled(hasUnsavedChanges)
            .confirmationDialog(
                "Discard filter changes?",
                isPresented: $showDiscardConfirm,
                titleVisibility: .visible
            ) {
                Button("Discard changes", role: .destructive) {
                    // 还原到打开 sheet 那一刻的初始值，避免下次再打开还看到脏数据
                    selectedStatus = initialStatus
                    selectedCities = initialCities
                    selectedTypes = initialTypes
                    selectedContract = initialContract
                    selectedEnergy = initialEnergy
                    dismiss()
                }
                Button("Keep editing", role: .cancel) {}
            }
            .task {
                // 第一次出现时快照初始 filter 值；后续 sheet 内修改 binding
                // 不会触发再次快照（snapshotTaken 守卫）。
                if !snapshotTaken {
                    initialStatus   = selectedStatus
                    initialCities   = selectedCities
                    initialTypes    = selectedTypes
                    initialContract = selectedContract
                    initialEnergy   = selectedEnergy
                    snapshotTaken   = true
                }
                await loadOptions()
            }
        }
    }

    /// Known status values. The backend SQL `WHERE status = ?` matches exactly;
    /// unused values simply return empty results.
    private var availableStatusesFromOptions: [String] {
        ["Available to book", "Available in lottery", "Not available", "Reserved", "Rented"]
    }

    @ViewBuilder
    private func multiSelectRows(choices: [String], selection: Binding<[String]>) -> some View {
        ForEach(choices, id: \.self) { c in
            Toggle(isOn: Binding(
                get: { selection.wrappedValue.contains(c) },
                set: { add in
                    if add {
                        if !selection.wrappedValue.contains(c) {
                            selection.wrappedValue.append(c)
                        }
                    } else {
                        selection.wrappedValue.removeAll { $0 == c }
                    }
                }
            )) {
                Text(c)
            }
        }
    }

    private func loadOptions() async {
        isLoadingOptions = true
        defer { isLoadingOptions = false }
        do {
            options = try await APIClient.shared.getFilterOptions()
        } catch {
            #if DEBUG
            print("[ListingFilterSheet] loadOptions error: \(error)")
            #endif
        }
    }
}
