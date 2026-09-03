import SwiftUI

struct LoginView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @Environment(\.colorScheme) private var colorScheme
    /// "减弱动态效果"：用户在 设置 > 辅助功能 > 动态效果 里开启时为 true。
    /// 受影响的动画（如 hero 图标呼吸）应在此 flag true 时跳过或显著弱化。
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// "增加对比度"系统开关。开启时把若干自定义灰阶 token 提到更深 / 更亮，
    /// 达到 WCAG AA 4.5:1。受影响的全是非语义自定义 RGB 颜色，Apple semantic
    /// label color（.primary / .secondary / .tertiary）由系统自己调整。
    @Environment(\.colorSchemeContrast) private var contrast
    private var highContrast: Bool { contrast == .increased }
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
    /// "live" 小绿点的两段动画相位，**各自独立的状态 + 各自的 repeatForever 曲线**：
    /// - liveRipple：外圈光晕，easeOut + 不回弹（放大渐隐后从头来）
    /// - liveCore  ：内核实心点，easeInOut + 回弹（1.0↔1.12 原地呼吸）
    /// 之前用单个 liveDotBreathing + `.animation(_, value:)` 驱动两段——那种写法
    /// 的 repeatForever 会被视图出现/转场的 ambient 事务"捕获"，偶发变成一次性
    /// 弹跳而不是持续呼吸。改成显式 withAnimation(.repeatForever) 驱动，稳定。
    @State private var liveRipple = false
    @State private var liveCore = false
    @State private var showTerms = false
    @State private var showPrivacy = false
    /// 登录被拒且是 401 时，待确认建号的用户名。非 nil 即弹确认框。
    @State private var pendingRegistrationName: String?
    @State private var isAuthenticatingBiometric = false
    @State private var contentWidth: CGFloat = 0
    private var useLargeCards: Bool { contentWidth > 410 }

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
        // 默认 light vs 白 ≈ 4.9:1（达标），但 12pt 用户字号放大时变体可能跌破。
        // Increase Contrast 时拉到 ~7:1 给余量。
        if highContrast {
            return isDark ? Color(red: 0.85, green: 0.87, blue: 0.92)
                          : Color(red: 0.20, green: 0.22, blue: 0.26)
        }
        return isDark ? Color(red: 0.60, green: 0.64, blue: 0.72) : Color(red: 0.43, green: 0.46, blue: 0.50)
    }

    private var subtitleColor: Color {
        // 默认 light vs 白 ≈ 4.3:1（边缘失败，11pt INDEPENDENT 字号小风险更高）。
        if highContrast {
            return isDark ? Color(red: 0.82, green: 0.84, blue: 0.90)
                          : Color(red: 0.25, green: 0.27, blue: 0.31)
        }
        return isDark ? Color(red: 0.55, green: 0.58, blue: 0.65) : Color(red: 0.49, green: 0.51, blue: 0.54)
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
        // 默认值在 light mode 下 vs 白底约 3.9:1，刚好低于 WCAG AA 4.5:1。
        // 开 Increase Contrast 时拉到 ~7.1:1（深灰），暗模式也同步提亮。
        if highContrast {
            return isDark ? Color(red: 0.82, green: 0.84, blue: 0.88)
                          : Color(red: 0.32, green: 0.33, blue: 0.35)
        }
        return isDark ? Color(red: 0.50, green: 0.53, blue: 0.60) : Color(red: 0.55, green: 0.56, blue: 0.58)
    }

    private var domainColor: Color {
        // 默认值 light mode 下 vs 白底仅 ~1.5:1（远低于 AA），属于"水印感"装饰。
        // Increase Contrast 时硬拉到 ~4.6:1，保证 12pt mono 也能稳读。
        if highContrast {
            return isDark ? Color(red: 0.70, green: 0.72, blue: 0.78)
                          : Color(red: 0.40, green: 0.40, blue: 0.42)
        }
        return isDark ? Color(red: 0.30, green: 0.33, blue: 0.38) : Color(red: 0.76, green: 0.76, blue: 0.78)
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
                .background(GeometryReader { proxy in
                    Color.clear.onAppear { contentWidth = proxy.size.width }
                        .onChange(of: proxy.size.width) { _, w in contentWidth = w }
                })
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
            .task { await fetchStats() }
            // 登录即注册：名字没被注册过时，登录会被后端以 401 拒绝（响应刻意
            // 不区分"密码错"和"查无此人"，不给用户枚举留侧信道）。这里把决定权
            // 交回用户——要不要用这个名字建一个号。
            //
            // 条款同意就落在这个确认框上：Web 端删掉自动注册时列的第一条理由，
            // 正是"登录表单上根本没有勾选框，只能替用户默认同意"。
            .confirmationDialog(
                "Create an account?",
                isPresented: Binding(
                    get: { pendingRegistrationName != nil },
                    set: { if !$0 { pendingRegistrationName = nil } }
                ),
                titleVisibility: .visible,
                presenting: pendingRegistrationName
            ) { name in
                Button("Create Account") {
                    pendingRegistrationName = nil
                    Task { await performRegister() }
                }
                Button("Cancel", role: .cancel) { pendingRegistrationName = nil }
            } message: { name in
                // 第一句必须留着：这个确认框对**任何** 401 都弹，包括"账号存在但
                // 密码打错了"。写成"这个名字还没被注册"就等于替后端确认了账号不
                // 存在——那正是后端刻意不透露的东西。
                Text("No account signed in as \"\(name)\" with that password.\n\nSigning in with a new name creates the account — no separate registration needed.\n\nBy continuing you agree to the Terms of Use and Privacy Policy.")
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
                    // App 自己的 logo，而不是手画的 houseShape 套一个白/黑圆。
                    //
                    // 圆底原本是 Color(.systemBackground)：深色模式下那是**纯黑**，
                    // 扣在深蓝色的 hero 背景上是一块硬邦邦的黑饼。
                    // BrandLogo 图集里浅深两版各自带背景（与 App 图标同源），
                    // SwiftUI 按 colorScheme 自己挑，不需要在这里判断主题。
                    Image("BrandLogo")
                        .resizable()
                        .frame(width: 48, height: 48)
                        .clipShape(RoundedRectangle(cornerRadius: 11, style: .continuous))
                        // 呼吸幅度从 ±12% 收到 ±3%：原先缩放的是圆里那个小房子，
                        // 现在缩放的是整枚徽标，同样的幅度会晃得很明显。
                        .scaleEffect(reduceMotion ? 1.0 : (breathe ? 1.03 : 0.97))
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
                        Text("INDEPENDENT · v\(appVersion)")
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

                // 平台数由 Platform 推出来，不写死——写死的数字就是下一次
                // "登录页还写着 H2S"。接第八个平台时这里自动跟上。
                Text("Real-time availability across \(Platform.knownKeys.count) rental platforms.")
                    .font(.system(size: 16))
                    .foregroundStyle(descriptionColor)
                    .padding(.top, 14)

                HStack(spacing: 10) {
                    badge(icon: "circle.fill", iconColor: .green, value: "\(liveCount)", label: "live", animatesIcon: true)
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

    private func badge(
        icon: String,
        iconColor: Color,
        value: String,
        label: String,
        animatesIcon: Bool = false
    ) -> some View {
        // 只有 live 小绿点这种"实时/在线"语义的徽章才传 animatesIcon=true。
        // reduceMotion 开启时即使要求动画也停下来——遵守 iOS HIG。
        let shouldAnimate = animatesIcon && !reduceMotion
        return HStack(spacing: 5) {
            // animatesIcon=true 时（live 那条）改用裸 Circle，与 Dashboard
            // liveBadge 的做法保持一致：
            // - SF Symbol Image("circle.fill") 在 .font(size:7) 下 glyph box
            //   比可见圆大（含字体上下空白），HStack 居中对齐时圆视觉偏下
            // - 裸 Circle 是 Shape，无字体度量，frame 就是可见尺寸，对齐准
            // 其他 badge (clock / bell.fill) 仍保留 Image，因为视觉上对齐没问题
            if animatesIcon {
                ZStack {
                    if shouldAnimate {
                        // 外层光晕：放大 + 渐隐反复。动画由 startLiveBreathing()
                        // 的显式 withAnimation(.repeatForever) 驱动——这里不再挂
                        // .animation(value:)，避免被外层转场事务捕获成弹跳。
                        Circle()
                            .fill(iconColor)
                            .frame(width: 7, height: 7)
                            .scaleEffect(liveRipple ? 2.4 : 1.0)
                            .opacity(liveRipple ? 0.0 : 0.45)
                    }
                    // 内层实心点：原地轻微缩放呼吸（1.0↔1.12）
                    Circle()
                        .fill(iconColor)
                        .frame(width: 7, height: 7)
                        .scaleEffect(liveCore ? 1.12 : 1.0)
                        .shadow(color: iconColor.opacity(0.4), radius: 5, x: 0, y: 0)
                }
                // 锁定布局尺寸，光晕 / ripple 只在视觉上溢出
                .frame(width: 7, height: 7)
            } else {
                Image(systemName: icon)
                    .font(.system(size: 7))
                    .foregroundStyle(iconColor)
            }
            Text(value).font(.system(size: 14, weight: .bold))
                .foregroundStyle(badgeValueColor)
            Text(label).font(.system(size: 14))
                .foregroundStyle(badgeLabelColor)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        // ⚠️ 关键修正：原本用 `.background(...).clipShape(RoundedRectangle(...))`
        // 会把 ripple 在 badge 圆角处剪掉一块，导致动画看起来不是"原地呼吸"
        // 而是朝一个方向偏出。DashboardView.liveBadge 用的是 `.background(_, in:)`
        // —— 圆角只作用于背景，content（含 ripple）不参与裁剪，与 dashboard 保持一致。
        .background(badgeBackground, in: RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(isDark ? 0 : 0.06), radius: 4, y: 2)
        .onAppear {
            if shouldAnimate { startLiveBreathing() }
        }
        // reduceMotion 切换时实时停/起。
        .onChange(of: shouldAnimate) { _, willAnimate in
            if willAnimate { startLiveBreathing() } else { stopLiveBreathing() }
        }
    }

    /// 启动 live 绿点的持续呼吸。两段各用**自己的** repeatForever 曲线显式驱动。
    /// 关键稳定点：
    /// 1. 先用 disablesAnimations 事务把相位复位到 false——上一轮 repeatForever
    ///    若被中途打断，残留中间态会和新动画 blend 成"弹跳"。
    /// 2. 用 DispatchQueue.main.async 推迟到下一个 runloop 再启动——避开视图
    ///    appear / 导航转场那一帧的 ambient 事务，否则 repeatForever 会被它
    ///    捕获成一次性 spring。
    private func startLiveBreathing() {
        var reset = Transaction()
        reset.disablesAnimations = true
        withTransaction(reset) {
            liveRipple = false
            liveCore = false
        }
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 1.6).repeatForever(autoreverses: false)) {
                liveRipple = true
            }
            withAnimation(.easeInOut(duration: 1.6).repeatForever(autoreverses: true)) {
                liveCore = true
            }
        }
    }

    /// 停止呼吸：无动画复位到静止态（reduceMotion 开启时调用）。
    private func stopLiveBreathing() {
        var reset = Transaction()
        reset.disablesAnimations = true
        withTransaction(reset) {
            liveRipple = false
            liveCore = false
        }
    }

    // MARK: - Content

    private var contentSection: some View {
        VStack(alignment: .leading, spacing: 0) {
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
            // Staff 卡片已移除。后端 /auth/login 本来就按用户名分流——
            // `__admin__` + 管理密码走 admin 分支，其余走 user 表，角色由服务端
            // 在响应里给出（AuthStore.applyMe 读 me.role）。管理员从这同一个
            // 表单登录即可，前端不需要一个独立入口，也不该在登录页上公示后台的
            // 存在。

            if BiometricAuthService.hasStoredCredentials {
                biometricButton
                    .padding(.top, 16)
            }
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
                HStack(spacing: useLargeCards ? 14 : 13) {
                    ZStack {
                        let iconSize: CGFloat = useLargeCards ? 44 : 42
                        RoundedRectangle(cornerRadius: useLargeCards ? 12 : 11)
                            .fill(cardIconBg).frame(width: iconSize, height: iconSize)
                        Image(systemName: icon)
                            .font(.system(size: useLargeCards ? 20 : 19)).foregroundStyle(brandBlue)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(title)
                                .font(.system(size: useLargeCards ? 18 : 17, weight: .heavy))
                                .foregroundStyle(cardTitleColor)
                            if mode == .user {
                                Text("MOST")
                                    .font(.system(size: useLargeCards ? 10 : 9, weight: .heavy))
                                    .foregroundStyle(brandBlue).tracking(1)
                                    .padding(.horizontal, useLargeCards ? 6 : 5).padding(.vertical, 1)
                                    .background(cardIconBg)
                                    .clipShape(RoundedRectangle(cornerRadius: useLargeCards ? 5 : 4))
                            }
                        }
                        Text(description)
                            .font(.system(size: useLargeCards ? 14 : 13)).foregroundStyle(cardDescColor)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: useLargeCards ? 22 : 20, weight: .light))
                        .foregroundStyle(isExpanded ? brandBlue : chevronMuted)
                        .rotationEffect(isExpanded ? .degrees(90) : .zero)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(useLargeCards ? 16 : 13)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded, mode != .guest {
                VStack(spacing: 8) {
                    Divider()

                    if mode == .user {
                        HStack(spacing: 0) {
                            Image(systemName: "envelope.fill")
                                .font(.caption).foregroundStyle(.secondary).frame(width: 24)
                            TextField("Email or username", text: $username)
                                .textContentType(.emailAddress).textFieldStyle(.plain)
                                .autocorrectionDisabled().textInputAutocapitalization(.never)
                        }
                        .padding(10)
                        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                    }

                    HStack(spacing: 0) {
                        Image(systemName: "key.fill")
                            .font(.caption).foregroundStyle(.secondary).frame(width: 24)
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
                                .frame(width: 24, height: 24)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(showPasswordPlain ? "Hide password" : "Show password")
                    }
                    .padding(10)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))

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
                            if mode == .user {
                                Text("Sign In / Register").fontWeight(.semibold)
                            } else {
                                Text("Login").fontWeight(.semibold)
                            }
                        }
                        .frame(maxWidth: .infinity).padding(.vertical, 10)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(loginDisabled(for: mode))
                    .tint(.blue)

                    if mode == .user {
                        // 注册不再是单独一屏：名字没被注册过时，登录失败会问一句
                        // 「要不要用这个名字建号」，同意即注册（见 offerRegistration）。
                        VStack(spacing: 2) {
                            // 与网页端登录页同一句：「未注册的账户将自动完成注册。」
                            Text("Unregistered accounts are created automatically.")
                                .font(.caption).foregroundStyle(.secondary)
                            Text("By continuing you agree to the Terms of Use and Privacy Policy.")
                                .font(.caption).foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                        .padding(.top, 4)
                    }
                }
                .padding(.horizontal, 12).padding(.bottom, 12)
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

            // 只声明"与 Holland2Stay 无关"是不够的：现在监控七个平台，其余六个
            // 一个都没覆盖到。改成泛指，加平台时不必再回来改这句法律声明。
            Text("FlatRadar is an **independent** third-party client.\nNot affiliated with, endorsed by, or sponsored by any of the platforms it monitors.\nAll listing data belongs to its respective owners.")
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
            LegalSheetView(title: LegalText.isChineseLocale ? "使用条款" : "Terms of Use",
                          kind: "terms")
        }
        .sheet(isPresented: $showPrivacy) {
            LegalSheetView(title: LegalText.isChineseLocale ? "隐私政策" : "Privacy Policy",
                          kind: "privacy")
        }
    }

    // MARK: - Biometric

    private var biometricButton: some View {
        let name = BiometricAuthService.biometryName
        return Button {
            Task { await performBiometricLogin() }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: name == "Face ID" ? "faceid" : "touchid")
                    .font(.system(size: 22))
                    .foregroundStyle(brandBlue)
                Text("Sign in with \(name)")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(cardTitleColor)
                Spacer()
                if isAuthenticatingBiometric {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(chevronMuted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.vertical, 14)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isAuthenticatingBiometric)
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay {
            RoundedRectangle(cornerRadius: 18)
                .strokeBorder(cardBorderColor, lineWidth: 1)
        }
        .shadow(color: cardShadowColor.opacity(0.03), radius: 4, y: 1)
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
        // 必须在 login 之前设置 pendingBiometricCredential：
        // login 内部 isAuthenticated → true 时，ContentView.onChange
        // 会立即触发；如果 pending 在 login 之后才写，onChange 看到的还是 nil。
        if mode == .user,
           BiometricAuthService.isAvailable,
           !BiometricAuthService.hasStoredCredentials {
            auth.pendingBiometricCredential = (username, password, "user")
        }

        switch mode {
        case .admin: await auth.loginAsAdmin(password: password)
        case .user:  await auth.loginAsUser(name: username, password: password)
        case .guest: break
        }

        // 登录失败 → 清理 pending（isAuthenticated 未变，onChange 没触发）
        if !auth.isAuthenticated {
            auth.pendingBiometricCredential = nil
            // 凭据被拒（401）才提议建号。网络故障、限流、服务端错误一律不提——
            // 断网时问「要不要注册」会让用户以为自己的账号不存在了。
            // 管理员用同一个表单登录（Staff 入口已删）。他打错密码时不能弹
            // 「要用 __admin__ 建个号吗」——后端 /auth/register 明确拒绝 `__`
            // 开头的用户名，那个提议从一开始就不可能成立。
            if mode == .user,
               !isReservedName(username),
               case .unauthorized = auth.lastError {
                pendingRegistrationName = username
            }
            return
        }

        // 登录成功但拿到的不是 user 角色（管理员走同一个表单）——pending 里
        // 存着明文密码，而 ContentView 的保存提示只对 user 弹，它不会被消费，
        // 就这么留在内存里。这里显式清掉。
        if !auth.isUser {
            auth.pendingBiometricCredential = nil
        }

        if !auth.isGuest {
            await push.requestPermissionAndRegister()
        }
    }

    /// 后端保留给自己的用户名（`__admin__` 等以 `__` 开头的），不可注册。
    /// 与 `app/routes/api_v1/auth.py` 的 `_register` 校验对齐。
    private func isReservedName(_ name: String) -> Bool {
        name.trimmingCharacters(in: .whitespaces).lowercased().hasPrefix("__")
    }

    private func performLoginAsGuest() async {
        auth.enterAsGuest()
    }

    /// 用主表单里那对用户名/密码建号。
    ///
    /// 只在用户于确认框里点了「Create Account」之后才会走到这里——那个确认框
    /// 上写着条款同意，这是 App 端 terms_accepted 的落点。Web 端把自动注册删掉
    /// 时列的头一条理由就是"登录表单上根本没有那个勾选框，只能替用户默认同意"。
    ///
    /// 用户名按后端同样的规则截到 64 字符：Web 那条理由之二是自动注册绕过了
    /// `[:64]`，客户端先截一次，显示的名字与真正建出来的账号一致。
    private func performRegister() async {
        let name = String(username.trimmingCharacters(in: .whitespaces).prefix(64))
        guard name.count >= 2, password.count >= 4 else { return }

        // 注册前设 pending，同 performLogin——register 内部 login 完成后
        // isAuthenticated → true，ContentView.onChange 需要此时 pending 已就位。
        if BiometricAuthService.isAvailable,
           !BiometricAuthService.hasStoredCredentials {
            auth.pendingBiometricCredential = (name, password, "user")
        }

        await auth.register(name: name, password: password)
        if auth.isAuthenticated, !auth.isGuest {
            await push.requestPermissionAndRegister()
        } else {
            auth.pendingBiometricCredential = nil
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
    let kind: String  // "terms" or "privacy"
    @State private var loaded: String?
    @State private var isLoading = true
    @Environment(\.dismiss) private var dismiss

    /// Local fallback when API is unreachable
    private var fallback: String {
        if kind == "privacy" { return LegalText.privacyLocalized }
        return LegalText.termsLocalized
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                if isLoading {
                    ProgressView().padding(.top, 80)
                }
                Text(loaded ?? fallback)
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
            .task {
                do {
                    let resp = try await APIClient.shared.getLegal()
                    loaded = kind == "privacy" ? resp.privacy : resp.terms
                } catch {
                    // Use local fallback (already default)
                }
                isLoading = false
            }
        }
    }
}
