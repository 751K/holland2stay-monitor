import SwiftUI

struct SettingsView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @AppStorage("server_url") private var serverURL: String = APIClient.defaultServerHost
    @AppStorage("color_scheme") private var colorScheme: String = "system"
    @State private var editedURL = ""
    @State private var showLogoutConfirm = false

    // Test push 状态
    @State private var isSendingTest = false
    @State private var testResultMessage: String?
    @State private var showTestResult = false

    var body: some View {
        NavigationStack {
            Form {
                // Server
                Section {
                    HStack {
                        TextField("host:port", text: $editedURL)
#if os(iOS)
                            .keyboardType(.URL)
#endif
                            .autocorrectionDisabled()
#if os(iOS)
                            .textInputAutocapitalization(.never)
#endif
                        Button("Save") {
                            serverURL = editedURL.trimmingCharacters(in: .whitespaces)
                            let url = buildBaseURL(from: serverURL)
                            APIClient.shared.configure(baseURL: url)
                            endEditing()
                        }
                        .buttonStyle(.bordered)
                        .disabled(editedURL.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } header: {
                    Text("Server")
                } footer: {
                    Text("Enter the host:port of your FlatRadar server.\nHTTPS is enforced for production.")
                }

                // Account
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
                                    // 先解绑设备（用旧 token 调）再撤销 token
                                    await push.logout()
                                    await auth.logout()
                                }
                            }
                            Button("Cancel", role: .cancel) {}
                        }
                    }

                    // Push notifications
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
                    } header: {
                        Text("Push Notifications")
                    } footer: {
                        Text("Sends a test alert to all devices registered under this session. Verifies APNs end-to-end.")
                    }
                } else if auth.isGuest {
                    Section("Account") {
                        HStack {
                            Text("Role")
                            Spacer()
                            Text("Guest")
                                .foregroundStyle(.secondary)
                        }
                        // guest 没 token，logout 不调服务端 revoke；只是清本地
                        // 状态、回到登录页，让用户能登 admin/user。
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

                // Appearance
                Section("Appearance") {
                    Picker("Color Scheme", selection: $colorScheme) {
                        Text("System").tag("system")
                        Text("Light").tag("light")
                        Text("Dark").tag("dark")
                    }
                }

                // About
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
