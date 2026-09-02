import SwiftUI

/// 地图底部浮层的左右内缩。
///
/// 取值对齐**浮动 tab bar 的左右边界**——iOS 26 的 tab bar 不再贴边，而是一颗
/// 内缩的胶囊。底部这几行控件若还按 12pt 贴着屏幕边，会比 tab bar 各突出一截，
/// 三条边界参差不齐。
///
/// 系统没有公开这个内缩值，所以这里是**目测对齐**的常量，不是读来的。
/// 哪天 tab bar 的内缩变了，改这一处。
enum MapLayout {
    static let horizontalInset: CGFloat = 20
}

/// 地图筛选：状态 chip 条 + 其余条件的 sheet。
///
/// 为什么状态筛选长成「图例」的样子
/// --------------------------------
/// 颜色、名称、数量、开关四件事挤在同一个控件里。分成「图例」和「筛选器」两块的
/// 话，用户得先看懂颜色，再去别处找对应的开关。
///
/// 数字很重要：生产实测 235 条里 Occupied 117、Reserved 48——不把数字摆出来，
/// 「这张图七成是租不到的」这件事就只能靠用户自己数。
struct MapStatusChips: View {
    @Environment(MapStore.self) private var store

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            GlassGroup(spacing: 6) {
                HStack(spacing: 6) {
                    ForEach(ListingStatus.byPriority) { kind in
                        let count = store.statusCounts[kind] ?? 0
                        // 「未知状态」只在真的出现时才占位置——它默认开着，平时是 0，
                        // 常驻一个空 chip 只是噪音；真冒出来时反而最该被看见。
                        if kind != .other || count > 0 {
                            chip(kind, count: count)
                        }
                    }
                }
            }
        }
        // 不加 scrollClipDisabled：加了 chip 会画到 ScrollView 边界之外，被屏幕
        // 边缘从字中间硬切开，看着像布局坏了而不是「可以左右滑」。
        .padding(.horizontal, MapLayout.horizontalInset)
    }

    private func chip(_ kind: ListingStatus, count: Int) -> some View {
        let on = store.activeStatuses.contains(kind)
        return Button {
            if on { store.activeStatuses.remove(kind) }
            else  { store.activeStatuses.insert(kind) }
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(on ? kind.color : Color.secondary.opacity(0.55))
                    .frame(width: 8, height: 8)
                Text(kind.label)
                    .font(.system(size: 14.5, weight: on ? .semibold : .medium))
                    .fixedSize()
                Text("\(count)")
                    .font(.system(size: 12.5, weight: .bold))
                    .monospacedDigit()
                    .fixedSize()
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background((on ? kind.color : Color.primary).opacity(0.14),
                                in: Capsule())
            }
            // 选中态只给**文字**上色，玻璃保持中性。
            //
            // 之前是给玻璃 tint：绿橙蓝紫灰五档底色亮度差很多，前景色只好一档
            // 一档去凑，凑到最后是「颜色太浓」和「数字看不见」。文字上色没有这个
            // 问题——色相由状态决定，对比度由系统的中性玻璃保证，两件事解耦。
            .foregroundStyle(on ? kind.color : Color.secondary)
            .padding(.horizontal, 13)
            .padding(.vertical, 9)
            .liquidGlass(Capsule(), interactive: true)
            .opacity(on ? 1 : 0.8)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(kind.label), \(count) listings")
        .accessibilityValue(on ? "Shown" : "Hidden")
        .accessibilityHint("Double tap to toggle")
    }
}

/// 城市 / 平台 / 租金 / 面积。状态那四五档留在图上的 chip 条里，
/// 因为它是最常动的一个，塞进 sheet 等于每次都要多两步。
struct MapFilterSheet: View {
    @Environment(MapStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var store = store
        NavigationStack {
            Form {
                Section {
                    Picker("City", selection: $store.cityFilter) {
                        Text("All").tag("")
                        ForEach(store.cityOptions, id: \.self) { Text($0).tag($0) }
                    }
                    Picker("Platform", selection: $store.sourceFilter) {
                        Text("All").tag("")
                        ForEach(store.sourceOptions, id: \.self) {
                            Text(Platform.displayName($0)).tag($0)
                        }
                    }
                }

                Section {
                    LabeledContent("Max rent") {
                        TextField("Any", text: $store.maxRentText)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    }
                    LabeledContent("Min area") {
                        TextField("Any", text: $store.minAreaText)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                    }
                } footer: {
                    // 读不出价格 ≠ 超预算。说清楚，免得用户以为漏了。
                    Text("Listings whose rent or area cannot be read are kept rather than hidden.")
                }

                Section {
                    Button("Reset", role: .destructive) { store.resetFilters() }
                }
            }
            .navigationTitle("Filter map")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
