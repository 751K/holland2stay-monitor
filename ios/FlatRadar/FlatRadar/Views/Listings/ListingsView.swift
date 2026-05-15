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
        .searchable(text: $searchText, prompt: "Search by name...")
        .onSubmit(of: .search) {
            Task { await store.fetch(query: searchText.isEmpty ? nil : searchText) }
        }
        .toolbar {
            if store.isFiltered {
                ToolbarItem(placement: .topBarTrailing) {
                    Label("Filtered", systemImage: "line.3.horizontal.decrease.circle.fill")
                        .foregroundStyle(.blue)
                }
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

    private var listContent: some View {
        List {
            ForEach(store.listings) { listing in
                NavigationLink(value: ListingRoute.known(listing)) {
                    ListingRow(listing: listing)
                        .onAppear {
                            if listing.id == store.listings.last?.id {
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
}
