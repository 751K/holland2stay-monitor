import SwiftUI

/// 单个过滤维度的取值选择页 —— ``FilterEditView`` 每个多选维度推一层。
///
/// 为什么从 Form 里拆出来
/// ----------------------
/// 原本十一个维度全部平铺在一张 ``Form`` 里，每个取值一行 `Toggle`。城市和街区
/// 各自可能有几十项，整张表单要滚很久才能翻到底部的 "Reset"，而且哪些维度折叠、
/// 哪些不折叠取决于 `choices.count > 6` 这条阈值——同一个界面里一半展开一半收起，
/// 没有规律可循。
///
/// 现在每个维度在主表单里只占一行，右侧写当前选了什么，点进来才是取值列表。
///
/// 三件原来做不到的事
/// ------------------
/// 1. **搜索**：取值多于八项时给 `.searchable`，找 "Strijp-S" 不用滚。
/// 2. **清空**：一键清掉本维度，而不是逐个取消勾选。
/// 3. **看得见已失效的取值**：用户存下来的值可能已经不在后端的候选里
///    （平台改了写法、该取值的房源全下架）。这些值仍然**在过滤**，但旧界面
///    只渲染 `choices`，它们既不显示也删不掉——用户看到的选择和实际生效的
///    过滤条件不是同一份。这里把它们并进列表并单独标注。
struct FilterChoiceList: View {
    let title: LocalizedStringKey
    let choices: [String]
    @Binding var selection: [String]

    /// 该维度对哪些平台生效（``FilterOptions.dimSources``）。空 = 不作标注。
    var appliesTo: [String] = []
    /// 用户当前勾选的平台，用于判断"这条维度对你选的平台一个都不生效"。
    var selectedSources: [String] = []
    /// 该维度取值本身需要解释时补一句。目前只有 Types 用得上——见 ``FilterEditView``。
    var hint: LocalizedStringKey?

    @State private var query = ""

    /// 候选 + 用户已选但候选里没有的值。后者排在末尾，不参与搜索排序。
    private var allChoices: [String] {
        let known = Set(choices)
        return choices + selection.filter { !known.contains($0) }
    }

    private var stale: Set<String> {
        let known = Set(choices)
        return Set(selection.filter { !known.contains($0) })
    }

    private var filtered: [String] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return allChoices }
        return allChoices.filter {
            $0.localizedCaseInsensitiveContains(q)
                || FeatureText.display($0).localizedCaseInsensitiveContains(q)
        }
    }

    var body: some View {
        Group {
            if allChoices.count > 8 {
                list.searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always))
            } else {
                list
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var list: some View {
        List {
            Section {
                if filtered.isEmpty {
                    Group {
                        if query.isEmpty {
                            Text("No options available")
                        } else {
                            Text("No match")
                        }
                    }
                    .foregroundStyle(.secondary)
                } else {
                    ForEach(filtered, id: \.self) { choice in
                        row(choice)
                    }
                }
            } footer: {
                footer
            }

            // 清空放在列表末尾：它是收尾动作，不是入口。摆在顶部会把第一屏
            // 最显眼的位置让给一个破坏性操作，而用户来这一页是为了挑取值。
            if !selection.isEmpty {
                Section {
                    Button(role: .destructive) {
                        selection = []
                    } label: {
                        Text("Clear selection")
                    }
                } footer: {
                    Text("Nothing selected = this condition is not applied.")
                }
            }
        }
    }

    private func row(_ choice: String) -> some View {
        let isOn = selection.contains(choice)
        return Button {
            toggle(choice)
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    // 只改显示。勾选、比对、回传用的都是后端原值——把
                    // "student only" 大写后存回去，后端白名单就匹配不上了。
                    Text(verbatim: FeatureText.display(choice))
                        .foregroundStyle(.primary)
                    if stale.contains(choice) {
                        Text("No current listings use this value")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 8)
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Color.accentColor)
                    .opacity(isOn ? 1 : 0)
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isOn ? [.isSelected] : [])
    }

    @ViewBuilder
    private var footer: some View {
        if let hint {
            Text(hint)
        }
        if let note = PlatformScope.note(appliesTo: appliesTo, selectedSources: selectedSources) {
            Label {
                Text(verbatim: note.text)
            } icon: {
                Image(systemName: note.isWarning ? "exclamationmark.triangle.fill" : "info.circle")
            }
                .foregroundStyle(note.isWarning ? Color.orange : Color.secondary)
        }
    }

    private func toggle(_ choice: String) {
        if let idx = selection.firstIndex(of: choice) {
            selection.remove(at: idx)
        } else {
            selection.append(choice)
        }
    }
}
