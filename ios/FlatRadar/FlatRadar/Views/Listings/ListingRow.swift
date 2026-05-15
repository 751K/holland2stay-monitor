import SwiftUI

struct ListingRow: View {
    let listing: Listing

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(listing.name)
                    .font(.headline)
                    .lineLimit(1)
                HStack {
                    Text(listing.city)
                    if let price = listing.priceRaw {
                        Text("·")
                        Text(price)
                    }
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
            }

            Spacer()

            Text(listing.status)
                .font(.caption)
                .fontWeight(.medium)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(statusColor.opacity(0.15))
                .foregroundStyle(statusColor)
                .clipShape(Capsule())
        }
        .padding(.vertical, 4)
    }

    private var statusColor: Color {
        let s = listing.status.lowercased()
        if s.contains("available to book") { return .green }
        if s.contains("lottery") { return .orange }
        return .secondary
    }
}
