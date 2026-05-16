import SwiftUI

/// 当前 user 的 ``ListingFilter`` 编辑表单。
///
/// 设计
/// ----
/// - 进来时用 ``AuthStore.userInfo.listingFilter`` 初始化本地副本
///   （副本：编辑不立即反映，保存才提交）
/// - 字段分组：核心 4 项（rent/area/floor/energy）+ 城市多选 + reset 按钮
/// - 城市候选从后端 ``/stats/public/charts/city_dist`` 拉，避免要求用户手输
/// - 保存：PUT /me/filter，成功后 ``AuthStore.updateLocalFilter`` 同步 UserInfo，
///   关闭 sheet
///
/// Phase 5 MVP 只暴露最常用的 5 个维度。后续可补 occupancy / contract / tenant
/// / type / neighborhood / finishing 等长尾字段。
struct FilterEditView: View {
    @Environment(AuthStore.self) private var auth
    @Environment(MeFilterStore.self) private var saveStore
    @Environment(\.dismiss) private var dismiss

    // 本地编辑副本
    @State private var draft = ListingFilter.empty
    // 城市选择候选（从后端 city_dist 拉）
    @State private var availableCities: [String] = []
    // 数值输入用 String 中介，避免 nil/0 区分难题
    @State private var maxRentText = ""
    @State private var minAreaText = ""
    @State private var minFloorText = ""
    @State private var showResetConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                priceSection
                citiesSection
                energySection
                advancedSection
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
                await loadCities()
            }
            .confirmationDialog(
                "Reset filter to none?",
                isPresented: $showResetConfirm,
                titleVisibility: .visible
            ) {
                Button("Reset", role: .destructive) {
                    draft = .empty
                    maxRentText = ""
                    minAreaText = ""
                    minFloorText = ""
                }
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
            Text("Price & Space")
        } footer: {
            Text("Empty = no limit. Floor 0 = ground floor.")
        }
    }

    @ViewBuilder
    private var citiesSection: some View {
        Section {
            if availableCities.isEmpty {
                ProgressView().padding(.vertical, 4)
            } else {
                ForEach(availableCities, id: \.self) { city in
                    Toggle(isOn: Binding(
                        get: { draft.allowedCities.contains(city) },
                        set: { add in
                            if add {
                                if !draft.allowedCities.contains(city) {
                                    draft.allowedCities.append(city)
                                }
                            } else {
                                draft.allowedCities.removeAll { $0 == city }
                            }
                        }
                    )) {
                        Text(city)
                    }
                }
            }
        } header: {
            HStack {
                Text("Cities")
                Spacer()
                Text("\(draft.allowedCities.count) selected")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } footer: {
            Text("Select one or more. Empty = match all cities.")
        }
    }

    private var energySection: some View {
        Section {
            Picker("Min energy label", selection: $draft.allowedEnergy) {
                Text("Any").tag("")
                ForEach(energyLabels, id: \.self) { label in
                    Text(label).tag(label)
                }
            }
        } header: {
            Text("Energy")
        } footer: {
            Text("Min A means A is okay but A+/A++/A+++ are also accepted. Anything below the threshold is filtered out.")
        }
    }

    private var advancedSection: some View {
        Section {
            Button(role: .destructive) {
                showResetConfirm = true
            } label: {
                Label("Reset All Filters", systemImage: "arrow.counterclockwise")
            }
        }
    }

    // MARK: - Lifecycle helpers

    private func loadFromAuth() {
        if let f = auth.userInfo?.listingFilter {
            draft = f
            maxRentText = f.maxRent.map { String(Int($0)) } ?? ""
            minAreaText = f.minArea.map { String(Int($0)) } ?? ""
            minFloorText = f.minFloor.map { String($0) } ?? ""
        }
    }

    private func loadCities() async {
        do {
            let chart = try await APIClient.shared.getPublicChart(key: "city_dist", days: 30)
            availableCities = chart.data.map(\.label).filter { !$0.isEmpty }.sorted()
        } catch {
            // 城市拉取失败不致命；用户仍可改其它字段
            print("[FilterEditView] loadCities error: \(error)")
        }
    }

    private func save() async {
        // 数值输入 → ListingFilter
        draft.maxRent = Double(maxRentText.trimmingCharacters(in: .whitespaces))
        draft.minArea = Double(minAreaText.trimmingCharacters(in: .whitespaces))
        draft.minFloor = Int(minFloorText.trimmingCharacters(in: .whitespaces))

        guard let resp = await saveStore.save(draft) else {
            // 保留 sheet，错误显示在表单底部
            return
        }
        // 后端返回的是规范化版本——同步到 AuthStore
        auth.updateLocalFilter(resp.filter)
        dismiss()
    }
}
