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
    @State private var showRemoveBiometric = false
    @State private var showEnableBiometric = false
    @State private var showGuestRegister = false
    @State private var isExporting = false
    @State private var exportString: String?
    @State private var showShareSheet = false
    // 修改密码 sheet 状态
    @State private var showChangePassword = false

    var body: some View {
        NavigationStack {
            Form {
                // 1. Push Filter (user only)
                if auth.isUser, let info = auth.userInfo {
                    Section {
                        Button {
                            showFilterEdit = true
                        } label: {
                            filterSummaryRow(info.listingFilter)
                        }
                        .buttonStyle(.plain)
                    } header: {
                        Text("Notification Filter")
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
                        if auth.isUser {
                            // User: 一个开关，开启/关闭通知
                            Toggle("Enable Notifications", isOn: Binding(
                                get: { push.registeredDeviceId != nil },
                                set: { enable in
                                    Task { await push.setEnabled(enable) }
                                }
                            ))
                            .disabled(push.permissionStatus == .denied)
                            if push.permissionStatus == .denied {
                                Text("Notifications are disabled in iOS Settings. Enable them in Settings → Notifications → FlatRadar.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } else {
                            // Admin: 保留 permission / device id 诊断信息
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
                        if auth.isUser {
                            Text("Receive push notifications for new listings matching your filter.")
                        } else {
                            Text("User accounts receive push notifications for listings matching their filter. Test push verifies this device session end-to-end.")
                        }
                    }
                }

                // 4. Account
                if auth.isAuthenticated, auth.role != .guest {
                    Section("Account") {
                        if auth.isUser, BiometricAuthService.isAvailable {
                            let name = BiometricAuthService.biometryName
                            if BiometricAuthService.hasStoredCredentials {
                                Toggle("Sign in with \(name)", isOn: Binding(
                                    get: { true },
                                    set: { enable in
                                        if !enable { showRemoveBiometric = true }
                                    }
                                ))
                            } else {
                                // 这里**不能**是开关。启用要把明文密码写进 Keychain，
                                // 而设置页手里只有 token——密码只在登录那一刻存在，
                                // 所以得让用户重新输一次（见 EnableBiometricSheet）。
                                //
                                // 之前这里是个开关，set 闭包只处理 enable == false：
                                // 往开的方向拨，什么都不写，get 再读一次仍是 false，
                                // 开关弹回原位，没有任何提示。
                                Button {
                                    showEnableBiometric = true
                                } label: {
                                    HStack {
                                        Text("Sign in with \(name)")
                                            .foregroundStyle(.primary)
                                        Spacer()
                                        Text("Set Up")
                                            .foregroundStyle(Color.accentColor)
                                    }
                                    .contentShape(.rect)
                                }
                                .buttonStyle(.plain)
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

                        // 修改密码：仅 user 可用。admin 密码在 .env 不走 API。
                        if auth.isUser {
                            Button {
                                showChangePassword = true
                            } label: {
                                Text("Change Password")
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
                        // 注册入口。访客是纯本地状态，手里没有 token，此前要变成
                        // 真账号只能先「退出访客模式」回登录页——而那个按钮看起来
                        // 像是要把人赶出去，没人会为了注册去点它。
                        Button {
                            showGuestRegister = true
                        } label: {
                            HStack {
                                Text("Create an Account")
                                    .foregroundStyle(.primary)
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .font(.caption).foregroundStyle(.tertiary)
                            }
                            .contentShape(.rect)
                        }
                        .buttonStyle(.plain)

                        // 不再显示 "Role: Guest" —— 底下那个按钮就叫
                        // "Sign Out of Guest Mode"，已经把身份说清楚了。
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

                // 6. Legal — admin 是后端运维者，自己维护条款 / 隐私政策文本，
                // 设置里再放一遍是噪音；只对普通用户 / guest 展示
                if !auth.isAdmin {
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
                }

                // 7. Coffee — admin 是后端运维者，不展示打赏/反馈入口
                // （admin 自己维护项目，给自己买咖啡 + 给自己发反馈都没意义）
                if !auth.isAdmin {
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
                        // 用 SF Symbol 替代 ☕ emoji——iOS HIG 推荐 UI chrome 用
                        // SF Symbol，跟系统字体度量、字重一致，且 VoiceOver 会朗读
                        // "cup and saucer"语义而非泛指 emoji。
                        Text("Buy me a coffee")
                    } footer: {
                        Text("A one-time tip to support development.\nDoes not unlock any features.")
                    }
                }

                // 8. About — admin 隐藏 Send Feedback，仅保留版本号
                Section("About") {
                    HStack {
                        Text("App")
                        Spacer()
                        Text(AppVersion.displayName)
                            .foregroundStyle(.secondary)
                    }
                    if !auth.isAdmin {
                        Button {
                            showFeedback = true
                        } label: {
                            Text("Send Feedback")
                        }
                    }
                }
            }
            .navigationTitle("Settings")
            .onAppear { editedURL = serverURL }
            .sheet(isPresented: $showEnableBiometric) {
                EnableBiometricSheet()
            }
            .sheet(isPresented: $showGuestRegister) {
                RegisterAccountSheet()
            }
            .confirmationDialog("Remove Face ID Sign-In?", isPresented: $showRemoveBiometric, titleVisibility: .visible) {
                Button("Remove", role: .destructive) {
                    BiometricAuthService.deleteCredentials()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("You can enable it again next time you sign in with your password.")
            }
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
                               kind: "terms")
            }
            .sheet(isPresented: $showLegalPrivacy) {
                LegalSheetView(title: LegalText.isChineseLocale ? "隐私政策" : "Privacy Policy",
                               kind: "privacy")
            }
            .sheet(isPresented: $showFeedback) {
                FeedbackView()
            }
            .sheet(isPresented: $showChangePassword) {
                ChangePasswordSheet()
            }
        }
    }

    // MARK: - Push permission UI helpers

    /// 筛选条件摘要 —— 一枚条件一枚 chip，装不下就折行。
    ///
    /// 之前是一整行用 `·` 连起来的文字。七条条件连成一句
    /// "≤ €1400/mo · ≥ 20 m² · Amsterdam, Diemen · H2S, OC, XR, MG, PZ, SE ·
    /// Tenant: Student only, Student and empl…"，三行还是被截断，而且价格、
    /// 面积、城市、平台、租客五种完全不同的条件长得一模一样，只能逐字读。
    ///
    /// 拆成 chip 之后边界自己就分开了，折行也不会截在词中间；平台复用
    /// ``PlatformBadge`` 的彩色徽章，那套颜色在地图和房源页已经用熟了。
    @ViewBuilder
    private func filterSummaryRow(_ filter: ListingFilter) -> some View {
        HStack(spacing: 10) {
            if filter.isEmpty {
                Text("No conditions — every new listing notifies you.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(Array(filter.summaryChips.enumerated()), id: \.offset) { _, chip in
                        summaryChip(chip)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Image(systemName: "chevron.right")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func summaryChip(_ chip: SummaryChip) -> some View {
        switch chip {
        case .platform(let source):
            PlatformBadge(source: source, size: .medium)
        case .platformCount(let n):
            neutralChip(Text("\(n) platforms"))
        case .text(let value):
            neutralChip(Text(verbatim: value))
        }
    }

    private func neutralChip(_ label: Text) -> some View {
        label
            .font(.caption)
            .foregroundStyle(.primary)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Color.secondary.opacity(0.14), in: Capsule())
    }

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

// MARK: - Change Password sheet

/// 用户改密码 sheet。
///
/// 设计要点
/// - 三栏：当前密码 / 新密码 / 确认新密码；全部 SecureField
/// - 客户端基础校验：新密码 ≥ 4、两次一致、与当前不同——通过才允许 Submit
/// - 失败时不清空表单，方便用户改一处重提交
/// - 成功 → 关闭 sheet；其他设备会话已被后端撤销，提示用户
private struct ChangePasswordSheet: View {
    @Environment(AuthStore.self) private var auth
    @Environment(\.dismiss) private var dismiss

    @State private var currentPw = ""
    @State private var newPw = ""
    @State private var confirmPw = ""
    @State private var inlineError: String?
    @State private var showSuccess = false
    @FocusState private var focused: Field?

    private enum Field { case current, new, confirm }

    /// 客户端校验：长度 / 一致 / 与当前不同。
    /// 服务端会重做这些校验，这里只为 UX 即时反馈。
    private var clientValidationError: String? {
        if currentPw.isEmpty || newPw.isEmpty || confirmPw.isEmpty {
            return nil   // 还没填完不报错
        }
        if newPw.count < 4 {
            return String(localized: "New password must be at least 4 characters.")
        }
        if newPw != confirmPw {
            return String(localized: "New passwords don't match.")
        }
        if newPw == currentPw {
            return String(localized: "New password must differ from current.")
        }
        return nil
    }

    private var canSubmit: Bool {
        !currentPw.isEmpty && !newPw.isEmpty && !confirmPw.isEmpty
            && clientValidationError == nil && !auth.isLoading
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Current password", text: $currentPw)
                        .textContentType(.password)
                        .focused($focused, equals: .current)
                        .submitLabel(.next)
                        .onSubmit { focused = .new }
                    SecureField("New password", text: $newPw)
                        .textContentType(.newPassword)
                        .focused($focused, equals: .new)
                        .submitLabel(.next)
                        .onSubmit { focused = .confirm }
                    SecureField("Confirm new password", text: $confirmPw)
                        .textContentType(.newPassword)
                        .focused($focused, equals: .confirm)
                        .submitLabel(.go)
                        .onSubmit { Task { await submit() } }
                } footer: {
                    Text("Other devices signed in to this account will be signed out.")
                }

                // 错误展示：服务端错误优先，客户端校验作 fallback
                if let err = inlineError ?? clientValidationError {
                    Section {
                        Text(err)
                            .font(.callout)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Change Password")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        Task { await submit() }
                    } label: {
                        if auth.isLoading {
                            ProgressView()
                        } else {
                            Text("Update")
                        }
                    }
                    .disabled(!canSubmit)
                }
            }
            .alert("Password Updated", isPresented: $showSuccess) {
                Button("OK") { dismiss() }
            } message: {
                Text("Use the new password next time you sign in. Other devices have been signed out.")
            }
            .onAppear { focused = .current }
        }
    }

    private func submit() async {
        guard canSubmit else { return }
        inlineError = nil
        let ok = await auth.changePassword(current: currentPw, new: newPw)
        if ok {
            showSuccess = true
        } else {
            inlineError = auth.errorMessage
        }
    }
}
