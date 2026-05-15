import SwiftUI

struct ListingsView: View {
    @Environment(ListingsStore.self) private var store
    @Environment(NavigationCoordinator.self) private var coord
    @State private var searchText = ""
    @State private var showFilters = false

    var body: some View {
        @Bindable var coord = coord

        NavigationStack(path: $coord.listingsPath) {
            Group {
                if store.isLoading && store.listings.isEmpty {
                    ProgressView().padding(.top, 60)
                } else if let err = store.errorMessage, store.listings.isEmpty {
                    ContentUnavailableView(
                        "Unable to Load",
                        systemImage: "wifi.slash",
                        description: Text(err))
                    .refreshable { await store.refresh() }
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
            .navigationTitle("Listings")
            .searchable(text: $searchText, prompt: "Search by name...")
            .onSubmit(of: .search) {
                Task { await store.fetch(query: searchText.isEmpty ? nil : searchText) }
            }
            .toolbar {
                ToolbarItem(placement: .automatic) {
                    if store.isFiltered {
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
            // 统一路由：列表点击 + deep link push 都走 ListingRoute → ListingDetailView
            .navigationDestination(for: ListingRoute.self) { route in
                ListingDetailView(route: route)
            }
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
