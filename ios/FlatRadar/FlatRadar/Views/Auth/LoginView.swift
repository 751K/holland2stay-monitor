import SwiftUI

struct LoginView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @State private var mode: LoginMode = .admin
    @State private var username = ""
    @State private var password = ""
    @State private var showError = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                // Logo / title
                VStack(spacing: 8) {
                    Image(systemName: "house.lodge")
                        .font(.system(size: 44))
                        .foregroundStyle(.tint)
                    Text("FlatRadar")
                        .font(.largeTitle.weight(.bold))
                    Text("Holland2Stay Monitor")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 40)

                // Mode picker
                LoginModePicker(mode: $mode)
                    .padding(.horizontal)

                // Form
                Group {
                    switch mode {
                    case .admin:
                        SecureField("Admin Password", text: $password)
                            .textContentType(.password)
                            .textFieldStyle(.roundedBorder)

                    case .user:
                        TextField("Username", text: $username)
                            .textContentType(.username)
                            .textFieldStyle(.roundedBorder)
                            .autocorrectionDisabled()
#if os(iOS)
                            .textInputAutocapitalization(.never)
#endif
                        SecureField("App Password", text: $password)
                            .textContentType(.password)
                            .textFieldStyle(.roundedBorder)

                    case .guest:
                        Text("Browse public statistics without logging in.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                }
                .padding(.horizontal)

                // Action button
                Button {
                    Task { await performLogin() }
                } label: {
                    HStack {
                        if auth.isLoading { ProgressView() }
                        Text(buttonLabel)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .padding(.horizontal)
                .disabled(buttonDisabled)
            }
            .frame(maxHeight: .infinity, alignment: .top)
            .padding()
            .toolbar(.hidden)
            .alert(auth.lastError?.errorDescription ?? "Login Failed", isPresented: $showError) {
                Button("OK") {}
            } message: {
                Text(auth.errorMessage ?? "Unknown error")
            }
            .onChange(of: auth.errorMessage) { _, new in
                showError = new != nil
            }
        }
    }

    private var buttonLabel: String {
        switch mode {
        case .admin: return "Login as Admin"
        case .user:  return "Login as User"
        case .guest: return "Enter as Guest"
        }
    }

    private var buttonDisabled: Bool {
        if auth.isLoading { return true }
        switch mode {
        case .admin: return password.isEmpty
        case .user:  return username.isEmpty || password.isEmpty
        case .guest: return false
        }
    }

    private func performLogin() async {
        switch mode {
        case .admin:
            await auth.loginAsAdmin(password: password)
        case .user:
            await auth.loginAsUser(name: username, password: password)
        case .guest:
            auth.enterAsGuest()
        }
        // 登录成功且非 guest 时触发 APNs 注册——guest 没有 Bearer，
        // 调 /devices/register 会 401。
        if auth.isAuthenticated, !auth.isGuest {
            await push.requestPermissionAndRegister()
        }
    }
}
