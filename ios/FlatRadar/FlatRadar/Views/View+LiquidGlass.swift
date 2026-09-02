import SwiftUI

/// Liquid Glass 的统一入口，带**降级**。
///
/// 为什么要包一层
/// --------------
/// `glassEffect` 是 iOS 26 才有的，而本 App 的部署目标是 iOS 18——直接写会编译
/// 不过，写成散落各处的 `if #available` 又会让每个调用点都长出一段分支，改配色
/// 时得记着两边一起改。包成一个修饰符之后，调用点只写「这块要玻璃」，两条路径
/// 的形状（圆角、填充）由这里保证一致。
///
/// 降级到 `.regularMaterial` 而不是纯色：材质本来就是 Liquid Glass 的前身，
/// 观感最接近，也是 iOS 18 上同类浮层的通行做法。
extension View {

    /// 给浮在内容之上的控件加玻璃底。
    ///
    /// - Parameters:
    ///   - shape: 玻璃的形状。圆钮用 `.circle`，chip 用 `.capsule`，卡片用圆角矩形。
    ///   - interactive: 可点的控件设 true——按下时玻璃会跟着形变／高光，
    ///     纯展示的（计数、说明文字）不要开，否则会看起来像能点。
    ///   - tint: 需要着色时传，比如选中的状态 chip。
    @available(iOS 26.0, *)
    fileprivate static func makeGlass(interactive: Bool, tint: Color?) -> Glass {
        var glass = Glass.regular
        if let tint { glass = glass.tint(tint) }
        if interactive { glass = glass.interactive() }
        return glass
    }

    @ViewBuilder
    func liquidGlass(
        _ shape: some Shape,
        interactive: Bool = false,
        tint: Color? = nil
    ) -> some View {
        if #available(iOS 26.0, *) {
            // 构造过程必须放在 @ViewBuilder 之外：ViewBuilder 会把这里的 `if`
            // 当成视图表达式，`glass = ...` 这种赋值语句的类型是 ()，编译期直接
            // 报 "type '()' cannot conform to 'View'"。
            self.glassEffect(Self.makeGlass(interactive: interactive, tint: tint),
                             in: shape)
        } else {
            self.background(tint?.opacity(0.16) ?? Color.clear, in: shape)
                .background(.regularMaterial, in: shape)
        }
    }
}

/// 把相邻的玻璃控件放进同一个容器，它们之间才会互相融合／流动。
///
/// 各自单独 `glassEffect` 的话，几块玻璃只是各画各的，挨在一起时边界生硬；
/// 放进容器才有 Liquid Glass 那种「靠近就连成一片」的效果。iOS 26 以下这个
/// 容器不存在，直接透传内容。
struct GlassGroup<Content: View>: View {
    var spacing: CGFloat = 10
    @ViewBuilder var content: Content

    var body: some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: spacing) { content }
        } else {
            content
        }
    }
}


extension View {
    /// 主操作按钮：iOS 26 用原生的 Liquid Glass 突出样式，更早退回
    /// `.borderedProminent`——两者的角色一致（页面上唯一的主动作）。
    @ViewBuilder
    func glassProminentButtonStyle() -> some View {
        if #available(iOS 26.0, *) {
            self.buttonStyle(.glassProminent)
        } else {
            self.buttonStyle(.borderedProminent)
        }
    }
}
