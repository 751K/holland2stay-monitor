import SwiftUI

struct ContentView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(NavigationCoordinator.self) private var coord
    @AppStorage("terms_accepted") private var termsAccepted = false
    @AppStorage("onboarding_completed") private var onboardingCompleted = false
    /// 用户在崩溃报告 alert 里点过"Don't Ask Again"后置 true；不再弹窗。
    /// 想恢复请去 Settings 重置（暂未提供 UI；将来可加）。
    @AppStorage("crash_prompt_suppressed") private var crashPromptSuppressed = false
    @State private var showTerms = false
    @State private var showOnboarding = false
    @State private var showSaveBiometric = false
    @State private var showCrashPrompt = false
    @State private var pendingCrashCount = 0
    @State private var crashPromptUploading = false

    var body: some View {
        Group {
            if auth.isAuthenticated {
                MainTabView()
                    .transition(.opacity)
            } else {
                LoginView()
                    .transition(.opacity)
            }
        }
        .animation(.easeInOut(duration: 0.3), value: auth.isAuthenticated)
        .onAppear {
            showTerms = !termsAccepted
            applyScreenshotLaunchArgs()
        }
        .sheet(isPresented: $showTerms) {
            TermsAgreementView {
                termsAccepted = true
                showTerms = false
            }
            .interactiveDismissDisabled()
        }
        .sheet(isPresented: $showOnboarding) {
            OnboardingView {
                onboardingCompleted = true
                showOnboarding = false
            }
            .interactiveDismissDisabled()
        }
        .onChange(of: termsAccepted) { _, new in
            if new, auth.isAuthenticated, !onboardingCompleted {
                showOnboarding = true
            }
        }
        .onChange(of: auth.isAuthenticated) { _, new in
            if new, termsAccepted, !onboardingCompleted {
                showOnboarding = true
            }
            // Face ID 保存提示——LoginView 登录成功后会立即被
            // MainTabView 替换，alert 不能放 LoginView 层级。
            // 只有 user 展示 Face ID 保存提示；guest 和 admin 跳过。
            if new, auth.isUser, auth.pendingBiometricCredential != nil {
                showSaveBiometric = true
            }
        }
        .alert("Save for \(BiometricAuthService.biometryName)?", isPresented: $showSaveBiometric) {
            Button("Save") {
                if let c = auth.pendingBiometricCredential {
                    try? BiometricAuthService.saveCredentials(
                        .init(username: c.username, password: c.password, role: c.role))
                }
                auth.pendingBiometricCredential = nil
            }
            Button("Not Now", role: .cancel) {
                auth.pendingBiometricCredential = nil
            }
        } message: {
            Text("Next time, sign in instantly with \(BiometricAuthService.biometryName) instead of typing your password.")
        }
        // 崩溃诊断上传 prompt：
        // - 用户已通过条款且已登录后才弹（避免对没接触过 app 的人显得突兀）
        // - MetricKit 在 didReceive 写盘后会发 pendingChangedNotification，
        //   首次启动后短期内（系统调度时机）触发，所以也监听该 notification
        //   以便用户在使用过程中收到新报告时也能看到
        .alert(
            pendingCrashCount > 1
                ? "Send \(pendingCrashCount) crash reports?"
                : "Send crash report?",
            isPresented: $showCrashPrompt
        ) {
            Button("Send", role: nil) {
                Task { await uploadPendingCrashReports() }
            }
            .disabled(crashPromptUploading)
            Button("Not Now", role: .cancel) {
                CrashDiagnosticsCollector.shared.markAllDeclined()
                pendingCrashCount = 0
            }
            Button("Don't Ask Again", role: .destructive) {
                CrashDiagnosticsCollector.shared.markAllDeclined()
                crashPromptSuppressed = true
                pendingCrashCount = 0
            }
        } message: {
            Text("FlatRadar didn't run smoothly recently. Sending the diagnostic report helps fix the issue.\n\nWhat's included: crash stack trace, app version, device model, iOS version. No personal info or app data.")
        }
        // 监听 pending 变化（MetricKit 异步写盘 / 用户操作完成后会 post）
        .onReceive(NotificationCenter.default.publisher(
            for: CrashDiagnosticsCollector.pendingChangedNotification)) { _ in
            refreshCrashPrompt()
        }
        // App 启动后第一次进 ContentView + auth/terms 变化时也检查一次
        .task {
            refreshCrashPrompt()
        }
        .onChange(of: auth.isAuthenticated) { _, _ in refreshCrashPrompt() }
        .onChange(of: termsAccepted) { _, _ in refreshCrashPrompt() }
    }

    /// 是否满足"现在该弹崩溃报告 alert"的全部前置条件。
    private func refreshCrashPrompt() {
        // 条款没接受 → 还在条款 sheet 里，不打扰
        // 没认证（包括 guest 的"已 authenticated 但 isGuest"）→ 也可以弹，
        //   崩溃报告是匿名上传，guest 同样适用
        // suppress 已开 → 永远不弹
        guard termsAccepted, !crashPromptSuppressed else {
            showCrashPrompt = false
            return
        }
        let count = CrashDiagnosticsCollector.shared.pendingDiagnostics().count
        pendingCrashCount = count
        showCrashPrompt = count > 0
    }

    /// 依次上传所有 pending 报告。上传成功的物理删除，失败的留盘下次再试。
    /// 不在 .task 直接做（与 alert 解耦）：用户可能想 "Not Now"，那时不该跑网络。
    private func uploadPendingCrashReports() async {
        crashPromptUploading = true
        defer { crashPromptUploading = false }

        let pending = CrashDiagnosticsCollector.shared.pendingDiagnostics()
        for d in pending {
            guard let data = CrashDiagnosticsCollector.shared.readPayload(at: d.url) else {
                continue
            }
            // 把 MetricKit JSON 解为字典再塞进 envelope
            let payloadDict: [String: Any]
            if let obj = try? JSONSerialization.jsonObject(with: data),
               let dict = obj as? [String: Any] {
                payloadDict = dict
            } else {
                // 解析不出来就当 base64 字符串传，后端仍可归档
                payloadDict = ["raw_base64": data.base64EncodedString()]
            }
            do {
                try await APIClient.shared.uploadCrashDiagnostic(
                    kind: d.kind, payload: payloadDict)
                CrashDiagnosticsCollector.shared.markUploaded(d)
            } catch {
                #if DEBUG
                print("[ContentView] 上传崩溃报告失败 \(d.id): \(error)")
                #endif
                // 失败不动文件，下次启动还能再尝试（除非用户改主意拒绝）
            }
        }
        pendingCrashCount = CrashDiagnosticsCollector.shared.pendingDiagnostics().count
    }

    // MARK: - Screenshot mode launch args

    /// 截图自动化的 launch arg 处理（生产 build 永远不会进这里）。
    ///
    /// 支持的参数：
    /// - ``UI_TEST_SCREENSHOT_MODE``  → 启用截图模式；下面的开关只在此前提下生效
    /// - ``UI_TEST_SHOW_LOGIN``       → 不自动 guest，让 LoginView 可被截图
    /// - ``UI_TEST_TAB=<name>``       → 启动直达指定 tab
    ///                                  (dashboard / browse / listings / map / calendar / notifications / settings)
    /// - ``UI_TEST_BROWSE_MODE=<m>``  → Browse tab 初始 mode (list / map / calendar)
    ///                                  绕开 menu UI 不稳定性
    private func applyScreenshotLaunchArgs() {
        let args = CommandLine.arguments
        guard args.contains("UI_TEST_SCREENSHOT_MODE") else { return }

        // 1. 自动 guest（默认开；UI_TEST_SHOW_LOGIN 跳过让登录页可被截）
        if !args.contains("UI_TEST_SHOW_LOGIN"), !auth.isAuthenticated {
            auth.enterAsGuest()
        }

        // 2. 初始 tab
        if let tab = argValue("UI_TEST_TAB", in: args) {
            switch tab.lowercased() {
            case "dashboard":     coord.selectedTab = .dashboard
            case "browse":        coord.selectedTab = .browse
            case "listings":      coord.selectedTab = .listings
            case "map":           coord.selectedTab = .map
            case "calendar":      coord.selectedTab = .calendar
            case "notifications": coord.selectedTab = .notifications
            case "settings":      coord.selectedTab = .settings
            default: break
            }
        }

        // 3. Browse 子 mode（iPhone 上从 menu 切换不稳定，靠这里直设）
        if let mode = argValue("UI_TEST_BROWSE_MODE", in: args) {
            switch mode.lowercased() {
            case "list":     coord.selectedBrowseMode = .list
            case "map":      coord.selectedBrowseMode = .map
            case "calendar": coord.selectedBrowseMode = .calendar
            default: break
            }
        }
    }

    /// 解析 ``KEY=VALUE`` 形式的 launch arg（test 端通过 ``app.launchArguments += [...]`` 加进来）。
    private func argValue(_ key: String, in args: [String]) -> String? {
        let prefix = key + "="
        guard let match = args.first(where: { $0.hasPrefix(prefix) }) else { return nil }
        return String(match.dropFirst(prefix.count))
    }
}

// MARK: - Terms agreement sheet (first launch)

private struct TermsAgreementView: View {
    let onAgree: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Before You Continue")
                        .font(.title2.weight(.bold))

                    Text("Please read and accept the Terms of Use to use FlatRadar.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)

                    Divider()

                    Text(termsSummary)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)

                    Divider()

                    VStack(alignment: .leading, spacing: 10) {
                        Text("By continuing, you agree that:")
                            .font(.subheadline.weight(.semibold))

                        bullet("FlatRadar is an unofficial, independent tool. Not affiliated with or endorsed by Holland2Stay.")
                        bullet("You are responsible for complying with Holland2Stay's Terms of Service.")
                        bullet("Listing data may be delayed, incomplete, inaccurate, or change without notice.")
                        bullet("Push notifications are best-effort. Always verify listings on the official website.")
                        bullet("FlatRadar is for personal, non-commercial use only.")
                    }
                }
                .padding()
            }
            .navigationTitle("Terms of Use")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Agree & Continue") {
                        onAgree()
                    }
                    .fontWeight(.semibold)
                }
            }
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text("•").foregroundStyle(.blue)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private var termsSummary: String {
        """
        FlatRadar is an independent, unofficial monitoring tool for Holland2Stay listings. It is not affiliated with, endorsed by, sponsored by, maintained by, or operated by Holland2Stay.

        By using FlatRadar, you acknowledge that you have read and agree to the Terms of Use and Privacy Policy. Full legal terms are available on the login screen.
        """
    }
}
