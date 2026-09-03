import SwiftUI

/// 当前 user 的 ``ListingFilter`` 编辑表单 —— 与网页端 user_form.html 维度对齐。
///
/// 布局
/// ----
/// 十一个多选维度每个只占主表单一行，右侧显示当前选了什么，点进
/// ``FilterChoiceList`` 才是取值列表。改版前它们是十一个平铺的 Section、每个取值
/// 一行 Toggle，主表单长到要滚十几屏，而且 `choices.count > 6` 这条阈值让一半
/// 维度默认折叠、一半默认展开。
///
/// 分组按"用户在想什么"而不是按后端字段顺序：
/// 1. **Price & Space** —— 先决条件，绝大多数人只改这里
/// 2. **Location** —— 城市 / 街区
/// 3. **Platforms** —— 房源来源（七项，直接内联，它是其它维度的前提）
/// 4. **Property** —— 房型 / 装修 / 能耗
/// 5. **Eligibility** —— 租客 / 入住人数 / 合同
/// 6. **Perks** —— 优惠
/// 7. **Reset All**
///
/// 平台适用范围
/// ------------
/// 每个维度带一句"这条对哪些平台生效"（见 ``PlatformScope``）。Contract /
/// Neighborhood / Offer 七个平台里只对 Holland2Stay 生效，此前界面上没有任何
/// 地方说过——用户设了以为是全局条件，其实只约束了七分之一的来源。
///
/// 后端 ``_coerce_filter_payload`` 会做白名单 + 边界校验，少传字段不会报错。
struct FilterEditView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(MeFilterStore.self) private var saveStore
    @Environment(\.dismiss) private var dismiss

    // 本地编辑副本
    @State private var draft = ListingFilter.empty
    @State private var options = FilterOptions.empty
    @State private var loadingOptions = false

    // 数值输入用 String 中介
    @State private var maxRentText = ""
    @State private var minAreaText = ""
    @State private var minFloorText = ""

    /// 打开时的快照，用于判断"有没有改过"。
    @State private var baseline = ListingFilter.empty
    @State private var baselineNumbers: [String] = ["", "", ""]

    @State private var showResetConfirm = false
    @State private var showDiscardConfirm = false
    @FocusState private var focusedNumber: NumberField?

    private enum NumberField: Hashable { case rent, area, floor }

    var body: some View {
        NavigationStack {
            Form {
                summarySection
                priceSection
                locationSection
                platformSection
                propertySection
                eligibilitySection
                perksSection
                resetSection
                if let err = saveStore.errorMessage {
                    Section {
                        Label(err, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .font(.subheadline)
                    }
                }
            }
            .navigationTitle("Notification Filter")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        if hasChanges { showDiscardConfirm = true } else { dismiss() }
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task { await save() }
                    }
                    .disabled(saveStore.isSaving || !numberErrors.isEmpty)
                }
                // numberPad 没有 return 键 —— 不给一个 Done，键盘只能靠点别处收起。
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { focusedNumber = nil }
                }
            }
            .overlay {
                if saveStore.isSaving {
                    ProgressView("Saving…")
                        .padding()
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
            }
            .task {
                loadFromAuth()
                await loadOptions()
            }
            .confirmationDialog(
                "Reset filter to none?",
                isPresented: $showResetConfirm,
                titleVisibility: .visible
            ) {
                Button("Reset", role: .destructive) { resetAll() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("All filters will be cleared. You'll start receiving notifications for every new listing.")
            }
            .confirmationDialog(
                "Discard changes?",
                isPresented: $showDiscardConfirm,
                titleVisibility: .visible
            ) {
                Button("Discard", role: .destructive) { dismiss() }
                Button("Keep Editing", role: .cancel) {}
            }
        }
    }

    // MARK: - Sections

    /// 表单顶部先说清楚"现在这套条件是什么" —— 十一个维度分散在下面七个 Section 里，
    /// 没有这一行，用户得逐个点进去才知道自己到底设了些什么。
    private var summarySection: some View {
        Section {
            HStack(alignment: .top, spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    // 三元里的字面量会被合并成 String，Text 就走非本地化重载，
                    // 这两句会从 Localizable.xcstrings 里消失。分支写。
                    if preview.isEmpty {
                        Text("Every new listing").font(.subheadline.weight(.semibold))
                        Text("No conditions set — you'll be notified about everything.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("\(activeCount) conditions").font(.subheadline.weight(.semibold))
                        Text(verbatim: preview.summary)
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.vertical, 2)
        } footer: {
            Text("A listing must match every condition below to notify you.")
        }
    }

    private var priceSection: some View {
        Section {
            numberRow("Max rent", text: $maxRentText, field: .rent,
                      placeholder: "Any", unit: "€/mo")
            numberRow("Min area", text: $minAreaText, field: .area,
                      placeholder: "Any", unit: "m²")
            numberRow("Min floor", text: $minFloorText, field: .floor,
                      placeholder: "Any", unit: nil)
            if let note = scopeNote(for: "floor"), !minFloorText.isEmpty {
                noteLabel(note)
            }
        } header: {
            Text("Price & Space")
        } footer: {
            if numberErrors.isEmpty {
                Text("Empty = no limit. Floor 0 = ground floor.")
            } else {
                Text(verbatim: numberErrors.joined(separator: "\n")).foregroundStyle(.red)
            }
        }
    }

    private var locationSection: some View {
        Section {
            choiceRow(.cities)
            choiceRow(.neighborhoods)
        } header: {
            Text("Location")
        } footer: {
            if options.neighborhoods.isEmpty && !loadingOptions {
                Text("Neighborhoods appear once listings in your cities have been indexed.")
            }
        }
    }

    private var propertySection: some View {
        Section {
            choiceRow(.types)
            choiceRow(.finishing)
            energyRow
        } header: {
            Text("Property")
        }
    }

    private var eligibilitySection: some View {
        Section {
            choiceRow(.tenant)
            choiceRow(.occupancy)
            choiceRow(.contract)
        } header: {
            Text("Eligibility")
        } footer: {
            Text("Tenant and occupancy are checked strictly: a listing that doesn't state them is filtered out.")
        }
    }

    private var perksSection: some View {
        Section {
            choiceRow(.offer)
        } header: {
            Text("Perks")
        }
    }

    private var energyRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            Picker(selection: $draft.allowedEnergy) {
                Text("Any").tag("")
                ForEach(options.energy.isEmpty ? energyLabels : options.energy, id: \.self) { label in
                    Text(label).tag(label)
                }
            } label: {
                Text("Min energy label")
            }
            .pickerStyle(.menu)
            if !draft.allowedEnergy.isEmpty {
                Text("A/A+/A++/A+++ above \(draft.allowedEnergy) also pass.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let note = scopeNote(for: "energy") { noteLabel(note) }
            }
        }
    }

    /// 平台是所有其它维度的前提（一个维度对哪些平台生效取决于这里选了谁），
    /// 所以七项直接内联，不再推一层。
    private var platformSection: some View {
        Section {
            if loadingOptions && options.sources.isEmpty {
                ProgressView().padding(.vertical, 4)
            } else if options.sources.isEmpty {
                Text("No platforms available").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(options.sources, id: \.self) { source in
                    Toggle(isOn: sourceBinding(source)) {
                        Text(Platform.displayName(source))
                    }
                }
            }
        } header: {
            Text("Platforms")
        } footer: {
            if draft.allowedSources.isEmpty {
                Text("Nothing selected = all platforms can notify you.")
            } else {
                Text("Only these platforms can trigger your notifications.")
            }
        }
    }

    private var resetSection: some View {
        Section {
            Button(role: .destructive) {
                showResetConfirm = true
            } label: {
                Text("Reset All Filters")
            }
            .disabled(preview.isEmpty)
        }
    }

    // MARK: - Row builders

    private func numberRow(
        _ title: LocalizedStringKey,
        text: Binding<String>,
        field: NumberField,
        placeholder: LocalizedStringKey,
        unit: String?
    ) -> some View {
        HStack {
            Text(title)
            Spacer()
            TextField(placeholder, text: text)
                .keyboardType(.numberPad)
                .multilineTextAlignment(.trailing)
                .focused($focusedNumber, equals: field)
                .frame(width: 100)
            if let unit {
                Text(unit).foregroundStyle(.secondary)
            }
        }
    }

    /// 主表单里的一行：维度名 + 当前选了什么 + chevron。
    private func choiceRow(_ dim: FilterDim) -> some View {
        let choices = dim.choices(options)
        let selected = draft[keyPath: dim.path]
        return NavigationLink {
            FilterChoiceList(
                title: dim.title,
                choices: choices,
                selection: binding(dim.path),
                appliesTo: options.dimSources[dim.backendKey] ?? [],
                selectedSources: draft.allowedSources,
                hint: dim.hint(choices))
        } label: {
            HStack {
                Text(dim.title)
                Spacer(minLength: 12)
                Group {
                    if selected.isEmpty {
                        Text("Any").foregroundStyle(.secondary)
                    } else {
                        Text(verbatim: ListingFilter.brief(selected)).foregroundStyle(.primary)
                    }
                }
                .lineLimit(1)
                .truncationMode(.tail)
            }
        }
        .disabled(choices.isEmpty && selected.isEmpty)
    }

    /// 图标只在出问题时出现：``isWarning`` 那一档是"这条筛选对你选的平台
    /// 一个都不生效"，需要跳出来；纯说明性的一档就是一句话，不配图标。
    @ViewBuilder
    private func noteLabel(_ note: PlatformScope.Note) -> some View {
        if note.isWarning {
            Label {
                Text(verbatim: note.text)
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
            }
            .font(.caption)
            .foregroundStyle(Color.orange)
        } else {
            Text(verbatim: note.text)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func scopeNote(for dim: String) -> PlatformScope.Note? {
        PlatformScope.note(appliesTo: options.dimSources[dim] ?? [],
                           selectedSources: draft.allowedSources)
    }

    // MARK: - Bindings

    private func binding(_ path: WritableKeyPath<ListingFilter, [String]>) -> Binding<[String]> {
        Binding(get: { draft[keyPath: path] }, set: { draft[keyPath: path] = $0 })
    }

    private func sourceBinding(_ source: String) -> Binding<Bool> {
        Binding(
            get: { draft.allowedSources.contains(source) },
            set: { add in
                if add {
                    if !draft.allowedSources.contains(source) {
                        draft.allowedSources.append(source)
                    }
                } else {
                    draft.allowedSources.removeAll { $0 == source }
                }
            })
    }

    // MARK: - Derived state

    /// 把三个文本框并进 draft 之后的样子 —— 顶部摘要和 Reset 的可用性都看它，
    /// 否则用户刚输入的 "Max rent 900" 要等保存后才反映到摘要里。
    private var preview: ListingFilter {
        var f = draft
        f.maxRent = Double(maxRentText.trimmingCharacters(in: .whitespaces))
        f.minArea = Double(minAreaText.trimmingCharacters(in: .whitespaces))
        f.minFloor = Int(minFloorText.trimmingCharacters(in: .whitespaces))
        return f
    }

    private var activeCount: Int { preview.summaryParts.count }

    private var hasChanges: Bool {
        draft != baseline
            || [maxRentText, minAreaText, minFloorText] != baselineNumbers
    }

    /// 数字框写了东西但解析不出来 —— 必须拦住。
    ///
    /// 旧的 `save()` 直接 `Double(text)`，"90O"（字母 O）解析成 nil，于是
    /// "最高 €900" 被静默保存成"不限价"，用户从此收到所有价位的推送，界面上
    /// 没有任何迹象。认不出的输入不能当成一个确定的答案。
    private var numberErrors: [String] {
        var errs: [String] = []
        func check(_ text: String, _ name: String, allowZero: Bool) {
            let t = text.trimmingCharacters(in: .whitespaces)
            guard !t.isEmpty else { return }
            guard let v = Double(t) else {
                errs.append("\(name): \"\(t)\" is not a number.")
                return
            }
            if v < 0 || (!allowZero && v == 0) {
                errs.append("\(name) must be greater than 0.")
            }
        }
        check(maxRentText, "Max rent", allowZero: false)
        check(minAreaText, "Min area", allowZero: false)
        check(minFloorText, "Min floor", allowZero: true)
        return errs
    }

    // MARK: - Lifecycle

    private func loadFromAuth() {
        guard let f = auth.userInfo?.listingFilter else { return }
        draft = f
        maxRentText = f.maxRent.map { String(Int($0)) } ?? ""
        minAreaText = f.minArea.map { String(Int($0)) } ?? ""
        minFloorText = f.minFloor.map { String($0) } ?? ""
        baseline = f
        baselineNumbers = [maxRentText, minAreaText, minFloorText]
    }

    private func loadOptions() async {
        loadingOptions = true
        defer { loadingOptions = false }
        do {
            options = try await APIClient.shared.getFilterOptions()
        } catch {
            #if DEBUG
            print("[FilterEditView] loadOptions error: \(error)")
            #endif
        }
    }

    private func resetAll() {
        draft = .empty
        maxRentText = ""
        minAreaText = ""
        minFloorText = ""
    }

    private func save() async {
        guard numberErrors.isEmpty else { return }
        draft = preview

        guard let resp = await saveStore.save(draft) else {
            return  // 错误显示在表单底部
        }
        auth.updateLocalFilter(resp.filter)
        dismiss()
    }
}

/// 主表单一行对应的维度描述。
///
/// 把「显示名 / 后端维度名 / 候选来自 options 的哪个字段 / 存进 filter 的哪个字段」
/// 四件事绑在一起，因为它们必须一致：`backendKey` 用来查 `dim_sources`，写错了
/// 提示语会指到别的维度上去，而这种错不会有任何报错。
private struct FilterDim {
    let backendKey: String
    let title: LocalizedStringKey
    let choices: (FilterOptions) -> [String]
    let path: WritableKeyPath<ListingFilter, [String]>

    /// 取值本身需要解释时补一句，否则 nil。
    ///
    /// 只有 Types 用得上：Holland2Stay 的房型字段是 ``no_of_rooms``，取值直接
    /// 就是 "1" "2" "3" "4"，和其它平台的 "Studio"、"2-room apartment" 并排
    /// 列在一起，看不出那几个数字是什么意思。
    ///
    /// 只写一句说明，不把 "2" 改写成 "2 rooms"：勾选和回传用的是后端原值，
    /// 显示与取值一旦分家，五种语言的单复数各写一遍，收益不抵成本；而且
    /// ``no_of_rooms`` 是"房间数"不是"卧室数"，改写措辞就得替平台断言语义。
    func hint(_ choices: [String]) -> LocalizedStringKey? {
        guard backendKey == "type" else { return nil }
        let hasBareNumber = choices.contains { !$0.isEmpty && $0.allSatisfy(\.isNumber) }
        return hasBareNumber ? "Plain numbers are room counts." : nil
    }

    static let cities = FilterDim(
        backendKey: "city", title: "Cities",
        choices: { $0.cities }, path: \.allowedCities)
    static let neighborhoods = FilterDim(
        backendKey: "neighborhood", title: "Neighborhoods",
        choices: { $0.neighborhoods }, path: \.allowedNeighborhoods)
    static let types = FilterDim(
        backendKey: "type", title: "Types",
        choices: { $0.types }, path: \.allowedTypes)
    static let finishing = FilterDim(
        backendKey: "finishing", title: "Finishing",
        choices: { $0.finishing }, path: \.allowedFinishing)
    static let tenant = FilterDim(
        backendKey: "tenant", title: "Tenant",
        choices: { $0.tenant }, path: \.allowedTenant)
    static let occupancy = FilterDim(
        backendKey: "occupancy", title: "Occupancy",
        choices: { $0.occupancy }, path: \.allowedOccupancy)
    static let contract = FilterDim(
        backendKey: "contract", title: "Contract",
        choices: { $0.contract }, path: \.allowedContract)
    static let offer = FilterDim(
        backendKey: "offer", title: "Offer",
        choices: { $0.offer }, path: \.allowedOffer)
}
