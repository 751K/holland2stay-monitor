import SwiftUI

/// 仪表盘可点击卡片。
///
/// 之前是纯展示组件；现在挂 ``action``（可选）让 DashboardView 决定点击行为
/// （弹出图表详情 sheet / 切换 tab 等）。``action==nil`` 时仍可正常显示，
/// 没有点击反馈。
struct StatCard: View {
    let title: String
    let value: String
    let systemImage: String
    let color: Color
    var action: (() -> Void)? = nil

    var body: some View {
        Button {
            action?()
        } label: {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Image(systemName: systemImage)
                        .font(.subheadline)
                        .foregroundStyle(color)
                    Spacer()
                    if action != nil {
                        Image(systemName: "chevron.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                // 数值；空串 / "—" 等 placeholder 直接省略（用于 Explore 那种纯导航卡）
                if !value.isEmpty && value != "—" {
                    Text(value)
                        .font(.title2.weight(.bold))
                        .foregroundStyle(.primary)
                }
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.regularMaterial)
            .clipShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
        .disabled(action == nil)
    }
}
