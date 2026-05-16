import SwiftUI

struct ListingRow: View {
    let listing: Listing

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(listing.name)
                    .font(.headline)
                    .lineLimit(2)

                HStack(spacing: 6) {
                    Text(listing.city)
                    if let price = listing.priceRaw {
                        Text("·")
                        Text(price)
                    }
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)

                HStack(spacing: 8) {
                    if let area = listing.areaText {
                        Text(area)
                    }
                    if let available = listing.availableDayKey {
                        Text(ServerTime.displayDate(available))
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            Spacer()

            Text(statusLabel)
                .font(.caption)
                .fontWeight(.medium)
                .frame(width: 52)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(statusColor.opacity(0.15))
                .foregroundStyle(statusColor)
                .clipShape(Capsule())
                .fixedSize(horizontal: true, vertical: false)
        }
        .padding(.vertical, 4)
    }

    private var statusLabel: String {
        let s = listing.status.lowercased()
        if s.contains("available to book") { return "Book" }
        if s.contains("lottery") { return "Lottery" }
        return listing.status
    }

    private var statusColor: Color {
        let s = listing.status.lowercased()
        if s.contains("available to book") { return .green }
        if s.contains("lottery") { return .orange }
        return .secondary
    }
}
