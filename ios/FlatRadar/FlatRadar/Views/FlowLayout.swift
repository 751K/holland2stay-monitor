import SwiftUI

/// 会折行的横向排列 —— 装不下就换行，而不是压缩或截断。
///
/// `HStack` 装不下时会挤压子视图或把它们推出边界；把 chip 塞进
/// `ScrollView(.horizontal)` 又要求用户横向滑才能看全，在一张只读的摘要卡片上
/// 不合适。SwiftUI 没有内置的 flow layout，这里用 ``Layout`` 实现一个。
struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    /// 一行：起始下标、元素个数、宽高。
    private struct Line {
        var indices: [Int] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    private func lines(_ subviews: Subviews, maxWidth: CGFloat) -> [Line] {
        var out: [Line] = []
        var line = Line()
        for i in subviews.indices {
            let size = subviews[i].sizeThatFits(.unspecified)
            let advance = line.indices.isEmpty ? size.width : size.width + spacing
            // 一行里至少放一个：单个元素比整行还宽时也不能无限换行。
            if !line.indices.isEmpty && line.width + advance > maxWidth {
                out.append(line)
                line = Line()
                line.indices = [i]
                line.width = size.width
                line.height = size.height
            } else {
                line.indices.append(i)
                line.width += advance
                line.height = max(line.height, size.height)
            }
        }
        if !line.indices.isEmpty { out.append(line) }
        return out
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard !subviews.isEmpty else { return .zero }
        // 提案宽度可能是 nil（未指定）或无穷（滚动容器）——两种都当作"不换行"，
        // 由父视图再给一个确定宽度。此时按一行算，而不是除以零或算出 NaN。
        let maxWidth = (proposal.width ?? .infinity).isFinite ? proposal.width! : .infinity
        let rows = lines(subviews, maxWidth: maxWidth)
        let height = rows.reduce(0) { $0 + $1.height }
            + lineSpacing * CGFloat(max(0, rows.count - 1))
        let width = rows.map(\.width).max() ?? 0
        return CGSize(width: maxWidth.isFinite ? min(maxWidth, width) : width, height: height)
    }

    func placeSubviews(
        in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
    ) {
        let rows = lines(subviews, maxWidth: bounds.width)
        var y = bounds.minY
        for row in rows {
            var x = bounds.minX
            for i in row.indices {
                let size = subviews[i].sizeThatFits(.unspecified)
                subviews[i].place(
                    at: CGPoint(x: x, y: y + (row.height - size.height) / 2),
                    proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + lineSpacing
        }
    }
}
