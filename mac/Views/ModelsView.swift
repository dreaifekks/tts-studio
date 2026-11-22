import SwiftUI
import Charts

struct ModelsView: View {
    @Environment(StudioStore.self) private var studio
    var body: some View {
        CharacterVersionsView(character: studio.params.character, manager: studio.weightManager)
            .padding(.horizontal, 24)
            .padding(.vertical, 12)
    }
}

struct CharacterVersionsView: View {
    @Environment(StudioStore.self) private var studio
    let character: String
    @Bindable var manager: WeightStore

    @State private var selectedForPK: Set<String> = []
    @State private var showingLowLevel = false
    @State private var sortOrder = [KeyPathComparator(\WeightEntry.created_at, order: .reverse)]
    @State private var displayName = ""
    @State private var note = ""
    @State private var deleting: WeightEntry?

    private var recommendedVersion: ModelVersion? {
        manager.versions.rows.first(where: \.recommended) ?? manager.versions.rows.first
    }

    private var orderedPKSelection: [ModelVersion] {
        manager.versions.rows.filter { selectedForPK.contains($0.id) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                defaultVoiceCard

                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 10) {
                        evalSetPicker
                        refreshVersionsButton
                        Spacer(minLength: 4)
                        pkToolbarActions
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(spacing: 10) {
                            evalSetPicker
                            refreshVersionsButton
                            Spacer(minLength: 0)
                        }
                        HStack(spacing: 8) {
                            pkToolbarActions
                            Spacer(minLength: 0)
                        }
                    }
                }

                if let notice = manager.notice {
                    Text(notice).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                }

                if let id = manager.evaluationJobID, let job = studio.jobs.first(where: { $0.id == id }) {
                    HStack {
                        JobProgressView(job: job)
                        if manager.evaluationBusy {
                            Button("取消评测") { manager.stopEvaluation(studio: studio) }
                                .controlSize(.small)
                        }
                    }
                    .padding(10)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                }

                ComputePicker(
                    planner: manager.planner,
                    input: PlanInput(kind: "eval", engine: manager.pairEngine, items: manager.evaluationItems),
                    action: "评测",
                    ready: manager.versions.selected != nil || manager.manualPair
                )
                .task(id: manager.evaluationSet) {
                    do {
                        let sets: EvalSetList = try await studio.api.get("/eval-sets")
                        if let set = sets.sets.first(where: { $0.name == manager.evaluationSet }) {
                            manager.evaluationItems = set.sentences.count * set.candidates
                        }
                    } catch { studio.errorMessage = error.localizedDescription }
                }

                pairedVersionsSection

                Divider()

                DisclosureGroup("底层权重文件与手动混搭", isExpanded: $showingLowLevel) {
                    lowLevelWeightsSection
                        .padding(.top, 10)
                }
            }
            .padding(.bottom, 16)
        }
        .onAppear {
            syncCharacter()
            synchronizeSortIndicator()
        }
        .onChange(of: character) {
            selectedForPK.removeAll()
            syncCharacter()
        }
        .onChange(of: manager.evalSet) {
            manager.evaluationSet = manager.evalSet
        }
        .onChange(of: sortStateKey) { synchronizeSortIndicator() }
        .task(id: manager.queryKey) { await loadWeightsTask() }
        .task(id: manager.pairKey) { await loadPairTask() }
        .onChange(of: manager.selectedID) { handleSelectedWeightChange() }
        .onChange(of: sortOrder) { handleSortOrderChange() }
        .confirmationDialog("删除这个模型？默认声音会使用可用的其他模型。", isPresented: deleteDialogPresented) {
            if let entry = deleting {
                Button("删除 \(entry.label)", role: .destructive) {
                    studio.perform { try await manager.delete(entry, studio: studio); deleting = nil }
                }
            }
        }
    }

    private var sortStateKey: String { "\(manager.sort)|\(manager.order)" }

    private var deleteDialogPresented: Binding<Bool> {
        Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })
    }

    private func loadWeightsTask() async {
        do { try await manager.load() }
        catch is CancellationError { }
        catch { studio.errorMessage = error.localizedDescription }
    }

    private func loadPairTask() async {
        do { try await manager.loadPair(studio: studio) }
        catch is CancellationError { }
        catch { studio.errorMessage = error.localizedDescription }
    }

    private func handleSelectedWeightChange() {
        guard let entry = manager.selected else { return }
        displayName = entry.display_name ?? ""
        note = entry.note
        if !entry.hidden {
            manager.useManualPair()
            if let entryChar = entry.character { manager.pairCharacter = entryChar }
            manager.pairEngine = entry.engine
            if entry.type == "gpt" { manager.gptID = entry.id } else { manager.sovitsID = entry.id }
        }
    }

    private func handleSortOrderChange() {
        guard let comparator = sortOrder.first else { return }
        let keys: [(AnyKeyPath, String)] = [
            (\WeightEntry.modelLabel, "name"),
            (\WeightEntry.characterLabel, "character"),
            (\WeightEntry.engine, "engine"),
            (\WeightEntry.typeLabel, "type"),
            (\WeightEntry.epochValue, "epoch"),
            (\WeightEntry.source, "source"),
            (\WeightEntry.scoreValue, "score"),
            (\WeightEntry.created_at, "created_at"),
            (\WeightEntry.size_bytes, "size")
        ]
        if let field = keys.first(where: { $0.0 == comparator.keyPath })?.1 {
            manager.sort = field
            manager.order = comparator.order == .forward ? "asc" : "desc"
        }
    }

    private var evalSetPicker: some View {
        Picker("评测集", selection: $manager.evalSet) {
            Text("难句评测").tag("hard")
            Text("快速评测").tag("mini")
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(minWidth: 145, maxWidth: 170)
    }

    private var refreshVersionsButton: some View {
        Button("刷新", systemImage: "arrow.clockwise") {
            studio.perform { try await refresh() }
        }
        .fixedSize()
    }

    @ViewBuilder
    private var pkToolbarActions: some View {
        if selectedForPK.count == 2 {
            let picked = orderedPKSelection
            Button("发起盲听 PK", systemImage: "headphones") {
                guard picked.count == 2 else { return }
                selectedForPK.removeAll()
                studio.startBlindPK(character: character, versionA: picked[0].id, versionB: picked[1].id)
            }
            .buttonStyle(.borderedProminent)
            .fixedSize()
        } else if !selectedForPK.isEmpty {
            Text("已勾选 \(selectedForPK.count) 个版本（选 2 个可发起盲听 PK）")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("清空勾选") { selectedForPK.removeAll() }
                .controlSize(.small)
                .fixedSize()
        } else {
            Text("勾选 2 个版本可发起盲听 PK")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func syncCharacter() {
        guard !character.isEmpty else { return }
        if manager.character != character { manager.character = character }
        if manager.pairCharacter != character { manager.pairCharacter = character }
    }

    private var defaultVoiceCard: some View {
        GroupBox {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 14) {
                    defaultVoiceInfo
                    Spacer(minLength: 8)
                    defaultVoiceAction
                }
                VStack(alignment: .leading, spacing: 10) {
                    defaultVoiceInfo
                    HStack {
                        Spacer(minLength: 0)
                        defaultVoiceAction
                    }
                }
            }
            .padding(6)
        }
    }

    private var defaultVoiceInfo: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Label("当前默认声音", systemImage: "star.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.orange)
                if let rec = recommendedVersion {
                    Text(displayEngine(rec.engine))
                        .font(.caption)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.accentColor.opacity(0.14), in: Capsule())
                }
            }
            if let rec = recommendedVersion {
                Text(rec.label).font(.title3.bold())
                if let score = rec.scoreLabel {
                    Text(score).font(.callout).foregroundStyle(.secondary).help(scoreHelp(rec.score))
                } else {
                    Text("尚未评测").font(.callout).foregroundStyle(.secondary)
                }
            } else if manager.versions.loading {
                ProgressView("正在读取模型版本…").controlSize(.small)
            } else {
                Text("暂无可用模型版本").font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var defaultVoiceAction: some View {
        if let rec = recommendedVersion {
            Button("去合成", systemImage: "waveform.badge.plus") {
                studio.synthesizeWith(character: character, versionID: rec.id)
            }
            .buttonStyle(.borderedProminent)
            .fixedSize()
        }
    }

    private var pairedVersionsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("已配对的模型版本").font(.headline)
                Spacer()
                Text("\(manager.versions.rows.count) 个版本").font(.caption).foregroundStyle(.secondary)
            }

            if manager.versions.loading && manager.versions.rows.isEmpty {
                ProgressView("正在读取版本列表…").padding(.vertical, 16)
            } else if manager.versions.rows.isEmpty {
                ContentUnavailableView(
                    "暂无模型版本",
                    systemImage: "square.stack.3d.up",
                    description: Text("训练角色或检查算力设备底模后，版本会显示在这里。")
                )
            } else {
                LazyVStack(spacing: 8) {
                    ForEach(manager.versions.rows) { version in
                        versionRow(version)
                    }
                }
            }
        }
    }

    private func versionRow(_ version: ModelVersion) -> some View {
        let checked = selectedForPK.contains(version.id)
        let matchingCombo = manager.comparisons.first { $0.gpt == version.gpt && $0.sovits == version.sovits }

        let info = VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(version.label).font(.headline).lineLimit(1)
                Text(displayEngine(version.engine))
                    .font(.caption)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background(.quaternary, in: Capsule())
                    .fixedSize()
                if version.recommended {
                    Label("默认 / 推荐", systemImage: "checkmark.seal.fill")
                        .font(.caption)
                        .foregroundStyle(.tint)
                        .fixedSize()
                }
            }
            HStack(spacing: 10) {
                if let combo = matchingCombo {
                    Text("\(displayEvalSet(combo.eval_set)) 评分 \(displayScore(combo.score_median))")
                        .help(scoreHelp(combo.score_median))
                    Text("读音 \(displayAccuracy(combo.cer_median))")
                    Text("音色 \(displaySimilarity(combo.spk_sim_median))")
                } else if let score = version.scoreLabel {
                    Text(score).help(scoreHelp(version.score))
                } else {
                    Text("尚未评测")
                }
                Text("· \(displayDateTime(version.created))")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .monospacedDigit()
            .lineLimit(2)
        }

        let actions = HStack(spacing: 6) {
            Button("设为默认") {
                studio.perform {
                    try await studio.setDefaultVersion(character: character, engine: version.engine, gpt: version.gpt, sovits: version.sovits)
                    manager.notice = "已将「\(version.label)」设为默认声音"
                }
            }
            .disabled(manager.busy || manager.evaluationBusy || studio.settingDefaultVersion)

            Button("评测") {
                manager.evaluationSet = manager.evalSet
                manager.evaluationTask = studio.perform {
                    try await manager.evaluateVersion(version, studio: studio)
                }
            }
            .disabled(manager.busy || manager.evaluationBusy)

            Button("去合成") {
                studio.synthesizeWith(character: character, versionID: version.id)
            }
            .buttonStyle(.borderedProminent)
        }
        .controlSize(.small)
        .fixedSize()

        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                pkCheckbox(for: version, checked: checked)
                info
                Spacer(minLength: 8)
                actions
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 10) {
                    pkCheckbox(for: version, checked: checked)
                    info
                    Spacer(minLength: 0)
                }
                HStack {
                    Spacer(minLength: 0)
                    actions
                }
            }
        }
        .padding(12)
        .background(
            checked ? Color.accentColor.opacity(0.1) : Color(nsColor: .controlBackgroundColor),
            in: RoundedRectangle(cornerRadius: 10)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(checked ? Color.accentColor.opacity(0.5) : Color.secondary.opacity(0.15))
        )
    }

    private func pkCheckbox(for version: ModelVersion, checked: Bool) -> some View {
        Toggle(
            "选择 \(version.label) 参与盲听 PK",
            isOn: Binding(
                get: { checked },
                set: { enabled in
                    if enabled { selectedForPK.insert(version.id) }
                    else { selectedForPK.remove(version.id) }
                }
            )
        )
        .labelsHidden()
        .toggleStyle(.checkbox)
    }

    // MARK: - Low-level weights & manual pairing

    private var lowLevelWeightsSection: some View {
        let installed = studio.installedComputeEngines
        return VStack(alignment: .leading, spacing: 12) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 155), alignment: .leading)], alignment: .leading, spacing: 8) {
                Picker("角色", selection: $manager.character) {
                    Text("全部（含底模）").tag("")
                    ForEach(studio.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
                }
                Picker("引擎", selection: $manager.engine) {
                    Text("全部").tag("")
                    ForEach(studio.sortedAllEngines) { EngineChoice(engine: $0, installed: installed.isEmpty || installed.contains($0.id)) }
                }
                Picker("来源", selection: $manager.source) {
                    Text("全部").tag("")
                    Text("随附模型").tag("july")
                    Text("自己训练").tag("user")
                    Text("底模").tag("pretrained")
                }
                Picker("类型", selection: $manager.type) {
                    Text("全部").tag("")
                    Text("GPT").tag("gpt")
                    Text("SoVITS").tag("sovits")
                }
                Toggle("显示已隐藏", isOn: $manager.showHidden)
            }
            HStack {
                SortControls(options: ListSorting.weights, field: $manager.sort, order: $manager.order)
                Text("\(manager.total) 个权重文件").font(.caption).foregroundStyle(.secondary)
                Spacer()
            }

            weightTable

            if manager.rows.isEmpty {
                Text("没有匹配权重，切换角色为“全部（含底模）”可查看底模。").font(.caption).foregroundStyle(.secondary)
            }

            if let entry = manager.selected {
                let fields = VStack(alignment: .leading, spacing: 6) {
                    DisclosureGroup("权重详情") {
                        Text("文件：\(entry.name)").textSelection(.enabled)
                        if let epoch = entry.epoch { Text("训练轮数：\(epoch)") }
                    }.font(.caption).foregroundStyle(.secondary)
                    TextField("显示名", text: $displayName).textFieldStyle(.roundedBorder)
                    TextField("备注", text: $note).textFieldStyle(.roundedBorder)
                }
                let entryActions = HStack(spacing: 8) {
                    Button("保存名称 / 备注") {
                        studio.perform { try await manager.patch(entry, patch: WeightPatch(display_name: displayName, note: note)) }
                    }
                    Button(entry.hidden ? "取消隐藏" : "隐藏") {
                        studio.perform {
                            try await manager.patch(entry, patch: WeightPatch(hidden: !entry.hidden))
                            try await manager.loadPair(studio: studio)
                        }
                    }
                    Button("删除…", role: .destructive) { deleting = entry }
                        .disabled(!entry.deletable || entry.source != "user")
                }.fixedSize()

                WrappingHStack(horizontalSpacing: 10, verticalSpacing: 8, alignLastTrailingOnSingleLine: true) {
                    fields.frame(minWidth: 240, maxWidth: 420, alignment: .leading)
                    entryActions
                }
                .disabled(manager.busy || manager.evaluationBusy)
            }

            Divider()

            GroupBox("手动混搭 GPT / SoVITS") {
                VStack(alignment: .leading, spacing: 10) {
                    let pairEnginePicker = Picker("引擎", selection: Binding(
                        get: { manager.pairEngine },
                        set: { manager.useManualPair(); manager.pairEngine = $0 }
                    )) {
                        ForEach(studio.sortedAllEngines) { EngineChoice(engine: $0, installed: installed.isEmpty || installed.contains($0.id)) }
                    }
                    let pairButtons = HStack(spacing: 8) {
                        Button("设为默认组合") {
                            studio.perform { try await manager.setDefaultPair(studio: studio) }
                        }
                        .disabled(!manager.pairReady || manager.busy || manager.evaluationBusy || studio.settingDefaultVersion)
                        Button("评测此混搭") {
                            manager.evaluationSet = manager.evalSet
                            manager.evaluationTask = studio.perform { try await manager.evaluate(studio: studio) }
                        }
                        .disabled(!manager.pairReady || manager.busy || manager.evaluationBusy)
                    }.fixedSize()

                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) {
                            pairEnginePicker
                            Spacer(minLength: 8)
                            pairButtons
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            pairEnginePicker
                            pairButtons
                        }
                    }
                    let gptPicker = Picker("GPT", selection: Binding(
                        get: { manager.gptID },
                        set: { manager.useManualPair(); manager.gptID = $0 }
                    )) {
                        Text("请选择 GPT").tag("")
                        if let version = manager.versions.selected, manager.pairGPT == nil {
                            Text("\(version.label) · GPT").tag(manager.gptID)
                        }
                        ForEach(manager.pairRows.filter { $0.type == "gpt" }) { Text($0.modelLabel).tag($0.id) }
                    }
                    let sovitsPicker = Picker("SoVITS", selection: Binding(
                        get: { manager.sovitsID },
                        set: { manager.useManualPair(); manager.sovitsID = $0 }
                    )) {
                        Text("请选择 SoVITS").tag("")
                        if let version = manager.versions.selected, manager.pairSoVITS == nil {
                            Text("\(version.label) · SoVITS").tag(manager.sovitsID)
                        }
                        ForEach(manager.pairRows.filter { $0.type == "sovits" }) { Text($0.modelLabel).tag($0.id) }
                    }
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 10) {
                            gptPicker
                            sovitsPicker
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            gptPicker
                            sovitsPicker
                        }
                    }
                }
                .padding(6)
                .disabled(manager.busy || manager.evaluationBusy)
            }
        }
    }

    private var weightTable: some View {
        Table(manager.rows, selection: $manager.selectedID, sortOrder: $sortOrder) {
            TableColumnForEach(WeightTextColumn.initial) { column in
                TableColumn(column.title, value: column.path).width(column.width)
            }
            TableColumn("来源", value: \.source) { (entry: WeightEntry) in Text(displaySource(entry.source)) }.width(80)
            TableColumn("评分 · \(displayEvalSet(manager.evalSet))", value: \.scoreValue) { (entry: WeightEntry) in
                Text(displayScore(entry.best_eval?.score_median)).help(scoreHelp(entry.best_eval?.score_median))
            }.width(110)
            TableColumn("读音准确 / 音色相似") { (entry: WeightEntry) in
                Text("\(displayAccuracy(entry.best_eval?.cer_median)) / \(displaySimilarity(entry.best_eval?.spk_sim_median))")
            }.width(140)
            TableColumn("创建时间", value: \.created_at) { entry in Text(displayDateTime(entry.created_at)) }.width(145)
            TableColumn("大小", value: \.size_bytes) { (entry: WeightEntry) in Text(entry.sizeLabel) }.width(65)
            TableColumn("默认") { (entry: WeightEntry) in Text(entry.is_default ? "是" : "—") }.width(40)
            TableColumn("隐藏") { (entry: WeightEntry) in Text(entry.hidden ? "是" : "—") }.width(40)
        }
        .frame(height: CGFloat(min(8, max(2, manager.rows.count)) * 24 + 30))
    }

    private func synchronizeSortIndicator() {
        let direction: SortOrder = manager.order == "asc" ? .forward : .reverse
        let key: KeyPathComparator<WeightEntry>
        switch manager.sort {
        case "name": key = .init(\.label, order: direction)
        case "character": key = .init(\.characterLabel, order: direction)
        case "engine": key = .init(\.engine, order: direction)
        case "type": key = .init(\.typeLabel, order: direction)
        case "epoch": key = .init(\.epochValue, order: direction)
        case "source": key = .init(\.source, order: direction)
        case "score": key = .init(\.scoreValue, order: direction)
        case "size": key = .init(\.size_bytes, order: direction)
        default: key = .init(\.created_at, order: direction)
        }
        sortOrder = [key]
    }

    private func refresh() async throws {
        try await studio.reloadModels(resetSelection: false)
        try await manager.refreshVersions(studio: studio)
        try await manager.load()
    }
}

@MainActor private struct WeightTextColumn: Identifiable {
    let id: String
    let title: String
    let path: KeyPath<WeightEntry, String>
    let width: CGFloat
    static let initial = [
        WeightTextColumn(id: "name", title: "显示名", path: \.modelLabel, width: 240),
        WeightTextColumn(id: "character", title: "角色", path: \.characterLabel, width: 70),
        WeightTextColumn(id: "engine", title: "引擎", path: \.engine, width: 75),
        WeightTextColumn(id: "type", title: "类型", path: \.typeLabel, width: 60)
    ]
}
