import SwiftUI

/// admin 远程控制监控进程：状态 / Start / Stop / Reload。
struct AdminMonitorView: View {
    @Environment(AdminStore.self) private var store
    @State private var showStopConfirm = false

    var body: some View {
        Form {
            statusSection
            actionsSection
            if let err = store.errorMessage {
                Section {
                    Label(err, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                        .font(.subheadline)
                }
            }
        }
        .navigationTitle("Monitor Control")
        .navigationBarTitleDisplayMode(.inline)
        .task { await store.fetchMonitorStatus() }
        .refreshable { await store.fetchMonitorStatus() }
        .confirmationDialog("Stop monitor process?",
                            isPresented: $showStopConfirm,
                            titleVisibility: .visible) {
            Button("Stop", role: .destructive) {
                Task { await store.stopMonitor() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Scraping and APNs delivery will pause until restarted.")
        }
    }

    private var statusSection: some View {
        Section {
            HStack {
                Circle()
                    .fill(isRunning ? Color.green : Color.gray)
                    .frame(width: 10, height: 10)
                Text(isRunning ? "Running" : "Stopped")
                    .font(.headline)
                Spacer()
                if store.isLoadingMonitor {
                    ProgressView().controlSize(.small)
                } else {
                    Button {
                        Task { await store.fetchMonitorStatus() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                }
            }

            if let s = store.monitorStatus {
                if let pid = s.pid {
                    HStack {
                        Text("PID")
                        Spacer()
                        Text("\(pid)").foregroundStyle(.secondary)
                    }
                }
                if !s.lastScrape.isEmpty, s.lastScrape != "—" {
                    HStack {
                        Text("Last scrape")
                        Spacer()
                        Text(s.lastScrape).font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                if !s.lastCount.isEmpty, s.lastCount != "—" {
                    HStack {
                        Text("Last count")
                        Spacer()
                        Text(s.lastCount).foregroundStyle(.secondary)
                    }
                }
            }
        } header: {
            Text("Status")
        }
    }

    private var actionsSection: some View {
        Section {
            if isRunning {
                Button {
                    Task { await store.reloadMonitor() }
                } label: {
                    Label("Reload Config", systemImage: "arrow.triangle.2.circlepath")
                }
                .disabled(store.actionInFlight)

                Button(role: .destructive) {
                    showStopConfirm = true
                } label: {
                    Label("Stop Monitor", systemImage: "stop.fill")
                }
                .disabled(store.actionInFlight)
            } else {
                Button {
                    Task { await store.startMonitor() }
                } label: {
                    Label("Start Monitor", systemImage: "play.fill")
                }
                .disabled(store.actionInFlight)
            }

            if store.actionInFlight {
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Working…").foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Actions")
        } footer: {
            Text("Reload re-reads users.json and .env without restarting the process. Stop halts scraping; subsequent listings won't trigger push.")
        }
    }

    private var isRunning: Bool { store.monitorStatus?.running ?? false }
}
