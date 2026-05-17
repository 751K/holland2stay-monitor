import StoreKit
import SwiftUI
import UIKit

struct SettingsView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @Environment(CoffeeStore.self) private var coffee
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
    @State private var showFeedback = false
    @State private var isExporting = false
    @State private var exportString: String?
    @State private var showShareSheet = false

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
                        if auth.isAdmin {
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
                        }
                        if auth.isAdmin {
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
                        Text("User accounts receive push notifications for listings matching their filter. Test push verifies this device session end-to-end.")
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

                        if auth.isUser {
                            Button {
                                Task { await exportData() }
                            } label: {
                                HStack {
                                    if isExporting {
                                        ProgressView().controlSize(.small)
                                        Text("Exporting…").padding(.leading, 8)
                                    } else {
                                        Text("Export My Data")
                                    }
                                }
                            }
                            .disabled(isExporting)
                            .sheet(isPresented: $showShareSheet, onDismiss: { exportString = nil }) {
                                if let str = exportString {
                                    ActivitySheet(activityItems: [str])
                                }
                            }
                        }

                        Button(role: .destructive) {
                            showLogoutConfirm = true
                        } label: {
                            Text("Log Out")
                            .foregroundStyle(.red)
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
                            Button(role: .destructive) {
                                showDeleteConfirm = true
                            } label: {
                                Text("Delete Account")
                                .foregroundStyle(.red)
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
                        Button(role: .destructive) {
                            showLogoutConfirm = true
                        } label: {
                            Text("Sign Out of Guest Mode")
                            .foregroundStyle(.red)
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
                        Text("Terms of Use")
                        .foregroundStyle(.primary)
                    }
                    Button {
                        showLegalPrivacy = true
                    } label: {
                        Text("Privacy Policy")
                        .foregroundStyle(.primary)
                    }
                }

                // 7. Coffee
                Section {
                    if coffee.products.isEmpty && !coffee.isLoading {
                        HStack {
                            Text("Unable to load products")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Retry") {
                                Task { await coffee.loadProducts() }
                            }
                            .font(.subheadline)
                        }
                    } else if coffee.isLoading {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Loading…").foregroundStyle(.secondary).padding(.leading, 8)
                        }
                    } else {
                        ForEach(coffee.products, id: \.id) { product in
                            Button {
                                Task { await coffee.purchase(product) }
                            } label: {
                                HStack {
                                    Text(product.displayName)
                                        .foregroundStyle(.primary)
                                    Spacer()
                                    Text(product.displayPrice)
                                        .fontWeight(.semibold)
                                        .foregroundStyle(.blue)
                                }
                            }
                        }
                    }

                    if let err = coffee.purchaseError {
                        Text(err)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                } header: {
                    Text("Buy me a coffee ☕")
                } footer: {
                    Text("A one-time tip to support development.\nDoes not unlock any features.")
                }

                // 8. About
                Section("About") {
                    HStack {
                        Text("App")
                        Spacer()
                        Text(AppVersion.displayName)
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        showFeedback = true
                    } label: {
                        Text("Send Feedback")
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
            .alert("Thank you! 🙏", isPresented: Binding(
                get: { coffee.showThanks },
                set: { coffee.showThanks = $0 }
            )) {
                Button("You're welcome!") {}
            } message: {
                Text("Your support means a lot.\nEnjoy your \(coffee.thanksMessage)!")
            }
            .sheet(isPresented: $showFilterEdit) {
                FilterEditView()
            }
            .sheet(isPresented: $showLegalTerms) {
                LegalSheetView(title: LegalText.isChineseLocale ? "使用条款" : "Terms of Use",
                               content: LegalText.termsLocalized)
            }
            .sheet(isPresented: $showLegalPrivacy) {
                LegalSheetView(title: LegalText.isChineseLocale ? "隐私政策" : "Privacy Policy",
                               content: LegalText.privacyLocalized)
            }
            .sheet(isPresented: $showFeedback) {
                FeedbackView()
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

    // MARK: - Export

    private func exportData() async {
        isExporting = true
        defer { isExporting = false }
        do {
            let jsonData = try await APIClient.shared.meExport()
            exportString = String(data: jsonData, encoding: .utf8)
            showShareSheet = true
        } catch {
            testResultMessage = error.localizedDescription
            showTestResult = true
        }
    }

}

// MARK: - UIActivityViewController wrapper

struct ActivitySheet: UIViewControllerRepresentable {
    let activityItems: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: activityItems, applicationActivities: nil)
    }

    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
