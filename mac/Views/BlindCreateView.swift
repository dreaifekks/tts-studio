import SwiftUI

struct BlindCreateView: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openSettings) private var openSettings
    @Bindable var manager: BlindStore
    var prefill: BlindPrefill? = nil

    @State private var versions = ModelVersionStore()
    @State private var character = ""
    @State private var a = BlindSide(engine: "v2proplus")
    @State private var b = BlindSide(engine: "v2proplus")
    @State private var evalSet = "hard"
    @State private var repeats = 1
    @State private var title = ""
    @State private var error: String?
    @State private var appliedPrefill = false
    @State private var editedA = false
    @State private var editedB = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("新建盲听").font(.title2.bold())
            Text("两组使用同一句台词，左右顺序随机。创建后只显示左右，揭晓时再看模型。").font(.callout).foregroundStyle(.secondary)
            if studio.models.isEmpty {
                ContentUnavailableView("资料库还没有角色", systemImage: "person.crop.circle", description: Text("先在资料库中准备角色，再创建盲听。"))
                Button("查看资料库") { studio.settingsTab = .library; openSettings() }.buttonStyle(.link)
            } else {
                Form {
                    Picker("角色", selection: $character) {
                        Text("选择角色").tag("")
                        ForEach(studio.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
                    }
                    Section("组合 A") { sideFields($a, edited: $editedA) }
                    Section("组合 B") { sideFields($b, edited: $editedB) }
                    Section("题目") {
                        Picker("评测集", selection: $evalSet) {
                            ForEach(manager.evalSets, id: \.name) { Text("\(displayEvalSet($0.name)) · \($0.sentences.count) 句").tag($0.name) }
                        }
                        Stepper("每句重复 \(repeats) 次 · 共 \(items) 题", value: $repeats, in: 1...5)
                        TextField("可选标题", text: $title).onChange(of: title) {
                            if title.unicodeScalars.count > 100 { title = String(title.unicodeScalars.prefix(100)) }
                        }
                    }
                }.formStyle(.grouped).disabled(manager.busy)
            }
            ComputePicker(planner: manager.planner, input: Self.blindPlanInput(engines: [a.engine, b.engine], items: items), action: "出题", taskTitle: "盲听 \(character)", ready: ready)
            if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if manager.busy { ProgressView().controlSize(.small) }
                Button("创建并开始生成") {
                    studio.perform {
                        do {
                            try await manager.create(BlindCreate(character: character, a: a, b: b, eval_set: evalSet, repeats: repeats, title: title.isEmpty ? nil : title))
                            dismiss()
                        } catch let failure as APIError where failure.code == "version_unavailable" {
                            try await versions.invalidate(api: studio.api, character: character)
                            a.version = nil; b.version = nil; a.gpt = nil; a.sovits = nil; b.gpt = nil; b.sovits = nil
                            error = "这个模型版本已不可用，请重新选择。"
                        } catch { self.error = error.localizedDescription }
                    }
                }.buttonStyle(.borderedProminent).disabled(!ready || manager.busy)
            }
        }
        .padding(24)
        .frame(minWidth: 680, idealWidth: 780, minHeight: 620, idealHeight: 720)
        .onAppear {
            let preferred = studio.preferredEngine
            if let prefill, !appliedPrefill {
                character = prefill.character
                if !editedA {
                    if let comboA = prefill.comboA { a = comboA } else { a.engine = preferred }
                }
                if !editedB {
                    if let comboB = prefill.comboB { b = comboB } else { b.engine = preferred }
                }
            } else {
                if !editedA { a.engine = preferred }
                if !editedB { b.engine = preferred }
                if character.isEmpty, !studio.params.character.isEmpty {
                    character = studio.params.character
                }
            }
        }
        .onChange(of: character) { oldValue, newValue in
            if !oldValue.isEmpty, oldValue != newValue {
                editedA = false
                editedB = false
            }
        }
        .task {
            do {
                try await manager.prepare()
                evalSet = manager.evalSets.first(where: { $0.default })?.name ?? "hard"
            } catch { self.error = error.localizedDescription }
        }
        .task(id: studio.api.baseURL + "|" + character + "|" + studio.models.map(\.slug).joined(separator: ",") + "|" + studio.modelCompletionKey) {
            do {
                guard let selected = preferredBlindCharacter(character, available: studio.models.map(\.slug)) else { versions.reset(); return }
                if character != selected { character = selected; return }
                let targetCharacter = character
                let changed = versions.character != targetCharacter
                try await versions.load(api: studio.api, character: targetCharacter)
                guard !Task.isCancelled else { return }
                Self.applyLoadedVersions(
                    targetCharacter: targetCharacter,
                    currentCharacter: character,
                    versions: versions.rows,
                    selectedVersion: versions.selected,
                    changedCharacter: changed,
                    prefill: prefill,
                    appliedPrefill: &appliedPrefill,
                    editedA: editedA,
                    editedB: editedB,
                    a: &a,
                    b: &b
                )
            } catch is CancellationError { }
            catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
    }

    private var items: Int { (manager.evalSets.first { $0.name == evalSet }?.sentences.count ?? 0) * repeats }
    private var ready: Bool { !character.isEmpty && (a.version != nil || (a.gpt != nil && a.sovits != nil)) && (b.version != nil || (b.gpt != nil && b.sovits != nil)) && items > 0 }

    nonisolated static func blindPlanInput(engines: [String], items: Int) -> PlanInput {
        PlanInput(kind: "gen", engines: engines, items: items, cloud_capable: false)
    }

    nonisolated static func applyLoadedVersions(
        targetCharacter: String,
        currentCharacter: String,
        versions: [ModelVersion],
        selectedVersion: ModelVersion?,
        changedCharacter: Bool,
        prefill: BlindPrefill?,
        appliedPrefill: inout Bool,
        editedA: Bool,
        editedB: Bool,
        a: inout BlindSide,
        b: inout BlindSide
    ) {
        guard currentCharacter == targetCharacter else { return }
        func applyVersion(_ id: String?, to side: inout BlindSide) {
            side.version = id
            guard let version = versions.first(where: { $0.id == id }) else { return }
            side.engine = version.engine
            side.gpt = version.gpt
            side.sovits = version.sovits
        }
        if let prefill, !appliedPrefill, prefill.character == targetCharacter {
            appliedPrefill = true
            if !editedA {
                if let versionA = prefill.versionA, versions.contains(where: { $0.id == versionA }) {
                    applyVersion(versionA, to: &a)
                } else if let comboA = prefill.comboA {
                    a = comboA
                }
            }
            if !editedB {
                if let versionB = prefill.versionB, versions.contains(where: { $0.id == versionB }) {
                    applyVersion(versionB, to: &b)
                } else if let comboB = prefill.comboB {
                    b = comboB
                }
            }
        } else if changedCharacter, let first = selectedVersion {
            if !editedA {
                applyVersion(first.id, to: &a)
            }
            if !editedB {
                applyVersion((versions.first { $0.id != first.id } ?? first).id, to: &b)
            }
        }
    }

    private func apply(_ id: String?, to side: Binding<BlindSide>) {
        side.wrappedValue.version = id
        guard let version = versions.rows.first(where: { $0.id == id }) else { return }
        side.wrappedValue.engine = version.engine
        side.wrappedValue.gpt = version.gpt
        side.wrappedValue.sovits = version.sovits
    }

    private func sideFields(_ side: Binding<BlindSide>, edited: Binding<Bool>) -> some View {
        Group {
            ModelVersionPicker(
                rows: versions.rows,
                selection: Binding(
                    get: { side.wrappedValue.version },
                    set: { edited.wrappedValue = true; apply($0, to: side) }
                ),
                loading: versions.loading
            )
            DisclosureGroup("更多设置") {
                let installed = studio.installedComputeEngines
                Picker("引擎", selection: Binding(
                    get: { side.wrappedValue.engine },
                    set: {
                        edited.wrappedValue = true
                        side.wrappedValue.version = nil
                        side.wrappedValue.engine = $0
                        side.wrappedValue.gpt = nil
                        side.wrappedValue.sovits = nil
                    }
                )) {
                    ForEach(studio.sortedAllEngines) { EngineChoice(engine: $0, installed: installed.isEmpty || installed.contains($0.id)) }
                }
                Picker("GPT", selection: Binding(
                    get: { side.wrappedValue.gpt },
                    set: { edited.wrappedValue = true; side.wrappedValue.version = nil; side.wrappedValue.gpt = $0 }
                )) {
                    Text("选择 GPT").tag(String?.none)
                    ForEach(options(side.wrappedValue.engine, type: "gpt")) { Text($0.modelLabel).tag(Optional($0.name)) }
                }
                Picker("SoVITS", selection: Binding(
                    get: { side.wrappedValue.sovits },
                    set: { edited.wrappedValue = true; side.wrappedValue.version = nil; side.wrappedValue.sovits = $0 }
                )) {
                    Text("选择 SoVITS").tag(String?.none)
                    ForEach(options(side.wrappedValue.engine, type: "sovits")) { Text($0.modelLabel).tag(Optional($0.name)) }
                }
            }
        }
    }

    private func options(_ engine: String, type: String) -> [WeightEntry] {
        manager.weights.filter { $0.engine == engine && $0.type == type && ($0.character == nil || $0.character == character) }
    }
}

func preferredBlindCharacter(_ current: String, available: [String]) -> String? {
    available.contains(current) ? current : available.first
}
