import SwiftUI

struct ListingDetailView: View {
    let listing: Listing

    var body: some View {
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
                            .foregroundStyle(statusColor)
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
        .navigationTitle(listing.name)
    }

    private var statusColor: Color {
        let s = listing.status.lowercased()
        if s.contains("available") { return .green }
        if s.contains("lottery") { return .orange }
        return .secondary
    }
}
