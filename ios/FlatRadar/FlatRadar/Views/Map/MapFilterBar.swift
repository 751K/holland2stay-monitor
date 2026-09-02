import SwiftUI

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
        @Bindable var store = store
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(ListingStatus.byPriority) { kind in
                    let count = store.statusCounts[kind] ?? 0
                    // 「未知状态」只在真的出现时才占位置——它默认开着，平时是 0，
                    // 常驻一个空 chip 只是噪音；真冒出来时反而最该被看见。
                    if kind != .other || count > 0 {
                        chip(kind, count: count)
                    }
                }
            }
            .padding(.horizontal, 12)
        }
        .scrollClipDisabled()
    }

    private func chip(_ kind: ListingStatus, count: Int) -> some View {
        let on = store.activeStatuses.contains(kind)
        return Button {
            if on { store.activeStatuses.remove(kind) }
            else  { store.activeStatuses.insert(kind) }
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(on ? kind.color : Color.secondary)
                    .frame(width: 9, height: 9)
                Text(kind.label)
                    .font(.system(size: 13, weight: .medium))
                Text("\(count)")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.regularMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(
                on ? kind.color.opacity(0.55) : Color.clear, lineWidth: 1.5))
            .opacity(on ? 1 : 0.5)
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
