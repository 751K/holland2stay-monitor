import SwiftUI

struct SettingsView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @AppStorage("server_url") private var serverURL: String = "127.0.0.1:8088"
    @State private var editedURL = ""
    @State private var showLogoutConfirm = false

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
                            Task { await APIClient.shared.configure(baseURL: url) }
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
                } else if auth.isGuest {
                    Section("Account") {
                        HStack {
                            Text("Role")
                            Spacer()
                            Text("Guest")
                                .foregroundStyle(.secondary)
                        }
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
