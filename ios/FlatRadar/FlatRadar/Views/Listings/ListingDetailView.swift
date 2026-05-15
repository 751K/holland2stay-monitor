import SwiftUI

/// Listing 详情页。
///
/// 支持两种打开方式（``ListingRoute``）：
/// - ``known(Listing)``：从列表行点入，data 已在手，立即渲染
/// - ``byId(String)``：从推送通知 deep link 进来，只有 id，``.task`` 拉取
///   ``getListing(id:)`` 再渲染；中间显示 ProgressView
///
/// 加载失败（404 / 网络异常）时用 ContentUnavailableView 兜底。
struct ListingDetailView: View {
    let route: ListingRoute

    @State private var listing: Listing?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if let listing {
                content(listing)
            } else if isLoading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let err = errorMessage {
                ContentUnavailableView(
                    "Listing Not Available",
                    systemImage: "house.slash",
                    description: Text(err))
            } else {
                Color.clear
            }
        }
        .navigationTitle(listing?.name ?? "Loading…")
        .task { await load() }
    }

    private func load() async {
        switch route {
        case .known(let l):
            listing = l
        case .byId(let id):
            guard listing == nil else { return }   // 二次进入不重复 fetch
            isLoading = true
            errorMessage = nil
            do {
                listing = try await APIClient.shared.getListing(id: id)
            } catch {
                errorMessage = error.localizedDescription
            }
            isLoading = false
        }
    }

    @ViewBuilder
    private func content(_ listing: Listing) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                // Header
                VStack(alignment: .leading, spacing: 4) {
                    Text(listing.name)
                        .font(.title2)
                        .fontWeight(.bold)
                    HStack {
                        Text(listing.city)
                        Text("·")
                        Text(listing.status)
                            .foregroundStyle(statusColor(for: listing))
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }

                // Price
                if let price = listing.priceRaw {
                    HStack(alignment: .firstTextBaseline, spacing: 2) {
                        Text(price)
                            .font(.title)
                            .fontWeight(.semibold)
                        Text("/mo")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                }

                if let availableFrom = listing.availableFrom, !availableFrom.isEmpty {
                    LabeledContent("Available from", value: availableFrom)
                }

                Divider()

                // Features
                if !listing.featureMap.isEmpty {
                    Text("Details")
                        .font(.headline)
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                        ForEach(listing.featureMap.sorted(by: { $0.key < $1.key }), id: \.key) { key, value in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(key)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text(value)
                                    .font(.subheadline)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                } else if !listing.features.isEmpty {
                    Text("Features")
                        .font(.headline)
                    ForEach(listing.features, id: \.self) { f in
                        Label(f, systemImage: "checkmark.circle")
                            .font(.subheadline)
                    }
                }

                Divider()

                // Timestamps
                if let first = listing.firstSeen {
                    LabeledContent("First seen", value: first)
                        .font(.caption)
                }
                if let last = listing.lastSeen {
                    LabeledContent("Last seen", value: last)
                        .font(.caption)
                }

                // Open in browser
                if let url = URL(string: listing.url), !listing.url.isEmpty {
                    Link(destination: url) {
                        Label("Open on Holland2Stay", systemImage: "safari")
                    }
                    .padding(.top, 8)
                }
            }
            .padding()
        }
    }

    private func statusColor(for listing: Listing) -> Color {
        let s = listing.status.lowercased()
        if s.contains("available") { return .green }
        if s.contains("lottery") { return .orange }
        return .secondary
    }
}
