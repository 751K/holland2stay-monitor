import SwiftUI

struct ContentView: View {
    @Environment(AuthStore.self) private var auth
    @AppStorage("terms_accepted") private var termsAccepted = false
    @AppStorage("onboarding_completed") private var onboardingCompleted = false
    @State private var showTerms = false
    @State private var showOnboarding = false

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
        }
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
