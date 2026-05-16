import SwiftUI

/// admin role 在 Settings 里点 "Manage Users" 进入。
/// 只读列表 + toggle enabled + delete；新建 / 详细编辑仍在 Web 后台。
struct AdminUsersView: View {
    @Environment(AdminStore.self) private var store
    @State private var pendingDelete: AdminUserSummary?

    var body: some View {
        Group {
            if store.isLoadingUsers && store.users.isEmpty {
                ProgressView().padding(.top, 60)
            } else if let err = store.errorMessage, store.users.isEmpty {
                ContentUnavailableView(
                    "Unable to Load",
                    systemImage: "person.crop.circle.badge.exclamationmark",
                    description: Text(err))
            } else if store.users.isEmpty {
                ContentUnavailableView(
                    "No Users",
                    systemImage: "person.crop.circle.dashed",
                    description: Text("Create users via the web admin panel."))
            } else {
                List {
                    ForEach(store.users) { user in
                        userRow(user)
                            .swipeActions(edge: .trailing) {
                                Button("Delete", role: .destructive) {
                                    pendingDelete = user
                                }
                            }
                    }
                }
                .refreshable { await store.fetchUsers() }
            }
        }
        .navigationTitle("Manage Users")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if store.users.isEmpty { await store.fetchUsers() }
        }
        .confirmationDialog(
            "Delete user?",
            isPresented: Binding(
                get: { pendingDelete != nil },
                set: { if !$0 { pendingDelete = nil } }
            ),
            presenting: pendingDelete
        ) { user in
            Button("Delete \(user.name)", role: .destructive) {
                Task {
                    await store.deleteUser(id: user.id)
                    pendingDelete = nil
                }
            }
            Button("Cancel", role: .cancel) { pendingDelete = nil }
        } message: { user in
            Text("This will remove the user, their notification preferences, and revoke all their App sessions. This cannot be undone.")
                + (user.activeDevices > 0
                    ? Text("\n\nCurrently has \(user.activeDevices) active device session(s).")
                    : Text(""))
        }
    }

    @ViewBuilder
    private func userRow(_ user: AdminUserSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(user.name)
                    .font(.headline)
                    .foregroundStyle(user.enabled ? .primary : .secondary)
                Spacer()
                Toggle("", isOn: Binding(
                    get: { user.enabled },
                    set: { _ in
                        Task { await store.toggleUser(id: user.id) }
                    }
                ))
                .labelsHidden()
                .disabled(store.actionInFlight)
            }

            // Status chips
            HStack(spacing: 6) {
                statusChip(
                    label: user.enabled ? "Active" : "Disabled",
                    color: user.enabled ? .green : .gray)

                if user.channelCount > 0 {
                    statusChip(
                        label: "\(user.channelCount) channel\(user.channelCount == 1 ? "" : "s")",
                        color: .blue)
                }

                if user.autoBookEnabled {
                    statusChip(label: "Auto-book", color: .orange)
                }

                if user.appLoginEnabled {
                    statusChip(
                        label: "App: \(user.activeDevices) device\(user.activeDevices == 1 ? "" : "s")",
                        color: user.activeDevices > 0 ? .blue : .gray)
                }
            }

            if user.filterSummary.filterActive {
                Text(user.filterSummary.compactDescription)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            Text("ID: \(user.id)")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private func statusChip(label: String, color: Color) -> some View {
        Text(label)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }
}
