import SwiftUI

/// Listings 视图 —— BrowseView 内嵌的"列表"模式。
///
/// 不再持有 NavigationStack；外层 BrowseView 提供 NavigationStack(path:) +
/// navigationDestination，本视图只贡献内容 + `.searchable` + 自己的 toolbar item。
struct ListingsView: View {
    @Environment(ListingsStore.self) private var store
    @State private var searchText = ""
    @State private var showFilters = false
    @State private var showRefreshError = false
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
        .searchable(text: $searchText, prompt: "Search by name or address...")
        .onSubmit(of: .search) {
            Task { await fetchWithCurrentFilters() }
        }
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
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
    }

    private var listContent: some View {
        List {
            if store.isFiltered || activeFilterCount > 0 || sort != .newest {
                filterSummary
            }

            ForEach(displayedListings) { listing in
                NavigationLink(value: ListingRoute.known(listing)) {
                    ListingRow(listing: listing)
                        .onAppear {
                            if listing.id == displayedListings.last?.id {
                                Task { await store.loadMore() }
                            }
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

    private var displayedListings: [Listing] {
        store.listings.sorted(using: sort)
    }

    private var activeFilterCount: Int {
        ([selectedStatus].filter { !$0.isEmpty }.count
         + (selectedCities.isEmpty ? 0 : 1)
         + (selectedTypes.isEmpty ? 0 : 1)
         + (selectedContract.isEmpty ? 0 : 1)
         + (selectedEnergy.isEmpty ? 0 : 1))
    }

    private var filterButtonTitle: String {
        activeFilterCount > 0 ? "Filters (\(activeFilterCount))" : "Filters"
    }

    private var filterSummary: some View {
        Section {
            HStack(spacing: 8) {
                Label(summaryText, systemImage: "line.3.horizontal.decrease.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                if activeFilterCount > 0 || !searchText.isEmpty {
                    Button("Clear") {
                        selectedStatus = ""
                        selectedCities = []
                        selectedTypes = []
                        selectedContract = ""
                        selectedEnergy = ""
                        searchText = ""
                        Task { await fetchWithCurrentFilters() }
                    }
                    .font(.caption)
                }
            }
        }
        .listRowBackground(Color.clear)
    }

    private var summaryText: String {
        var parts: [String] = []
        if !selectedCities.isEmpty {
            parts.append(selectedCities.prefix(2).joined(separator: ", ")
                + (selectedCities.count > 2 ? "…" : ""))
        }
        if !selectedStatus.isEmpty { parts.append(selectedStatus) }
        if !selectedTypes.isEmpty {
            parts.append(selectedTypes.prefix(2).joined(separator: ", ")
                + (selectedTypes.count > 2 ? "…" : ""))
        }
        if !selectedContract.isEmpty { parts.append(selectedContract) }
        if !selectedEnergy.isEmpty { parts.append("Energy ≥ \(selectedEnergy)") }
        if sort != .newest { parts.append("Sorted by \(sort.title.lowercased())") }
        if parts.isEmpty { parts.append("Filtered results") }
        return parts.joined(separator: " · ")
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
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Apply", action: apply)
                }
            }
            .task {
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
            print("[ListingFilterSheet] loadOptions error: \(error)")
        }
    }
}
