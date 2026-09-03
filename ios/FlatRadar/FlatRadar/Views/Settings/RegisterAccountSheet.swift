import SwiftUI

/// 访客模式下从设置页直接注册账号。
///
/// 为什么单独做一页
/// ----------------
/// 访客是纯本地状态（``AuthStore/enterAsGuest()``），手里没有 token。要变成
/// 真账号，此前唯一的路是「退出访客模式」回到登录页再输一遍——而退出这个动作
/// 看起来像是要把人赶出去，没人会为了注册去点它。
///
/// 条款同意
/// --------
/// 与登录页同一条规矩：注册按钮上方就写着同意条款，按下去即为凭证。后端
/// ``_register`` 不校验这个字段（App 端没有 Web 那套表单），所以它必须在界面上
/// 说清楚，不能只写在别处。
struct RegisterAccountSheet: View {
    @Environment(AuthStore.self) private var auth
    @Environment(PushStore.self) private var push
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var password = ""
    @FocusState private var focused: Field?

    private enum Field { case name, password }

    /// 与后端 ``_register`` 的校验对齐：≥2 字符、≥4 位密码、不可用 `__` 开头。
    /// 客户端先挡一道只是为了即时反馈，服务端仍会重做。
    private var validationError: LocalizedStringKey? {
        let n = name.trimmingCharacters(in: .whitespaces)
        if n.isEmpty || password.isEmpty { return nil }   // 还没填完不报错
        if n.count < 2 { return "Username must be at least 2 characters." }
        if n.lowercased().hasPrefix("__") { return "That username isn't available." }
        if password.count < 4 { return "Password must be at least 4 characters." }
        return nil
    }

    private var canSubmit: Bool {
        let n = name.trimmingCharacters(in: .whitespaces)
        return n.count >= 2 && password.count >= 4
            && validationError == nil && !auth.isLoading
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Username", text: $name)
                        .textContentType(.username)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .focused($focused, equals: .name)
                        .submitLabel(.next)
                        .onSubmit { focused = .password }
                    SecureField("Password", text: $password)
                        .textContentType(.newPassword)
                        .focused($focused, equals: .password)
                        .submitLabel(.go)
                        .onSubmit { Task { await submit() } }
                } footer: {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("An account keeps your notification filter and alert history, and lets FlatRadar push new listings to you.")
                        Text("By creating an account you agree to the Terms of Use and Privacy Policy.")
                    }
                }

                if let validationError {
                    Section {
                        Text(validationError)
                            .font(.subheadline).foregroundStyle(.red)
                    }
                }

                if let err = auth.errorMessage {
                    Section {
                        Text(verbatim: err)
                            .font(.subheadline).foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Create Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { Task { await submit() } }
                        .disabled(!canSubmit)
                }
            }
            .overlay {
                if auth.isLoading {
                    ProgressView()
                        .padding()
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
            }
            .onAppear { focused = .name }
        }
    }

    private func submit() async {
        guard canSubmit else { return }
        let n = String(name.trimmingCharacters(in: .whitespaces).prefix(64))

        // 与 LoginView 同一处理：register 内部登录成功会让 isAuthenticated 变化，
        // ContentView 的 onChange 立刻读 pending，所以必须先写。
        if BiometricAuthService.isAvailable, !BiometricAuthService.hasStoredCredentials {
            auth.pendingBiometricCredential = (n, password, "user")
        }

        await auth.register(name: n, password: password)

        guard auth.isUser else {
            // 失败：错误已由 AuthStore 记在 errorMessage 上，留在本页让用户改。
            auth.pendingBiometricCredential = nil
            return
        }
        await push.requestPermissionAndRegister()
        dismiss()
    }
}
