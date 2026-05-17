import SwiftUI

// MARK: - Onboarding view (first-launch feature walkthrough)

struct OnboardingView: View {
    let onComplete: () -> Void

    @State private var step = 0

    private let pages: [OnboardingPage] = [
        .init(
            icon: "square.grid.2x2.fill",
            iconColor: .blue,
            title: "Browse Listings",
            body: "Switch between List, Map, and Calendar views\nusing the segmented picker at the top.\nEach view shows the same listings — just a different perspective."
        ),
        .init(
            icon: "line.3.horizontal.decrease.circle.fill",
            iconColor: .orange,
            title: "Filter & Search",
            body: "Tap the filter button to narrow by city, status, type, or energy label.\nUse search to find a specific address.\nActive filters appear as chips — tap to remove."
        ),
        .init(
            icon: "chart.bar.fill",
            iconColor: .green,
            title: "Explore Stats",
            body: "Dashboard cards are interactive.\nTap any mini chart to drill into daily trends, price distributions, or energy labels.\nThe \"New · 24h\" stat shows real matching listings."
        ),
        .init(
            icon: "bell.badge.fill",
            iconColor: .red,
            title: "Stay Updated",
            body: "Set a notification filter in Settings to receive push alerts for new listings that match your criteria.\nThe Alerts tab shows a live stream of every match."
        )
    ]

    var body: some View {
        VStack(spacing: 0) {
            // Top bar
            HStack {
                if step > 0 {
                    Button("Back") { withAnimation(.spring(duration: 0.3)) { step -= 1 } }
                        .font(.subheadline.weight(.medium))
                } else {
                    Spacer().frame(height: 1)
                }

                Spacer()

                if step < pages.count - 1 {
                    Button("Skip") { finish() }
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .frame(height: 44)

            Spacer(minLength: 0)

            // Page content
            TabView(selection: $step) {
                ForEach(Array(pages.enumerated()), id: \.offset) { idx, page in
                    pageCard(page, index: idx)
                        .tag(idx)
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .always))
            .animation(.spring(duration: 0.35), value: step)

            Spacer(minLength: 0)

            // Bottom button — label 本身撑满蓝色区域，整条都可点击
            Button {
                if step < pages.count - 1 {
                    withAnimation(.spring(duration: 0.3)) { step += 1 }
                } else {
                    finish()
                }
            } label: {
                Text(step < pages.count - 1 ? "Next" : "Get Started")
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(.blue, in: RoundedRectangle(cornerRadius: 14))
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 24)
            .padding(.bottom, 36)
        }
        .background(Color(.systemGroupedBackground))
    }

    private func pageCard(_ page: OnboardingPage, index: Int) -> some View {
        VStack(spacing: 0) {
            Spacer()

            ZStack {
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .fill(page.iconColor.opacity(0.12))
                    .frame(width: 120, height: 120)
                Image(systemName: page.icon)
                    .font(.system(size: 48, weight: .medium))
                    .foregroundStyle(page.iconColor)
            }

            Text(page.title)
                .font(.title.weight(.bold))
                .padding(.top, 36)

            Text(page.body)
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineSpacing(5)
                .padding(.top, 12)
                .padding(.horizontal, 32)

            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private func finish() {
        onComplete()
    }
}

private struct OnboardingPage {
    let icon: String
    let iconColor: Color
    let title: String
    let body: String
}
