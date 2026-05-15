import SwiftUI

struct DashboardView: View {
    @Environment(DashboardStore.self) private var store
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @State private var showLogoutConfirm = false

    var body: some View {
        NavigationStack {
            ScrollView {
                if store.isLoading {
                    ProgressView().padding(.top, 60)
                } else if let s = store.summary {
                    LazyVGrid(columns: [
                        GridItem(.flexible()), GridItem(.flexible()),
                    ], spacing: 12) {
                        StatCard(title: "Total Listings", value: s.total.formatted(),
                                 systemImage: "house.fill", color: .blue)
                        StatCard(title: "New (24h)", value: s.new24h.formatted(),
                                 systemImage: "sparkles", color: .green)
                        StatCard(title: "New (7d)", value: s.new7d.formatted(),
                                 systemImage: "calendar", color: .orange)
                        StatCard(title: "Changes (24h)", value: s.changes24h.formatted(),
                                 systemImage: "arrow.triangle.swap", color: .purple)
                    }
                    .padding(.horizontal)

                    // Personalized stats for logged-in users
                    if let me = store.meSummary, auth.isUser {
                        Divider().padding(.horizontal)
                        HStack(spacing: 4) {
                            Image(systemName: "person.fill")
                                .font(.caption)
                            Text("Your Matches")
                                .font(.subheadline)
                                .fontWeight(.semibold)
                            if me.filterActive {
                                Text("(filtered)")
                                    .font(.caption)
                                    .foregroundStyle(.blue)
                            }
                            Spacer()
                        }
                        .padding(.horizontal)
                        .padding(.top, 4)

                        LazyVGrid(columns: [
                            GridItem(.flexible()), GridItem(.flexible()),
                        ], spacing: 12) {
                            StatCard(title: "Matched", value: me.matchedTotal.formatted(),
                                     systemImage: "checkmark.circle", color: .blue)
                            StatCard(title: "Available",
                                     value: me.matchedAvailable?.formatted() ?? "--",
                                     systemImage: "house.circle", color: .green)
                        }
                        .padding(.horizontal)
                    }

                    if !s.lastScrape.isEmpty, s.lastScrape != "--" {
                        HStack {
                            Image(systemName: "clock")
                                .foregroundStyle(.secondary)
                            Text("Last scrape: \(s.lastScrape)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 8)
                    }
                } else if let err = store.errorMessage {
                    ContentUnavailableView(
                        "Unable to Load",
                        systemImage: "wifi.slash",
                        description: Text(err))
                } else {
                    ContentUnavailableView(
                        "No Data",
                        systemImage: "chart.bar",
                        description: Text("Pull to refresh"))
                }
            }
            .refreshable {
                await store.fetchSummary()
                if auth.isUser { await store.fetchMeSummary() }
            }
            .navigationTitle("Dashboard")
            .toolbar {
                ToolbarItem(placement: .automatic) {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(auth.isGuest ? Color.gray : auth.isAdmin ? Color.red : Color.blue)
                            .frame(width: 8, height: 8)
                        Text(auth.role.rawValue.capitalized)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                ToolbarItem(placement: .automatic) {
                    Button {
                        showLogoutConfirm = true
                    } label: {
                        Image(systemName: "rectangle.portrait.and.arrow.right")
                    }
                    .confirmationDialog("Log Out", isPresented: $showLogoutConfirm) {
                        Button("Log Out", role: .destructive) {
                            Task {
                                await push.logout()
                                await auth.logout()
                            }
                        }
                        Button("Cancel", role: .cancel) {}
                    }
                }
            }
            .task {
                await store.fetchSummary()
                if auth.isUser { await store.fetchMeSummary() }
            }
        }
    }
}
