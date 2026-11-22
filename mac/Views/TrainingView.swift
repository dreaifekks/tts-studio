import SwiftUI

struct TrainingView: View {
    @Environment(StudioStore.self) private var store
    @AppStorage("trainingSort") private var sort = "created"
    @AppStorage("trainingOrder") private var order = "desc"
    @State private var request = TrainRequest(slug: "", dataset_id: "")
    @State private var targetMode = "existing"
    @State private var existingSlug = ""
    @State private var newSlug = ""
    @State private var preview: DatasetPreview?
    @State private var mode = "train"
    @State private var cancellationTarget: Job?
    @State private var previewLoading = false
    @State private var previewError: String?
    @State private var initializedDefaults = false
    @State private var userEditedForm = false

    private var trainableEngines: [EngineInfo] { store.sortedTrainableEngines }
    private var selectedEngine: EngineInfo? { trainableEngines.first { $0.id == request.engine } }
    private var computeTrainableKey: String {
        store.compute.devices.map { "\($0.id):\($0.type):\($0.engines.joined(separator: ","))" }.joined(separator: "|")
            + "|" + store.compute.engines.joined(separator: ",")
    }
    private var loraRanks: [Int] {
        let ranks = selectedEngine?.train_defaults?.lora_ranks ?? []
        return ranks.isEmpty ? [16, 32, 64, 128] : ranks
    }
    private var previewKey: String { "\(request.dataset_id)|\(request.min_sec)|\(request.max_sec)|\(request.exclude_interjections)" }
    private var trainJobs: [Job] { store.trainingJobs }
    private var blockReason: String? {
        trainingBlockReason(
            request: request,
            submitting: store.trainingPlanner.submitting,
            previewLoading: previewLoading,
            preview: preview,
            engines: store.engines,
            targetMode: targetMode,
            existingSlugs: Set(store.models.map(\.slug))
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            PageHeading(title: "训练", subtitle: "先看数据和过滤结果，再决定训练。预览不会占用 GPU。")
            HSplitView {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        Picker("任务", selection: $mode) {
                            Text("训练").tag("train")
                            Text("扫参").tag("sweep")
                        }.pickerStyle(.segmented)

                        if mode == "sweep" {
                            SweepFormView()
                        } else {
                            datasetSection
                            configSection
                        }
                    }
                    .padding(.trailing, 12)
                }
                .frame(minWidth: 320, idealWidth: 380, maxWidth: 480)

                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 8) {
                                Text("训练任务").font(.title2.bold()).fixedSize()
                                Spacer(minLength: 4)
                                trainingListControls
                            }
                            VStack(alignment: .leading, spacing: 8) {
                                Text("训练任务").font(.title2.bold())
                                HStack(spacing: 8) {
                                    trainingListControls
                                    Spacer(minLength: 0)
                                }
                            }
                        }
                        ForEach(trainJobs) { job in
                            VStack(alignment: .leading, spacing: 8) {
                                ViewThatFits(in: .horizontal) {
                                    HStack(spacing: 8) {
                                        Text(job.target).font(.headline).lineLimit(1)
                                        Text(job.statusLabel).foregroundStyle(.secondary).fixedSize()
                                        Spacer(minLength: 4)
                                        trainingJobActions(job)
                                    }
                                    VStack(alignment: .leading, spacing: 6) {
                                        HStack(spacing: 8) {
                                            Text(job.target).font(.headline).lineLimit(1)
                                            Text(job.statusLabel).foregroundStyle(.secondary).fixedSize()
                                            Spacer(minLength: 0)
                                        }
                                        HStack(spacing: 8) {
                                            trainingJobActions(job)
                                            Spacer(minLength: 0)
                                        }
                                    }
                                }
                                if store.isDeletedTrainingCharacter(job) {
                                    Text("角色已删除").font(.caption).foregroundStyle(.secondary)
                                }
                                JobProgressView(job: job)
                                if let error = job.error { Text(error).foregroundStyle(.red).font(.caption) }
                                DisclosureGroup("日志尾部") {
                                    Text(job.log_tail.joined(separator: "\n"))
                                        .font(.system(.caption, design: .monospaced))
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                            .padding(14)
                            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
                        }
                        if let evaluation = store.evaluation { EvaluationView(evaluation: evaluation) }
                    }
                    .padding(.leading, 12)
                }
                .frame(minWidth: 380)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .task {
            store.perform {
                try await store.loadDatasets()
                if request.dataset_id.isEmpty { request.dataset_id = store.datasets.first?.id ?? "" }
            }
            syncInitialSelections()
        }
        .task(id: sort + "|" + order) {
            do { try await store.loadTrainingJobs() }
            catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .onChange(of: store.models.map(\.slug)) { syncInitialSelections() }
        .onChange(of: trainableEngines.map(\.id)) { syncPreferredEngineIfNeeded() }
        .onChange(of: store.compute.commonModels) { syncPreferredEngineIfNeeded() }
        .onChange(of: computeTrainableKey) { syncPreferredEngineIfNeeded() }
        .onChange(of: targetMode) {
            if targetMode == "existing" {
                if existingSlug.isEmpty { existingSlug = store.models.first(where: { $0.slug == store.params.character })?.slug ?? store.models.first?.slug ?? "" }
                request.slug = existingSlug
                matchDatasetForCharacter(existingSlug)
            } else {
                request.slug = newSlug.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        .onChange(of: existingSlug) {
            if targetMode == "existing" {
                request.slug = existingSlug
                matchDatasetForCharacter(existingSlug)
            }
        }
        .onChange(of: newSlug) {
            if targetMode == "new" {
                request.slug = newSlug.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
        .task(id: previewKey) {
            guard !request.dataset_id.isEmpty else { preview = nil; return }
            guard request.min_sec <= request.max_sec else { preview = nil; previewError = "最短时长不能大于最长时长"; return }
            previewLoading = true
            previewError = nil
            defer { previewLoading = false }
            do {
                try await Task.sleep(for: .milliseconds(350))
                let snapshot = request
                let result: DatasetPreview = try await store.api.post(
                    "/datasets/\(snapshot.dataset_id)/preview",
                    query: [
                        "min_sec": String(snapshot.min_sec),
                        "max_sec": String(snapshot.max_sec),
                        "exclude_interjections": String(snapshot.exclude_interjections)
                    ]
                )
                try Task.checkCancellation()
                preview = result
            } catch is CancellationError { }
            catch { preview = nil; previewError = error.localizedDescription }
        }
        .confirmationDialog(
            "取消训练任务？",
            isPresented: Binding(get: { cancellationTarget != nil }, set: { if !$0 { cancellationTarget = nil } }),
            presenting: cancellationTarget
        ) { job in
            Button("取消训练", role: .destructive) { cancellationTarget = nil; store.perform { try await store.cancel(job) } }
            Button("继续训练", role: .cancel) { cancellationTarget = nil }
        } message: { job in
            Text("将停止 \(job.target)（\(job.id)），未保存的训练进度会丢失。")
        }
    }

    private var datasetSection: some View {
        GroupBox("数据集与过滤") {
            VStack(alignment: .leading, spacing: 10) {
                Picker("数据集", selection: $request.dataset_id) {
                    Text("选择数据集").tag("")
                    ForEach(store.datasets) { Text("\($0.name) · \($0.lines) 句").tag($0.id) }
                }
                HStack(spacing: 6) {
                    Text("时长").fixedSize()
                    TextField("最短秒", value: $request.min_sec, format: .number).frame(minWidth: 48)
                    Text("–").fixedSize()
                    TextField("最长秒", value: $request.max_sec, format: .number).frame(minWidth: 48)
                    Text("秒").fixedSize()
                }.textFieldStyle(.roundedBorder)
                Toggle("排除纯语气词", isOn: $request.exclude_interjections)
                if previewLoading {
                    ProgressView("过滤预览…").controlSize(.small)
                } else if let preview {
                    HStack {
                        Metric(title: "保留句数", value: "\(preview.lines_kept) / \(preview.lines_total)")
                        Metric(title: "保留分钟", value: number(preview.minutes_kept, digits: 1))
                    }
                    Text("过短 \(preview.excluded_short) · 过长 \(preview.excluded_long) · 语气词 \(preview.excluded_interjection)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if !preview.interjection_examples.isEmpty {
                        Text("语气词示例：" + preview.interjection_examples.prefix(12).joined(separator: "、"))
                            .font(.caption)
                            .lineLimit(3)
                    }
                }
                if let previewError { Text(previewError).font(.caption).foregroundStyle(.red) }
            }
            .padding(8)
        }
    }

    private var configSection: some View {
        GroupBox("训练配置") {
            VStack(alignment: .leading, spacing: 12) {
                Picker("目标角色", selection: $targetMode) {
                    Text("已有角色").tag("existing")
                    Text("新建角色").tag("new")
                }
                .pickerStyle(.segmented)

                if targetMode == "existing" {
                    if store.models.isEmpty {
                        Text("资料库暂无已有角色，可切换“新建角色”。").font(.caption).foregroundStyle(.secondary)
                    } else {
                        Picker("角色", selection: $existingSlug) {
                            Text("选择已有角色").tag("")
                            ForEach(store.models, id: \.slug) { Text("\(characterName($0.slug))（\($0.slug)）").tag($0.slug) }
                        }
                    }
                } else {
                    TextField("新角色 slug（小写英文、数字、连字符）", text: $newSlug)
                        .textFieldStyle(.roundedBorder)
                }

                let installed = store.trainableComputeEngines
                let enginePicker = Picker("引擎", selection: Binding(
                    get: { request.engine },
                    set: { newEngineID in
                        userEditedForm = true
                        if let info = trainableEngines.first(where: { $0.id == newEngineID }) {
                            applyTrainDefaults(for: info, to: &request)
                        } else {
                            request.engine = newEngineID
                        }
                    }
                )) {
                    ForEach(trainableEngines) {
                        EngineChoice(engine: $0, installed: installed.isEmpty || installed.contains($0.id))
                    }
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        enginePicker
                        if let engine = selectedEngine {
                            Button("恢复此引擎默认") {
                                userEditedForm = true
                                applyTrainDefaults(for: engine, to: &request)
                            }
                            .controlSize(.small)
                            .fixedSize()
                        }
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        enginePicker
                        if let engine = selectedEngine {
                            Button("恢复此引擎默认") {
                                userEditedForm = true
                                applyTrainDefaults(for: engine, to: &request)
                            }
                            .controlSize(.small)
                            .fixedSize()
                        }
                    }
                }

                Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                    GridRow { Text(""); Text("轮数"); Text("Batch"); Text("保存间隔") }
                    GridRow {
                        Text("SoVITS")
                        integerField(editedBinding(\.sovits_epochs))
                        integerField(editedBinding(\.sovits_batch))
                        integerField(editedBinding(\.sovits_save_every))
                    }
                    GridRow {
                        Text("GPT")
                        integerField(editedBinding(\.gpt_epochs))
                        integerField(editedBinding(\.gpt_batch))
                        integerField(editedBinding(\.gpt_save_every))
                    }
                }

                if selectedEngine?.isLora == true {
                    Picker("LoRA Rank", selection: Binding(
                        get: { request.lora_rank ?? selectedEngine?.train_defaults?.lora_rank ?? 32 },
                        set: { userEditedForm = true; request.lora_rank = $0 }
                    )) {
                        ForEach(loraRanks, id: \.self) { rank in
                            Text("Rank \(rank)").tag(rank)
                        }
                    }
                }

                TextField("最多句数（留空使用全部）", value: Binding(
                    get: { request.max_lines },
                    set: { userEditedForm = true; request.max_lines = $0 }
                ), format: .number)
                    .textFieldStyle(.roundedBorder)

                Text("切换引擎时已自动填入推荐轮数、Batch 与保存间隔；轮数须不小于保存间隔。")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                ComputePicker(planner: store.trainingPlanner, input: trainingPlan, action: "训练", taskTitle: request.slug)

                Button("提交训练", systemImage: "cpu") {
                    let snapshot = request
                    store.perform { try await store.submitTraining(snapshot, lines: trainingPlan.lines) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(blockReason != nil)

                if let reason = blockReason {
                    Label(reason, systemImage: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            .padding(8)
        }
    }

    private func syncInitialSelections() {
        if store.models.isEmpty {
            targetMode = "new"
        } else if targetMode == "existing" && existingSlug.isEmpty {
            existingSlug = store.models.first(where: { $0.slug == store.params.character })?.slug ?? store.models.first?.slug ?? ""
            request.slug = existingSlug
            matchDatasetForCharacter(existingSlug)
        }
        syncPreferredEngineIfNeeded()
    }

    private func editedBinding(_ keyPath: WritableKeyPath<TrainRequest, Int>) -> Binding<Int> {
        Binding(
            get: { request[keyPath: keyPath] },
            set: { newValue in
                if request[keyPath: keyPath] != newValue {
                    userEditedForm = true
                    request[keyPath: keyPath] = newValue
                }
            }
        )
    }

    private func syncPreferredEngineIfNeeded() {
        Self.syncPreferredTrainingEngine(
            request: &request,
            trainableEngines: trainableEngines,
            preferredEngine: store.preferredTrainableEngine(from: trainableEngines),
            fallbackEngineID: store.preferredEngine,
            initializedDefaults: &initializedDefaults,
            userEditedForm: userEditedForm
        )
    }

    nonisolated static func syncPreferredTrainingEngine(
        request: inout TrainRequest,
        trainableEngines: [EngineInfo],
        preferredEngine: EngineInfo?,
        fallbackEngineID: String,
        initializedDefaults: inout Bool,
        userEditedForm: Bool
    ) {
        if !userEditedForm {
            if let preferred = preferredEngine {
                if !initializedDefaults || request.engine != preferred.id {
                    applyTrainDefaults(for: preferred, to: &request)
                    initializedDefaults = true
                }
            } else if !initializedDefaults {
                request.engine = fallbackEngineID
            }
        } else if !trainableEngines.isEmpty,
                  !trainableEngines.contains(where: { $0.id == request.engine }),
                  let fallback = preferredEngine ?? trainableEngines.first {
            applyTrainDefaults(for: fallback, to: &request)
            initializedDefaults = true
        }
    }

    @ViewBuilder
    private var trainingListControls: some View {
        SortControls(options: ListSorting.training, field: $sort, order: $order)
        Button("刷新") { store.perform { try await store.loadTrainingJobs() } }.fixedSize()
    }

    @ViewBuilder
    private func trainingJobActions(_ job: Job) -> some View {
        if job.active {
            Button("取消任务…", role: .destructive) { cancellationTarget = job }.fixedSize()
        }
        if job.kind == "train" && job.status == "succeeded" && store.trainingCatalogLoaded && !store.isDeletedTrainingCharacter(job) {
            Button("查看评测") { store.perform { try await store.loadEvaluation(job) } }.fixedSize()
        }
    }

    private func matchDatasetForCharacter(_ slug: String) {
        if let datasetID = store.models.first(where: { $0.slug == slug })?.dataset_id, !datasetID.isEmpty {
            request.dataset_id = datasetID
        }
    }

    private var trainingPlan: PlanInput {
        PlanInput(
            kind: "train",
            engine: request.engine,
            lines: min(preview?.lines_kept ?? 0, request.max_lines ?? Int.max),
            gpt_epochs: request.gpt_epochs,
            sovits_epochs: request.sovits_epochs,
            gpt_save_every: request.gpt_save_every,
            sovits_save_every: request.sovits_save_every
        )
    }

    private func integerField(_ value: Binding<Int>) -> some View {
        TextField("", value: value, format: .number)
            .textFieldStyle(.roundedBorder)
            .frame(minWidth: 45)
    }
}

struct EvaluationView: View {
    @Environment(StudioStore.self) private var store
    let evaluation: TrainEval
    @State private var versions = ModelVersionStore()
    @State private var weights: [WeightEntry] = []
    @State private var settingDefaultID: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    Text("模型评测 · \(characterName(evaluation.character)) · \(displayEvalSet(evaluation.eval_set))")
                        .font(.title3.bold())
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if evaluation.combos.count >= 2 {
                        Button("前两名盲听", systemImage: "headphones") {
                            startTopTwoBlind()
                        }
                        .controlSize(.small)
                        .fixedSize()
                    }
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("模型评测 · \(characterName(evaluation.character)) · \(displayEvalSet(evaluation.eval_set))")
                        .font(.title3.bold())
                    if evaluation.combos.count >= 2 {
                        Button("前两名盲听", systemImage: "headphones") {
                            startTopTwoBlind()
                        }
                        .controlSize(.small)
                        .fixedSize()
                    }
                }
            }
            Table(evaluation.combos) {
                TableColumn("模型版本") {
                    Text(modelVersionLabel(gpt: $0.gpt, sovits: $0.sovits, versions: versions.rows, weights: weights))
                }
                TableColumn("评分") {
                    Text(displayScore($0.score_median)).help(scoreHelp($0.score_median))
                }.width(55)
                TableColumn("读音准确") { Text(displayAccuracy($0.cer_median)) }.width(82)
                TableColumn("音色相似") { Text(displaySimilarity($0.spk_sim_median)) }.width(82)
                TableColumn("操作") { combo in
                    HStack(spacing: 6) {
                        Button(combo.id == evaluation.best.id ? "★ 试用" : "试用") {
                            store.perform { try await store.tryCombination(combo, evaluation: evaluation) }
                        }
                        Button(settingDefaultID == combo.id ? "设置中…" : "设为默认") {
                            settingDefaultID = combo.id
                            store.perform {
                                defer { settingDefaultID = nil }
                                try await store.setDefaultVersion(
                                    character: evaluation.character,
                                    engine: evaluation.engine,
                                    gpt: combo.gpt,
                                    sovits: combo.sovits
                                )
                            }
                        }
                        .disabled(settingDefaultID != nil || store.settingDefaultVersion)
                    }
                    .controlSize(.small)
                }.width(145)
            }
            .frame(height: CGFloat(min(8, max(1, evaluation.combos.count)) * 34 + 40))
        }
        .task(id: store.api.baseURL + "|" + evaluation.job_id + "|" + evaluation.character + "|" + evaluation.engine + "|" + evaluation.eval_set) {
            versions.reset()
            weights = []
            do {
                try await versions.load(api: store.api, character: evaluation.character)
                let values = try await store.visibleWeights(character: evaluation.character, engine: evaluation.engine, evalSet: evaluation.eval_set)
                try Task.checkCancellation()
                weights = values
            } catch is CancellationError { }
            catch { NSLog("评测结果标签读取失败，保留训练轮数：%@", error.localizedDescription) }
        }
    }

    private func startTopTwoBlind() {
        guard evaluation.combos.count >= 2 else { return }
        let first = evaluation.combos[0]
        let second = evaluation.combos[1]
        let versionA = versions.rows.first(where: { $0.engine == evaluation.engine && $0.gpt == first.gpt && $0.sovits == first.sovits })?.id
        let versionB = versions.rows.first(where: { $0.engine == evaluation.engine && $0.gpt == second.gpt && $0.sovits == second.sovits })?.id
        let sideA = BlindSide(engine: evaluation.engine, gpt: first.gpt, sovits: first.sovits, version: versionA)
        let sideB = BlindSide(engine: evaluation.engine, gpt: second.gpt, sovits: second.sovits, version: versionB)
        store.startBlindPK(character: evaluation.character, versionA: versionA, versionB: versionB, comboA: sideA, comboB: sideB)
    }
}
