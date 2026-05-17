import SwiftUI

// MARK: - In-app feedback submission

struct FeedbackView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(\.dismiss) private var dismiss

    @State private var kind = "suggestion"
    @State private var message = ""
    @State private var isSubmitting = false
    @State private var showError = false
    @State private var errorText = ""
    @State private var showSuccess = false

    private let kinds: [(id: String, icon: String, label: String)] = [
        ("suggestion", "lightbulb.fill", "Suggestion"),
        ("bug",        "ant.fill",        "Bug Report"),
        ("other",      "ellipsis",        "Other"),
    ]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Type", selection: $kind) {
                        ForEach(kinds, id: \.id) { k in
                            Label(k.label, systemImage: k.icon).tag(k.id)
                        }
                    }
                    .pickerStyle(.segmented)
                }
                .listRowBackground(Color.clear)

                Section {
                    TextField("What's on your mind?", text: $message, axis: .vertical)
                        .lineLimit(6...12)
                } header: {
                    Text("Message")
                } footer: {
                    HStack {
                        Spacer()
                        Text("\(message.count)/2000")
                            .font(.caption)
                            .foregroundStyle(message.count > 1900 ? .red : .secondary)
                    }
                }

                Section {
                    Button {
                        submit()
                    } label: {
                        HStack(spacing: 6) {
                            if isSubmitting {
                                ProgressView().controlSize(.small)
                            }
                            Text(isSubmitting ? "Sending…" : "Send Feedback")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .disabled(message.trimmingCharacters(in: .whitespacesAndNewlines).count < 5
                              || isSubmitting
                              || message.count > 2000)
                }
            }
            .navigationTitle("Feedback")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .alert("Error", isPresented: $showError) {
                Button("OK") {}
            } message: {
                Text(errorText)
            }
            .alert("Thank you!", isPresented: $showSuccess) {
                Button("Done") { dismiss() }
            } message: {
                Text("Your feedback has been submitted. I read every piece of feedback — it directly shapes what gets built next.")
            }
        }
    }

    private func submit() {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 5 else { return }

        isSubmitting = true
        Task {
            defer { isSubmitting = false }
            do {
                let userName = auth.userInfo?.name ?? ""
                _ = try await APIClient.shared.submitFeedback(
                    kind: kind,
                    message: trimmed,
                    userName: userName,
                    appVersion: AppVersion.short
                )
                showSuccess = true
            } catch {
                errorText = error.localizedDescription
                showError = true
            }
        }
    }
}
