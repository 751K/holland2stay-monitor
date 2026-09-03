import SwiftUI

/// 在设置页开启 Face ID / Touch ID 登录。
///
/// 为什么要重新输一次密码
/// ----------------------
/// ``BiometricAuthService.saveCredentials`` 存进 Keychain 的是**明文密码**
/// （日后由生物认证解锁，回放给 `/auth/login`）。密码只在登录那一刻存在于
/// 客户端内存里，设置页手里只有 token——所以从设置页开启，只能让用户再输一次。
///
/// 密码不发给别处：先调 ``/auth/verify`` 确认（该端点只回答对不对，不签发
/// token），确认通过才写进 Keychain。
///
/// 这一页存在之前
/// --------------
/// 设置页那一行是个开关，而它的 `set` 闭包只处理"关"：往开的方向拨什么都不
/// 写，`get` 再读一次仍是 false，开关弹回原位，没有任何提示。开启的唯一入口
/// 是登录成功后那次弹窗，而那个弹窗有 `!hasStoredCredentials` 的闸、"Not Now"
/// 又不留任何标记——点过一次 Not Now 的用户，除非手动退出登录，再也没有办法
/// 开启 Face ID。
struct EnableBiometricSheet: View {
    @Environment(AuthStore.self) private var auth
    @Environment(\.dismiss) private var dismiss

    @State private var password = ""
    @State private var inlineError: String?
    @FocusState private var focused: Bool

    private var biometryName: String { BiometricAuthService.biometryName }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("Password", text: $password)
                        .textContentType(.password)
                        .focused($focused)
                        .submitLabel(.go)
                        .onSubmit { Task { await submit() } }
                } footer: {
                    Text("Your password is stored in the device Keychain, protected by \(biometryName). It never leaves this device.")
                }

                if let inlineError {
                    Section {
                        Text(verbatim: inlineError)
                            .font(.subheadline)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Turn on \(biometryName)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Turn On") { Task { await submit() } }
                        .disabled(password.isEmpty || auth.isLoading)
                }
            }
            .overlay {
                if auth.isLoading {
                    ProgressView()
                        .padding()
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
            }
            .onAppear { focused = true }
        }
    }

    private func submit() async {
        guard !password.isEmpty else { return }
        inlineError = nil

        guard await auth.verifyPassword(password) else {
            // verifyPassword 返回 false 可能是密码错，也可能是断网。把
            // AuthStore 记下的原因原样带出来，不要一律说成"密码错误"。
            inlineError = auth.errorMessage
                ?? String(localized: "Couldn't verify your password. Please try again.")
            return
        }

        guard let name = auth.userInfo?.name else {
            inlineError = String(localized: "Couldn't read your account name.")
            return
        }

        do {
            try BiometricAuthService.saveCredentials(
                .init(username: name, password: password, role: "user"))
        } catch {
            // Keychain 写失败必须说出来。原先 ContentView 那处用的是 `try?`，
            // 存不进去时用户以为开好了，下次打开 App 才发现 Face ID 按钮没出现。
            inlineError = error.localizedDescription
            return
        }
        password = ""
        dismiss()
    }
}
