import SwiftUI

struct LoginView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @Environment(\.colorScheme) private var colorScheme
    /// "减弱动态效果"：用户在 设置 > 辅助功能 > 动态效果 里开启时为 true。
    /// 受影响的动画（如 hero 图标呼吸）应在此 flag true 时跳过或显著弱化。
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expandedRole: LoginMode?
    @State private var username = ""
    @State private var password = ""
    /// 是否显示密码明文（眼睛图标 toggle）。两套表单（登录卡片 / 注册 sheet）
    /// 各一个，避免互相影响。
    @State private var showPasswordPlain = false
    @State private var showRegPasswordPlain = false
    @State private var liveCount = 0
    @State private var new24h = 0
    @State private var changes24h = 0
    @State private var lastScrapeAt: Date?
    @State private var breathe = false
    @State private var showTerms = false
    @State private var showPrivacy = false
    @State private var showRegister = false
    @State private var regUsername = ""
    @State private var regPassword = ""
    @State private var isAuthenticatingBiometric = false
    @State private var showSaveBiometricPrompt = false
    @State private var pendingSaveCredential: (username: String, password: String)?

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private var appVersion: String {
        AppVersion.short
    }

    private var timeAgo: String {
        guard let date = lastScrapeAt else { return "--" }
        let secs = max(0, Int(Date().timeIntervalSince(date)))
        switch secs {
        case 0..<60: return "\(secs)s"
        case 60..<3600: return "\(secs / 60)m"
        case 3600..<86400: return "\(secs / 3600)h"
        default: return "\(secs / 86400)d"
        }
    }

    // MARK: - Adaptive colors

    private var isDark: Bool { colorScheme == .dark }

    private var brandBlue: Color { Color(red: 10/255, green: 132/255, blue: 255/255) }

    private var heroGradient: [Color] {
        isDark
        ? [Color(red: 0.08, green: 0.12, blue: 0.22),
           Color(red: 0.06, green: 0.10, blue: 0.18)]
        : [Color(red: 0.90, green: 0.95, blue: 1.0),
           Color(red: 0.82, green: 0.90, blue: 0.99)]
    }

    private var mountainBackColor: Color {
        isDark ? Color(red: 0.10, green: 0.18, blue: 0.35) : Color(red: 0.66, green: 0.80, blue: 0.98)
    }

    private var mountainFrontColor: Color {
        isDark ? Color(red: 0.06, green: 0.13, blue: 0.28) : Color(red: 0.50, green: 0.70, blue: 0.96)
    }

    private var headlineColor: Color {
        isDark ? Color(red: 0.92, green: 0.94, blue: 0.98) : Color(red: 0.05, green: 0.07, blue: 0.11)
    }

    private var descriptionColor: Color {
        isDark ? Color(red: 0.60, green: 0.64, blue: 0.72) : Color(red: 0.43, green: 0.46, blue: 0.50)
    }

    private var subtitleColor: Color {
        isDark ? Color(red: 0.55, green: 0.58, blue: 0.65) : Color(red: 0.49, green: 0.51, blue: 0.54)
    }

    private var badgeBackground: Color {
        isDark ? Color(red: 0.15, green: 0.18, blue: 0.25).opacity(0.95) : .white.opacity(0.95)
    }

    private var badgeValueColor: Color {
        isDark ? Color(red: 0.90, green: 0.92, blue: 0.95) : Color(red: 0.08, green: 0.10, blue: 0.13)
    }

    private var badgeLabelColor: Color {
        isDark ? Color(red: 0.60, green: 0.64, blue: 0.72) : Color(red: 0.21, green: 0.23, blue: 0.27)
    }

    private var sectionLabelColor: Color {
        isDark ? Color(red: 0.55, green: 0.58, blue: 0.65) : Color(red: 0.55, green: 0.56, blue: 0.58)
    }

    private var cardBackground: Color {
        isDark ? Color(red: 0.14, green: 0.16, blue: 0.20) : .white
    }

    private var cardTitleColor: Color {
        isDark ? Color(red: 0.92, green: 0.94, blue: 0.98) : Color(red: 0.06, green: 0.08, blue: 0.11)
    }

    private var cardDescColor: Color {
        isDark ? Color(red: 0.55, green: 0.58, blue: 0.65) : Color(red: 0.55, green: 0.56, blue: 0.58)
    }

    private var cardIconBg: Color {
        isDark ? Color(red: 0.12, green: 0.22, blue: 0.38) : Color(red: 0.91, green: 0.95, blue: 1.0)
    }

    private var cardBorderColor: Color {
        isDark ? Color.white.opacity(0.08) : Color.black.opacity(0.06)
    }

    private var cardShadowColor: Color {
        isDark ? .clear : .black
    }

    private var chevronMuted: Color {
        isDark ? Color(red: 0.35, green: 0.38, blue: 0.45) : Color(red: 0.78, green: 0.80, blue: 0.82)
    }

    private var footerTextColor: Color {
        isDark ? Color(red: 0.50, green: 0.53, blue: 0.60) : Color(red: 0.55, green: 0.56, blue: 0.58)
    }

    private var domainColor: Color {
        isDark ? Color(red: 0.30, green: 0.33, blue: 0.38) : Color(red: 0.76, green: 0.76, blue: 0.78)
    }

    private var overscrollColor: Color {
        isDark ? Color(red: 0.08, green: 0.12, blue: 0.22) : Color(red: 0.90, green: 0.95, blue: 1.0)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 0) {
                    heroSection
                    contentSection
                    footerSection
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            .ignoresSafeArea(edges: .top)
            .background(Color(.systemBackground))
            .background(alignment: .top) {
                overscrollColor
                    .frame(height: 400)
                    .ignoresSafeArea(edges: .top)
            }
            .toolbar(.hidden)
            // 登录错误不再用 .alert 弹窗打断——改为在展开的角色卡片里
            // 内联红字提示（见 roleCard 的 errorMessage 行）。打断式 alert
            // 强制用户先点 OK 才能改密码重试，不友好。
            // 登录成功的触觉确认：isAuthenticated 从 false → true 时触发 .success
            // 反馈。closure 形式只在真正"登录"那一刻响一次，logout (true→false)
            // 或重渲染不会误触发。
            .sensoryFeedback(.success, trigger: auth.isAuthenticated) { old, new in
                !old && new
            }
            .alert("Save for \(BiometricAuthService.biometryName)?", isPresented: $showSaveBiometricPrompt) {
                Button("Save") {
                    if let c = pendingSaveCredential {
                        try? BiometricAuthService.saveCredentials(
                            .init(username: c.username, password: c.password))
                    }
                    pendingSaveCredential = nil
                }
                Button("Not Now", role: .cancel) {
                    pendingSaveCredential = nil
                }
            } message: {
                Text("Next time, sign in instantly with \(BiometricAuthService.biometryName) instead of typing your password.")
            }
            .task { await fetchStats() }
            .sheet(isPresented: $showRegister) {
                registerSheet
            }
        }
    }

    // MARK: - Fetch live stats

    private func fetchStats() async {
        do {
            let summary = try await APIClient.shared.getPublicSummary()
            liveCount = summary.total
            new24h = summary.new24h
            changes24h = summary.changes24h
            let iso = summary.lastScrape
            if !iso.isEmpty, iso != "--" {
                lastScrapeAt = Self.isoFormatter.date(from: iso)
            }
        } catch { }
    }

    // MARK: - Hero

    private var heroSection: some View {
        ZStack(alignment: .bottom) {
            LinearGradient(colors: heroGradient, startPoint: .top, endPoint: .bottom)

            MountainPath(points: [
                (0, 0.70), (0.07, 0.52), (0.13, 0.68), (0.20, 0.45), (0.26, 0.28),
                (0.34, 0.55), (0.42, 0.35), (0.50, 0.58), (0.56, 0.45), (0.63, 0.70),
                (0.70, 0.30), (0.77, 0.62), (0.84, 0.48), (0.91, 0.70), (1.0, 0.48),
                (1.0, 1.0), (0, 1.0)
            ])
            .fill(mountainBackColor)
            .frame(height: 115)

            MountainPath(points: [
                (0, 0.72), (0.05, 0.50), (0.12, 0.72), (0.18, 0.40), (0.25, 0.24),
                (0.34, 0.62), (0.41, 0.34), (0.49, 0.70), (0.55, 0.55), (0.63, 0.80),
                (0.70, 0.42), (0.77, 0.72), (0.84, 0.45), (0.91, 0.72), (1.0, 0.58),
                (1.0, 1.0), (0, 1.0)
            ])
            .fill(mountainFrontColor)
            .frame(height: 95)

            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 12) {
                    ZStack {
                        Circle().fill(Color(.systemBackground)).frame(width: 48, height: 48)
                        houseShape
                            .fill(brandBlue)
                            .frame(width: 26, height: 18)
                            // 减弱动态效果开启时，呼吸缩放固定在中间值（1.0），
                            // 不再随时间变化；动画完全跳过。
                            .scaleEffect(reduceMotion ? 1.0 : (breathe ? 1.12 : 0.88))
                    }
                    .clipShape(Circle())
                    .onAppear {
                        guard !reduceMotion else { return }
                        withAnimation(.easeInOut(duration: 2.2).repeatForever(autoreverses: true)) {
                            breathe = true
                        }
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text("FlatRadar")
                            .font(.system(size: 19, weight: .heavy))
                            .foregroundStyle(brandBlue)
                        Text("UNOFFICIAL · v\(appVersion)")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(subtitleColor)
                            .tracking(1.5)
                    }
                }

                Text(expandedRole == nil
                     ? "Searching for a new\nhome in the Netherlands?"
                     : (expandedRole == .guest ? "Browse listings\nread-only." : "Sign in to your\naccount."))
                    .font(.system(size: 28, weight: .black))
                    .foregroundStyle(headlineColor)
                    .tracking(-0.8)
                    .lineSpacing(4)
                    .padding(.top, 26)

                Text("A real-time monitor for Holland2Stay availability.")
                    .font(.system(size: 16))
                    .foregroundStyle(descriptionColor)
                    .padding(.top, 14)

                HStack(spacing: 10) {
                    badge(icon: "circle.fill", iconColor: .green, value: "\(liveCount)", label: "live")
                    badge(icon: "clock", iconColor: .secondary, value: timeAgo, label: "ago")
                    badge(icon: "bell.fill", iconColor: .secondary, value: "\(new24h)", label: "new today")
                }
                .padding(.top, 22)

                Spacer()
            }
            .padding(.horizontal, 22)
            .padding(.top, 70)
            .frame(height: 350)
        }
        .frame(height: 350)
    }

    private func badge(icon: String, iconColor: Color, value: String, label: String) -> some View {
        HStack(spacing: 5) {
            Image(systemName: icon)
                .font(.system(size: 7))
                .foregroundStyle(iconColor)
            Text(value).font(.system(size: 14, weight: .bold))
                .foregroundStyle(badgeValueColor)
            Text(label).font(.system(size: 14))
                .foregroundStyle(badgeLabelColor)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(badgeBackground)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(isDark ? 0 : 0.06), radius: 4, y: 2)
    }

    // MARK: - Content

    private var contentSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            if BiometricAuthService.hasStoredCredentials {
                biometricButton
                    .padding(.bottom, 16)
            }

            Text("CONTINUE AS")
                .font(.system(size: 12, weight: .heavy))
                .foregroundStyle(sectionLabelColor)
                .tracking(3.5)
                .padding(.leading, 4)
                .padding(.bottom, 14)
                .padding(.top, 20)

            expandableCard(
                mode: .user, icon: "person.fill", title: "Tenant",
                description: "Saved searches, alerts, watching history",
                isExpanded: expandedRole == .user
            )
            expandableCard(
                mode: .guest, icon: "eye.fill", title: "Guest",
                description: "Browse current listings only",
                isExpanded: expandedRole == .guest
            )
            expandableCard(
                mode: .admin, icon: "shield.fill", title: "Staff",
                description: "Manage scrapers, users, push alerts",
                isExpanded: expandedRole == .admin
            )
        }
        .padding(.horizontal, 18)
    }

    // MARK: - Expandable card

    private func expandableCard(
        mode: LoginMode, icon: String, title: String, description: String, isExpanded: Bool
    ) -> some View {
        VStack(spacing: 0) {
            Button {
                withAnimation(.spring(duration: 0.35, bounce: 0.2)) {
                    expandedRole = isExpanded ? nil : mode
                }
                if mode == .guest { Task { await performLoginAsGuest() } }
            } label: {
                HStack(spacing: 14) {
                    ZStack {
                        // 44pt 命中 iOS HIG；这块图标背景虽然不是独立 button
                        // （外层 HStack 一整块 Button 都可点），但与右上角等其
                        // 它 icon-square 视觉统一在 44 也更协调。
                        RoundedRectangle(cornerRadius: 12)
                            .fill(cardIconBg).frame(width: 44, height: 44)
                        Image(systemName: icon)
                            .font(.system(size: 20)).foregroundStyle(brandBlue)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(title)
                                .font(.system(size: 18, weight: .heavy))
                                .foregroundStyle(cardTitleColor)
                            if mode == .user {
                                Text("MOST")
                                    .font(.system(size: 10, weight: .heavy))
                                    .foregroundStyle(brandBlue).tracking(1)
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(cardIconBg)
                                    .clipShape(RoundedRectangle(cornerRadius: 5))
                            }
                        }
                        Text(description)
                            .font(.system(size: 14)).foregroundStyle(cardDescColor)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: 22, weight: .light))
                        .foregroundStyle(isExpanded ? brandBlue : chevronMuted)
                        .rotationEffect(isExpanded ? .degrees(90) : .zero)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded, mode != .guest {
                VStack(spacing: 12) {
                    Divider()

                    if mode == .user {
                        HStack(spacing: 0) {
                            Image(systemName: "envelope.fill")
                                .font(.caption).foregroundStyle(.secondary).frame(width: 28)
                            TextField("Email or username", text: $username)
                                .textContentType(.emailAddress).textFieldStyle(.plain)
                                .autocorrectionDisabled().textInputAutocapitalization(.never)
                        }
                        .padding(12)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                    }

                    HStack(spacing: 0) {
                        Image(systemName: "key.fill")
                            .font(.caption).foregroundStyle(.secondary).frame(width: 28)
                        // 眼睛 toggle：根据 showPasswordPlain 在 TextField/SecureField
                        // 之间切换。两个组件共用同一 @State password，无需迁移。
                        if showPasswordPlain {
                            TextField("App password", text: $password)
                                .textContentType(.password).textFieldStyle(.plain)
                                .autocorrectionDisabled().textInputAutocapitalization(.never)
                        } else {
                            SecureField("App password", text: $password)
                                .textContentType(.password).textFieldStyle(.plain)
                        }
                        Button {
                            showPasswordPlain.toggle()
                        } label: {
                            Image(systemName: showPasswordPlain ? "eye.slash.fill" : "eye.fill")
                                .font(.caption).foregroundStyle(.secondary)
                                .frame(width: 28, height: 28)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(showPasswordPlain ? "Hide password" : "Show password")
                    }
                    .padding(12)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))

                    // 内联错误提示 —— 替代之前打断式 .alert。仅在该角色卡片
                    // 展开时显示，跟密码输入框紧贴，用户改密码时一眼能看到。
                    if let err = inlineLoginError(for: mode) {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.circle.fill")
                                .font(.caption)
                            Text(err)
                                .font(.caption)
                                .multilineTextAlignment(.leading)
                            Spacer(minLength: 0)
                        }
                        .foregroundStyle(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }

                    Button {
                        Task { await performLogin(mode: mode) }
                    } label: {
                        HStack(spacing: 6) {
                            if auth.isLoading { ProgressView() }
                            Text("Login").fontWeight(.semibold)
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, 12)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(loginDisabled(for: mode))
                    .tint(mode == .admin ? .red : .blue)

                    if mode == .user {
                        HStack(spacing: 4) {
                            Text("Don't have an account?")
                                .font(.caption).foregroundStyle(.secondary)
                            Button("Register") {
                                showRegister = true
                            }
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(brandBlue).underline()
                        }
                        .padding(.top, 4)
                    }
                }
                .padding(.horizontal, 16).padding(.bottom, 16)
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .strokeBorder(isExpanded ? brandBlue : cardBorderColor,
                              lineWidth: isExpanded ? 2 : 1)
        }
        .shadow(color: cardShadowColor.opacity(isExpanded ? 0.08 : 0.03),
                radius: isExpanded ? 12 : 4, y: isExpanded ? 4 : 1)
        .padding(.bottom, 12)
    }

    // MARK: - Footer

    private var footerSection: some View {
        VStack(spacing: 12) {
            Divider().padding(.horizontal, 25)

            Text("FlatRadar is an **unofficial** third-party client.\nNot affiliated with, endorsed by, or sponsored by Holland2Stay.\nAll listing data belongs to its respective owners.")
                .font(.system(size: 12))
                .foregroundStyle(footerTextColor)
                .multilineTextAlignment(.center).lineSpacing(3)

            HStack(spacing: 4) {
                Button(LegalText.isChineseLocale ? "使用条款" : "Terms") { showTerms = true }
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(brandBlue)
                Text("·").foregroundStyle(.secondary).font(.caption)
                Button(LegalText.isChineseLocale ? "隐私政策" : "Privacy") { showPrivacy = true }
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(brandBlue)
            }

            Text("flatradar.app")
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(domainColor).tracking(1)
        }
        .padding(.top, 24).padding(.bottom, 36)
        .sheet(isPresented: $showTerms) {
            legalSheet(title: LegalText.isChineseLocale ? "使用条款" : "Terms of Use",
                      content: termsText)
        }
        .sheet(isPresented: $showPrivacy) {
            legalSheet(title: LegalText.isChineseLocale ? "隐私政策" : "Privacy Policy",
                       content: privacyText)
        }
    }

    private func legalSheet(title: String, content: String) -> some View {
        LegalSheetView(title: title, content: content)
    }

    private var termsText: String { LegalText.termsLocalized }
    private var privacyText: String { LegalText.privacyLocalized }

    // MARK: - Biometric

    private var biometricButton: some View {
        let name = BiometricAuthService.biometryName
        return Button {
            Task { await performBiometricLogin() }
        } label: {
            HStack(spacing: 10) {
                if isAuthenticatingBiometric {
                    ProgressView().controlSize(.small)
                }
                Image(systemName: BiometricAuthService.biometryName == "Face ID"
                      ? "faceid" : "touchid")
                    .font(.system(size: 20))
                Text("Sign in with \(name)")
                    .font(.system(size: 16, weight: .semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
        }
        .buttonStyle(.bordered)
        .disabled(isAuthenticatingBiometric)
    }

    private func performBiometricLogin() async {
        isAuthenticatingBiometric = true
        defer { isAuthenticatingBiometric = false }

        guard let cred = await BiometricAuthService.authenticateAndLoad(
            reason: "Unlock FlatRadar to sign in"
        ) else { return }

        if cred.username == "__admin__" {
            await auth.loginAsAdmin(password: cred.password)
        } else {
            await auth.loginAsUser(name: cred.username, password: cred.password)
        }
        if auth.isAuthenticated, !auth.isGuest {
            await push.requestPermissionAndRegister()
        }
    }

    // MARK: - Helpers

    private var houseShape: some Shape {
        MountainPath(points: [
            (0, 1.0), (0, 0.55), (0.35, 0.20), (0.55, 0.45),
            (0.80, 0.20), (1.0, 0.55), (1.0, 1.0)
        ])
    }

    /// 当前应该在哪个角色的卡片里显示内联错误。
    /// - 只在卡片展开 && 该 mode 不是 guest && AuthStore 有错时显示
    /// - guest 模式没有密码字段，错误也没什么位置可放（理论上 guest 不会失败）
    private func inlineLoginError(for mode: LoginMode) -> String? {
        guard expandedRole == mode, mode != .guest else { return nil }
        guard let err = auth.lastError?.errorDescription ?? auth.errorMessage,
              !err.isEmpty else { return nil }
        return err
    }

    private func loginDisabled(for mode: LoginMode) -> Bool {
        if auth.isLoading { return true }
        switch mode {
        case .admin: return password.isEmpty
        case .user:  return username.isEmpty || password.isEmpty
        case .guest: return false
        }
    }

    private func performLogin(mode: LoginMode) async {
        switch mode {
        case .admin: await auth.loginAsAdmin(password: password)
        case .user:  await auth.loginAsUser(name: username, password: password)
        case .guest: break
        }
        if auth.isAuthenticated, !auth.isGuest {
            await push.requestPermissionAndRegister()

            // 第一次手动登录成功后，提示是否保存凭据供 Face ID 使用
            if BiometricAuthService.isAvailable, !BiometricAuthService.hasStoredCredentials {
                let user = mode == .admin ? "__admin__" : username
                let pwd = password
                pendingSaveCredential = (user, pwd)
                showSaveBiometricPrompt = true
            }
        }
    }

    private func performLoginAsGuest() async {
        auth.enterAsGuest()
    }

    // MARK: - Register sheet

    private var registerSheet: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Image(systemName: "person.badge.plus")
                    .font(.system(size: 40))
                    .foregroundStyle(.blue)
                    .padding(.top, 24)

                Text("Create Account")
                    .font(.title2.weight(.bold))

                VStack(spacing: 12) {
                    HStack(spacing: 0) {
                        Image(systemName: "person.fill")
                            .font(.caption).foregroundStyle(.secondary).frame(width: 28)
                        TextField("Username", text: $regUsername)
                            .textContentType(.username).textFieldStyle(.plain)
                            .autocorrectionDisabled().textInputAutocapitalization(.never)
                    }
                    .padding(12).background(.quinary, in: RoundedRectangle(cornerRadius: 10))

                    HStack(spacing: 0) {
                        Image(systemName: "key.fill")
                            .font(.caption).foregroundStyle(.secondary).frame(width: 28)
                        if showRegPasswordPlain {
                            TextField("Password (min 4 characters)", text: $regPassword)
                                .textContentType(.newPassword).textFieldStyle(.plain)
                                .autocorrectionDisabled().textInputAutocapitalization(.never)
                        } else {
                            SecureField("Password (min 4 characters)", text: $regPassword)
                                .textContentType(.newPassword).textFieldStyle(.plain)
                        }
                        Button {
                            showRegPasswordPlain.toggle()
                        } label: {
                            Image(systemName: showRegPasswordPlain ? "eye.slash.fill" : "eye.fill")
                                .font(.caption).foregroundStyle(.secondary)
                                .frame(width: 28, height: 28)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(showRegPasswordPlain ? "Hide password" : "Show password")
                    }
                    .padding(12).background(.quinary, in: RoundedRectangle(cornerRadius: 10))
                }
                .padding(.horizontal)

                Button {
                    Task { await performRegister() }
                } label: {
                    HStack(spacing: 6) {
                        if auth.isLoading { ProgressView() }
                        Text("Create Account & Login").fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 12)
                }
                .buttonStyle(.borderedProminent)
                .disabled(regUsername.count < 2 || regPassword.count < 4 || auth.isLoading)
                .padding(.horizontal)

                if let err = auth.errorMessage {
                    Text(err)
                        .font(.caption).foregroundStyle(.red)
                        .multilineTextAlignment(.center).padding(.horizontal)
                }

                HStack(spacing: 4) {
                    Text("By creating an account, you agree to our")
                        .font(.caption2).foregroundStyle(.tertiary)
                    Button("Terms") { showTerms = true }
                        .font(.caption2).foregroundStyle(brandBlue)
                }

                Spacer()
            }
            .presentationDetents([.fraction(0.48)])
            .presentationDragIndicator(.visible)
        }
    }

    private func performRegister() async {
        guard regUsername.count >= 2, regPassword.count >= 4 else { return }
        await auth.register(name: regUsername, password: regPassword)
        if auth.isAuthenticated, !auth.isGuest {
            showRegister = false
            await push.requestPermissionAndRegister()
        }
    }
}

// MARK: - Mountain path shape

private struct MountainPath: Shape {
    let points: [(CGFloat, CGFloat)]

    func path(in rect: CGRect) -> Path {
        Path { p in
            guard let first = points.first else { return }
            p.move(to: CGPoint(x: first.0 * rect.width, y: first.1 * rect.height))
            for pt in points.dropFirst() {
                p.addLine(to: CGPoint(x: pt.0 * rect.width, y: pt.1 * rect.height))
            }
            p.closeSubpath()
        }
    }
}

// MARK: - Legal sheet helper

struct LegalSheetView: View {
    let title: String
    let content: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                Text(content)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding()
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
