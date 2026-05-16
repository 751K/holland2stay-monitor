import SwiftUI

struct SettingsView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @AppStorage("server_url") private var serverURL: String = APIClient.defaultServerHost
    @AppStorage("color_scheme") private var colorScheme: String = "system"
    @State private var editedURL = ""
    @State private var showLogoutConfirm = false
    @State private var showDeleteConfirm = false

    // Test push 状态
    @State private var isSendingTest = false
    @State private var testResultMessage: String?
    @State private var showTestResult = false

    // Filter 编辑 sheet
    @State private var showFilterEdit = false
    @State private var showLegalTerms = false
    @State private var showLegalPrivacy = false

    var body: some View {
        NavigationStack {
            Form {
                // 1. Push Filter (user only)
                if auth.isUser, let info = auth.userInfo {
                    Section {
                        Button {
                            showFilterEdit = true
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Label("Notification Filter",
                                          systemImage: "line.3.horizontal.decrease.circle.fill")
                                        .foregroundStyle(.primary)
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption)
                                        .foregroundStyle(.tertiary)
                                }
                                Text(info.listingFilter.summary)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        .buttonStyle(.plain)
                    } header: {
                        Text("Push Filter")
                    } footer: {
                        Text("Only listings matching this filter trigger APNs and notification tab updates.")
                    }
                }

                // 2. Appearance
                Section("Appearance") {
                    Picker("Color Scheme", selection: $colorScheme) {
                        Text("System").tag("system")
                        Text("Light").tag("light")
                        Text("Dark").tag("dark")
                    }
                }

                // 3. Push Notifications (authenticated, non-guest)
                if auth.isAuthenticated, auth.role != .guest {
                    Section {
                        HStack {
                            Text("Permission")
                            Spacer()
                            Text(pushPermissionLabel)
                                .foregroundStyle(pushPermissionColor)
                                .font(.subheadline)
                        }
                        HStack {
                            Text("Device ID")
                            Spacer()
                            if let id = push.registeredDeviceId {
                                Text("\(id)").foregroundStyle(.secondary)
                            } else {
                                Text("not registered").foregroundStyle(.secondary)
                            }
                        }
                        if auth.isAdmin {
                            if let err = push.lastError {
                                VStack(alignment: .leading, spacing: 4) {
                                    Label("Registration failed", systemImage: "exclamationmark.triangle")
                                        .foregroundStyle(.red)
                                        .font(.subheadline)
                                    Text(err)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Button {
                                sendTestPush()
                            } label: {
                                HStack {
                                    if isSendingTest {
                                        ProgressView().controlSize(.small)
                                    }
                                    Text(isSendingTest ? "Sending…" : "Send Test Push")
                                }
                            }
                            .disabled(isSendingTest || push.registeredDeviceId == nil)
                            Button {
                                Task { await push.requestPermissionAndRegister() }
                            } label: {
                                Text("Re-register Device")
                            }
                            .disabled(push.permissionStatus == .denied)
                        }
                    } header: {
                        Text("Push Notifications")
                    } footer: {
                        if auth.isAdmin {
                            Text("Sends a test alert to all devices registered under this session. Verifies APNs end-to-end.")
                        } else {
                            Text("New listings matching your filter will arrive as push notifications.")
                        }
                    }
                }

                // 4. Account
                if auth.isAuthenticated, auth.role != .guest {
                    Section("Account") {
                        HStack {
                            Text("Role")
                            Spacer()
                            Text(auth.role.rawValue.capitalized)
                                .foregroundStyle(.secondary)
                        }
                        if let user = auth.userInfo {
                            HStack {
                                Text("Name")
                                Spacer()
                                Text(user.name)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Button("Log Out", role: .destructive) {
                            showLogoutConfirm = true
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

                        if auth.isUser {
                            Button("Delete Account", role: .destructive) {
                                showDeleteConfirm = true
                            }
                            .confirmationDialog(
                                "Permanently delete your account?",
                                isPresented: $showDeleteConfirm,
                                titleVisibility: .visible
                            ) {
                                Button("Delete Account", role: .destructive) {
                                    Task {
                                        await push.logout()
                                        await auth.deleteAccount()
                                    }
                                }
                                Button("Cancel", role: .cancel) {}
                            } message: {
                                Text("Your account data, saved filters, alert history, and preferences will be permanently removed. This cannot be undone.")
                            }
                        }
                    }
                } else if auth.isGuest {
                    Section("Account") {
                        HStack {
                            Text("Role")
                            Spacer()
                            Text("Guest")
                                .foregroundStyle(.secondary)
                        }
                        Button("Sign Out of Guest Mode", role: .destructive) {
                            showLogoutConfirm = true
                        }
                        .confirmationDialog("Sign Out", isPresented: $showLogoutConfirm) {
                            Button("Sign Out", role: .destructive) {
                                Task { await auth.logout() }
                            }
                            Button("Cancel", role: .cancel) {}
                        }
                    }
                }

                // Admin tools
                if auth.isAdmin {
                    Section {
                        NavigationLink {
                            AdminUsersView()
                        } label: {
                            Label("Manage Users", systemImage: "person.2.fill")
                        }
                        NavigationLink {
                            AdminMonitorView()
                        } label: {
                            Label("Monitor Control", systemImage: "gauge.with.dots.needle.50percent")
                        }
                    } header: {
                        Text("Admin")
                    } footer: {
                        Text("Toggle users on/off, delete accounts, and control the scraping process.")
                    }
                }

                // 6. Legal
                Section("Legal") {
                    Button {
                        showLegalTerms = true
                    } label: {
                        Label("Terms of Use", systemImage: "doc.text")
                            .foregroundStyle(.primary)
                    }
                    Button {
                        showLegalPrivacy = true
                    } label: {
                        Label("Privacy Policy", systemImage: "hand.raised")
                            .foregroundStyle(.primary)
                    }
                }

                // 7. About
                Section("About") {
                    HStack {
                        Text("App")
                        Spacer()
                        Text("FlatRadar v1.0")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Settings")
            .onAppear { editedURL = serverURL }
            .alert("Test Push", isPresented: $showTestResult, presenting: testResultMessage) { _ in
                Button("OK", role: .cancel) {}
            } message: { msg in
                Text(msg)
            }
            .sheet(isPresented: $showFilterEdit) {
                FilterEditView()
            }
            .sheet(isPresented: $showLegalTerms) {
                LegalSheetView(title: "Terms of Use", content: LegalText.terms)
            }
            .sheet(isPresented: $showLegalPrivacy) {
                LegalSheetView(title: "Privacy Policy", content: LegalText.privacy)
            }
        }
    }

    // MARK: - Push permission UI helpers

    private var pushPermissionLabel: String {
        switch push.permissionStatus {
        case .authorized:    return String(localized: "Authorized")
        case .provisional:   return String(localized: "Provisional")
        case .ephemeral:     return String(localized: "Ephemeral")
        case .denied:        return String(localized: "Denied")
        case .notDetermined: return String(localized: "Not determined")
        }
    }

    private var pushPermissionColor: Color {
        switch push.permissionStatus {
        case .authorized, .provisional, .ephemeral: return .green
        case .denied: return .red
        case .notDetermined: return .secondary
        }
    }

    // MARK: - Test push action

    private func sendTestPush() {
        isSendingTest = true
        Task {
            defer { isSendingTest = false }
            do {
                let r = try await APIClient.shared.testPush()
                if r.sent == r.total {
                    testResultMessage = String(localized: "✅ Sent to \(r.sent) device\(r.sent == 1 ? "" : "s"). Check your lock screen.")
                } else {
                    let failedReasons = r.results
                        .filter { !$0.ok }
                        .map { "\($0.status) \($0.reason)" }
                        .joined(separator: "; ")
                    testResultMessage = String(localized: "⚠️ Sent \(r.sent)/\(r.total). Failures: \(failedReasons)")
                }
            } catch {
                testResultMessage = String(localized: "❌ \(error.localizedDescription)")
            }
            showTestResult = true
        }
    }

    private func buildBaseURL(from host: String) -> URL {
        let clean = host.trimmingCharacters(in: ["/", " "])
        let scheme = clean.hasPrefix("localhost") || clean.hasPrefix("127.")
            ? "http" : "https"
        return URL(string: "\(scheme)://\(clean)")!
    }

    private func endEditing() {
#if os(iOS)
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
#endif
    }
}
