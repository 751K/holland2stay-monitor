import SwiftUI
import UIKit

// MARK: - Onboarding view (first-launch feature walkthrough)

struct OnboardingView: View {
    let onComplete: () -> Void

    @State private var step = 0
    /// "减弱动态效果"开关。开启时跳过 TabView .page 滑动 + spring 弹簧，
    /// 改用瞬时切换。遵守 iOS HIG：前庭功能敏感的用户开了这个 flag 后
    /// 不应再被快速横向滑动 / 弹簧反馈打扰。
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// 翻页动画：reduceMotion 时返回 nil（withAnimation 用 .default 等于无动效），
    /// 否则用与全局一致的 spring 0.3。
    private var pageAnimation: Animation? {
        reduceMotion ? nil : .spring(duration: 0.3)
    }

    /// 首次启动的功能引导。
    ///
    /// 两件之前错的事
    /// --------------
    /// 1. 第一页写着"用顶部的分段选择器切换视图"——分段选择器**只有 iPad 有**
    ///    （见 ``BrowseView`` 的 `usesInlineModePicker`），iPhone 是左上角的
    ///    菜单。新用户被指去找一个不存在的控件。现在按设备分别措辞。
    /// 2. 整组文案原本声明成 `String`，``Text`` 因此走非本地化重载，一条都没
    ///    进过 `Localizable.xcstrings`。西/荷/简中/繁中用户第一次打开 App 看到
    ///    四页英文，然后进入一个已经翻译好的界面。改成 ``LocalizedStringKey``。
    ///
    /// 另外补了最要紧的一页：**这个 App 聚合了哪些平台**。此前四页一个字都没提，
    /// 而这是它与直接刷某个平台官网的全部区别。平台数取
    /// ``Platform/knownKeys`` 的个数，不写死——接新平台时这句话跟着变。
    private var pages: [OnboardingPage] {
        [
            .init(
                icon: "square.stack.3d.up.fill",
                iconColor: .blue,
                title: "Every Platform, One Place",
                body: "FlatRadar watches \(Platform.knownKeys.count) Dutch rental platforms and gathers their listings into a single feed.\nEvery listing is labelled with the platform it came from."
            ),
            .init(
                icon: "square.grid.2x2.fill",
                iconColor: .indigo,
                title: "Browse Listings",
                // 分段选择器只有 iPad 有；iPhone 是 nav bar 左上角的菜单。
                body: usesInlineModePicker
                    ? "List, Map, and Calendar show the same listings from different angles.\nSwitch between them with the picker at the top."
                    : "List, Map, and Calendar show the same listings from different angles.\nSwitch between them from the menu at the top left."
            ),
            .init(
                icon: "line.3.horizontal.decrease.circle.fill",
                iconColor: .orange,
                title: "Filter & Search",
                body: "Narrow by city, platform, status, type, or energy label.\nUse search to find a specific address.\nActive filters appear as chips — tap to remove."
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
            ),
        ]
    }

    /// 与 ``BrowseView`` 同一条判据——两处说的必须是同一件事。
    private var usesInlineModePicker: Bool {
        UIDevice.current.userInterfaceIdiom == .pad
    }

    var body: some View {
        VStack(spacing: 0) {
            // Top bar
            HStack {
                if step > 0 {
                    Button("Back") { withAnimation(pageAnimation) { step -= 1 } }
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
            // reduceMotion 时禁用 step 切换的 spring 动画——TabView 内部的 swipe
            // gesture 我们无法关，但通过编程切换 step（Back / Next 按钮）此时
            // 走 nil 动画 = 瞬时切换，遵守 iOS HIG。
            .animation(reduceMotion ? nil : .spring(duration: 0.35), value: step)

            Spacer(minLength: 0)

            // Bottom button — label 本身撑满蓝色区域，整条都可点击
            Button {
                if step < pages.count - 1 {
                    withAnimation(pageAnimation) { step += 1 }
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
    /// `LocalizedStringKey` 而不是 `String`：后者会让 ``Text`` 走非本地化重载，
    /// 整组引导文案就此从 `Localizable.xcstrings` 里消失。
    let title: LocalizedStringKey
    let body: LocalizedStringKey
}
