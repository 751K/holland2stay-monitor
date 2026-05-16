import SwiftUI

struct NotificationRow: View {
    let notification: NotificationItem

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: iconName)
                .frame(width: 28)
                .foregroundStyle(iconColor)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(notification.title)
                        .font(.headline)
                    if !notification.isRead {
                        Circle()
                            .fill(.blue)
                            .frame(width: 8, height: 8)
                    }
                }
                Text(notification.body)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)

                Text(ServerTime.display(notification.createdAt))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 4)
    }

    private var iconName: String {
        switch notification.type {
        case "new_listing":   return "house.fill"
        case "status_change": return "arrow.triangle.swap"
        case "booking":       return "cart.fill"
        case "error":         return "exclamationmark.triangle.fill"
        case "blocked":       return "hand.raised.fill"
        default:              return "bell.fill"
        }
    }

    private var iconColor: Color {
        switch notification.type {
        case "new_listing":   return .green
        case "status_change": return .orange
        case "booking":       return .blue
        case "error":         return .red
        case "blocked":       return .red
        default:              return .secondary
        }
    }
}
