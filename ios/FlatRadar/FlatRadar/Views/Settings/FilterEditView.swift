import SwiftUI

/// 当前 user 的 ``ListingFilter`` 编辑表单 —— 与网页端 user_form.html 维度对齐。
///
/// 字段（与网页端对齐，按使用频率排序）
/// ----------------------------------
/// 1. **Price & Space**：max rent / min area / min floor
/// 2. **Energy** ⚡ —— 单选 picker，min energy label (A+++ 优 → F 差)
/// 3. **Types** 🏠 房型
/// 4. **Contract** 📅 长短租（"Indefinite" / "6 months max" 等）
/// 5. **Tenant** 👥 租客要求（student/employed/...）
/// 6. **Occupancy** 👤 入住人数
/// 7. **Cities** 🌆
/// 8. **Neighborhoods** 📍（可能很长，DisclosureGroup 默认折叠）
/// 9. **Offer** 🎁 优惠
/// 10. **Finishing** 🛋 装修
/// 11. **Reset All**
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

    @State private var showResetConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                priceSection
                energySection
                multiSelect("Types", icon: "house.lodge",
                            choices: options.types,
                            selection: $draft.allowedTypes)
                multiSelect("Contract", icon: "calendar",
                            choices: options.contract,
                            selection: $draft.allowedContract)
                multiSelect("Tenant", icon: "person.2.fill",
                            choices: options.tenant,
                            selection: $draft.allowedTenant)
                multiSelect("Occupancy", icon: "person.fill",
                            choices: options.occupancy,
                            selection: $draft.allowedOccupancy)
                multiSelect("Cities", icon: "building.2.fill",
                            choices: options.cities,
                            selection: $draft.allowedCities)
                neighborhoodsSection
                multiSelect("Offer", icon: "tag.fill",
                            choices: options.offer,
                            selection: $draft.allowedOffer)
                multiSelect("Finishing", icon: "sofa.fill",
                            choices: options.finishing,
                            selection: $draft.allowedFinishing)
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
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task { await save() }
                    }
                    .disabled(saveStore.isSaving)
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
        }
    }

    // MARK: - Sections

    private var priceSection: some View {
        Section {
            HStack {
                Text("Max rent")
                Spacer()
                TextField("Any", text: $maxRentText)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 100)
                Text("€/mo").foregroundStyle(.secondary)
            }
            HStack {
                Text("Min area")
                Spacer()
                TextField("Any", text: $minAreaText)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 100)
                Text("m²").foregroundStyle(.secondary)
            }
            HStack {
                Text("Min floor")
                Spacer()
                TextField("Any", text: $minFloorText)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 100)
            }
        } header: {
            Label("Price & Space", systemImage: "eurosign.circle.fill")
        } footer: {
            Text("Empty = no limit. Floor 0 = ground floor.")
        }
    }

    private var energySection: some View {
        Section {
            Picker("Min energy label", selection: $draft.allowedEnergy) {
                Text("Any").tag("")
                ForEach(options.energy.isEmpty ? energyLabels : options.energy, id: \.self) { label in
                    Text(label).tag(label)
                }
            }
            .pickerStyle(.menu)
        } header: {
            Label("Energy", systemImage: "bolt.fill")
        } footer: {
            Text("Min B = A/A+/A++/A+++ also accepted; C and worse filtered out.")
        }
    }

    /// 通用多选 section。空候选时显示 ProgressView（options 还没加载好）。
    /// 候选过多（≥ 6 项）会用 DisclosureGroup 折叠，避免表单太长。
    @ViewBuilder
    private func multiSelect(
        _ title: LocalizedStringKey,
        icon: String,
        choices: [String],
        selection: Binding<[String]>
    ) -> some View {
        let count = selection.wrappedValue.count
        Section {
            if loadingOptions && choices.isEmpty {
                ProgressView().padding(.vertical, 4)
            } else if choices.isEmpty {
                Text("No options available").font(.subheadline).foregroundStyle(.secondary)
            } else if choices.count > 6 {
                DisclosureGroup {
                    multiSelectRows(choices: choices, selection: selection)
                } label: {
                    HStack {
                        Text("\(count) selected")
                        Spacer()
                    }
                }
            } else {
                multiSelectRows(choices: choices, selection: selection)
            }
        } header: {
            HStack {
                Label(title, systemImage: icon)
                Spacer()
                if count > 0 {
                    Text("\(count)")
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(.blue.opacity(0.18), in: Capsule())
                        .foregroundStyle(.blue)
                }
            }
        }
    }

    @ViewBuilder
    private func multiSelectRows(choices: [String], selection: Binding<[String]>) -> some View {
        ForEach(choices, id: \.self) { c in
            Toggle(isOn: Binding(
                get: { selection.wrappedValue.contains(c) },
                set: { add in
                    if add {
                        if !selection.wrappedValue.contains(c) {
                            selection.wrappedValue.append(c)
                        }
                    } else {
                        selection.wrappedValue.removeAll { $0 == c }
                    }
                }
            )) {
                Text(c)
            }
        }
    }

    /// Neighborhoods 一般是空的 / 数量很大 —— 用 DisclosureGroup 默认折叠。
    /// 后端目前 distinct 出来的 neighborhood 只在用户选了 city 后才合理。
    private var neighborhoodsSection: some View {
        Section {
            if options.neighborhoods.isEmpty {
                Text(loadingOptions ? "Loading…" : "Select cities first to see neighborhoods")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                DisclosureGroup {
                    multiSelectRows(choices: options.neighborhoods,
                                    selection: $draft.allowedNeighborhoods)
                } label: {
                    Text("\(draft.allowedNeighborhoods.count) selected")
                }
            }
        } header: {
            Label("Neighborhoods", systemImage: "mappin.and.ellipse")
        }
    }

    private var resetSection: some View {
        Section {
            Button(role: .destructive) {
                showResetConfirm = true
            } label: {
                Label("Reset All Filters", systemImage: "arrow.counterclockwise")
            }
        }
    }

    // MARK: - Lifecycle

    private func loadFromAuth() {
        guard let f = auth.userInfo?.listingFilter else { return }
        draft = f
        maxRentText = f.maxRent.map { String(Int($0)) } ?? ""
        minAreaText = f.minArea.map { String(Int($0)) } ?? ""
        minFloorText = f.minFloor.map { String($0) } ?? ""
    }

    private func loadOptions() async {
        loadingOptions = true
        defer { loadingOptions = false }
        do {
            options = try await APIClient.shared.getFilterOptions()
        } catch {
            print("[FilterEditView] loadOptions error: \(error)")
        }
    }

    private func resetAll() {
        draft = .empty
        maxRentText = ""
        minAreaText = ""
        minFloorText = ""
    }

    private func save() async {
        draft.maxRent = Double(maxRentText.trimmingCharacters(in: .whitespaces))
        draft.minArea = Double(minAreaText.trimmingCharacters(in: .whitespaces))
        draft.minFloor = Int(minFloorText.trimmingCharacters(in: .whitespaces))

        guard let resp = await saveStore.save(draft) else {
            return  // 错误显示在表单底部
        }
        auth.updateLocalFilter(resp.filter)
        dismiss()
    }
}
