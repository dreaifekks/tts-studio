import SwiftUI

struct SweepFormView: View {
    @Environment(StudioStore.self) private var studio
    @State private var request = SweepRequest()
    @State private var userEditedEngines = false

    private var cloudEngines: Set<String> {
        Set(studio.compute.devices.filter { $0.type == "cloud" }.flatMap(\.engines))
    }

    private var sweepableEngines: [EngineInfo] {
        let trainable = studio.sortedTrainableEngines
        if cloudEngines.isEmpty { return trainable }
        return sortedEngineInfos(trainable.filter { cloudEngines.contains($0.id) }, installed: cloudEngines)
    }

    private var validSweepEngineIDs: Set<String> {
        Set(sweepableEngines.map(\.id))
    }

    private var hasKnownEngineCatalog: Bool {
        !studio.engines.isEmpty || !cloudEngines.isEmpty
    }

    private var invalidSelectedEngines: [String] {
        Self.invalidSelectedEngines(
            selected: request.engines,
            validEngineIDs: validSweepEngineIDs,
            hasKnownCatalog: hasKnownEngineCatalog
        )
    }

    private var uninstalledTrainableEngines: [EngineInfo] {
        guard !cloudEngines.isEmpty else { return [] }
        let invalidSet = Set(invalidSelectedEngines)
        return studio.sortedTrainableEngines.filter { !cloudEngines.contains($0.id) && !invalidSet.contains($0.id) }
    }

    nonisolated static func invalidSelectedEngines(selected: [String], validEngineIDs: Set<String>, hasKnownCatalog: Bool) -> [String] {
        guard hasKnownCatalog else { return [] }
        return sortedEngines(selected.filter { !validEngineIDs.contains($0) })
    }

    private var hasLoraEngineSelected: Bool {
        request.engines.contains { engineID in
            studio.engines.first(where: { $0.id == engineID })?.isLora == true
        }
    }

    private var loraRanks: [Int] {
        let ranks = request.engines.compactMap { id in
            studio.engines.first(where: { $0.id == id })?.train_defaults?.lora_ranks
        }.first(where: { !$0.isEmpty }) ?? []
        return ranks.isEmpty ? [16, 32, 64, 128] : ranks
    }

    private var blockReason: String? {
        sweepBlockReason(
            request: request,
            submitting: studio.sweepPlanner.submitting,
            allCompleted: studio.sweepPlanner.allCompleted,
            allowedEngines: hasKnownEngineCatalog ? validSweepEngineIDs : nil
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            GroupBox("角色与模型") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(studio.models.filter { $0.dataset_id != nil }, id: \.slug) { character in
                        Toggle(characterName(character.slug), isOn: membership(character.slug, in: $request.characters))
                    }
                    Divider()
                    ForEach(sweepableEngines) { engine in
                        Toggle(displayEngine(engine.id), isOn: engineMembership(engine.id))
                    }
                    ForEach(invalidSelectedEngines, id: \.self) { engineID in
                        Toggle("\(displayEngine(engineID))（当前算力不支持扫参，请取消勾选）", isOn: engineMembership(engineID))
                            .foregroundStyle(.orange)
                    }
                    ForEach(uninstalledTrainableEngines) { engine in
                        Toggle("\(displayEngine(engine.id))（当前算力未安装）", isOn: .constant(false))
                            .disabled(true)
                            .foregroundStyle(.secondary)
                    }
                }.padding(8)
            }
            GroupBox("训练组合") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(request.trains.indices, id: \.self) { index in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack {
                                Text("组合 \(index + 1)").font(.headline)
                                Spacer()
                                if request.trains.count > 1 { Button("移除") { request.trains.remove(at: index) }.fixedSize() }
                            }
                            Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 6) {
                                GridRow { Text(""); Text("轮数").fixedSize(); Text("保存间隔").fixedSize(); Text("每批").fixedSize() }.font(.caption)
                                GridRow { Text("SoVITS").fixedSize(); numberField($request.trains[index].sovits_epochs); numberField($request.trains[index].sovits_save_every); numberField($request.trains[index].sovits_batch) }
                                GridRow { Text("GPT").fixedSize(); numberField($request.trains[index].gpt_epochs); numberField($request.trains[index].gpt_save_every); numberField($request.trains[index].gpt_batch) }
                            }
                            if hasLoraEngineSelected {
                                Picker("LoRA Rank", selection: $request.trains[index].lora_rank) {
                                    Text("默认（32）").tag(Int?.none)
                                    ForEach(loraRanks, id: \.self) { rank in
                                        Text("Rank \(rank)").tag(Optional(rank))
                                    }
                                }
                            }
                        }
                    }
                    Button("添加组合", systemImage: "plus") {
                        request.trains.append(SweepTrain(sovits_epochs: 16, gpt_epochs: 20, lora_rank: hasLoraEngineSelected ? 32 : nil))
                    }
                    TextField("每组最多句数（留空使用全部）", value: $request.max_lines, format: .number)
                    HStack(spacing: 6) {
                        Text("保留前").fixedSize()
                        numberField($request.keep).frame(maxWidth: 72)
                        Text("个模型组合").fixedSize()
                        Spacer(minLength: 0)
                    }
                    HStack(spacing: 6) {
                        Text("生成样本的组合数").fixedSize()
                        numberField($request.samples).frame(maxWidth: 72)
                        Spacer(minLength: 0)
                    }
                }.textFieldStyle(.roundedBorder).padding(8)
            }
            ComputePicker(planner: studio.sweepPlanner, input: request, action: "扫参", sweep: true)
            if !studio.sweepPlanner.groups.isEmpty {
                Text("共 \(studio.sweepPlanner.groups.count) 组 · \(studio.sweepPlanner.groups.filter { $0.status == "skipped" }.count) 组已完成").font(.caption).foregroundStyle(.secondary)
            }
            Button(studio.sweepPlanner.allCompleted ? "都已完成" : studio.sweepPlanner.submitting ? "正在提交…" : "提交扫参") {
                let snapshot = request
                studio.perform {
                    let job: Job = try await studio.sweepPlanner.submit(api: studio.api, path: "/train/sweep", input: snapshot)
                    studio.recordLocalJob(job.id); studio.updateJob(job); studio.monitor(job)
                    try await studio.loadTrainingJobs()
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(blockReason != nil)

            if let reason = blockReason {
                Label(reason, systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .onAppear { syncPreferredEngineIfNeeded() }
        .onChange(of: sweepableEngines.map(\.id)) { syncPreferredEngineIfNeeded() }
        .onChange(of: cloudEngines.sorted()) { syncPreferredEngineIfNeeded() }
        .onChange(of: studio.compute.commonModels) { syncPreferredEngineIfNeeded() }
    }

    private func syncPreferredEngineIfNeeded() {
        guard !userEditedEngines else { return }
        let ids = sweepableEngines.map(\.id)
        guard !ids.isEmpty else { return }
        let preferred = ComputeStore.resolvePreferredEngine(commonModels: studio.compute.commonModels, availableEngines: ids)
        if ids.contains(preferred) {
            request.engines = [preferred]
        } else if let first = ids.first {
            request.engines = [first]
        }
    }

    private func engineMembership(_ item: String) -> Binding<Bool> {
        Binding(get: { request.engines.contains(item) }, set: { enabled in
            userEditedEngines = true
            if enabled { if !request.engines.contains(item) { request.engines.append(item) } }
            else { request.engines.removeAll { $0 == item } }
        })
    }

    private func membership(_ item: String, in values: Binding<[String]>) -> Binding<Bool> {
        Binding(get: { values.wrappedValue.contains(item) }, set: { enabled in
            if enabled { if !values.wrappedValue.contains(item) { values.wrappedValue.append(item) } }
            else { values.wrappedValue.removeAll { $0 == item } }
        })
    }

    private func numberField(_ value: Binding<Int>) -> some View {
        TextField("", value: value, format: .number).frame(minWidth: 44)
    }
}
