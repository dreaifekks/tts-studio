import SwiftUI

struct GenerateView: View {
    @Environment(StudioStore.self) private var store
    @AppStorage("tagNames") private var tagNames = "开心,生气,害羞,低落,平静"
    @State private var debugItem: AudioItem?
    @State private var addingItem: AudioItem?
    @State private var showingAdvanced = false
    @State private var refSearch = ""
    @State private var selectedEmotionFilter = ""
    @State private var lastFixedSeed = 42
    @FocusState private var candidateFocus: Bool

    private var currentCharacterRefs: [Ref] {
        store.refs.filter { $0.character == store.params.character }
    }

    private var eligibleEmotions: [String] {
        eligibleEmotionTags(in: store.refs, for: store.params.character)
    }

    private var quickEmotions: [String] {
        var seen: Set<String> = []
        var result: [String] = []
        for tag in currentCharacterRefs.flatMap(\.tags) where !tag.isEmpty && seen.insert(tag).inserted {
            result.append(tag)
        }
        for raw in tagNames.split(separator: ",") {
            let tag = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if !tag.isEmpty && seen.insert(tag).inserted { result.append(tag) }
        }
        return result
    }

    private var filteredRefs: [Ref] {
        currentCharacterRefs.filter { ref in
            let matchesSearch = refSearch.isEmpty
                || ref.text.localizedCaseInsensitiveContains(refSearch)
                || ref.label.localizedCaseInsensitiveContains(refSearch)
                || ref.tags.contains(where: { $0.localizedCaseInsensitiveContains(refSearch) })
            let matchesEmotion = selectedEmotionFilter.isEmpty || ref.tags.contains(selectedEmotionFilter)
            return matchesSearch && matchesEmotion
        }
    }

    private var auxRefsEnabledForCurrentRun: Bool {
        switch store.generateMode {
        case .candidates:
            return store.currentEngineSupportsAuxRefs
        case .compare:
            let targets = store.comparisonEnginesList
            return !targets.isEmpty && targets.allSatisfy { store.engineSupportsAuxRefs($0) }
        }
    }

    private var emotionRandomBlockReason: String? {
        guard store.referenceMode == "情绪" else { return nil }
        if !store.hasValidEmotionReference {
            return "该情绪下没有 3–10 秒的可用参考音频"
        }
        return nil
    }

    private var canSubmitPrimary: Bool {
        guard !store.generationBusy,
              store.composerReady,
              emotionRandomBlockReason == nil,
              !store.params.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        if store.generateMode == .compare {
            return store.comparisonEnginesList.count >= 2
        }
        return true
    }

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 10) {
            topBar
            Divider()
            HSplitView {
                referenceColumn
                    .frame(minWidth: 200, idealWidth: 245, maxWidth: 340)
                composerColumn
                    .frame(minWidth: 260, idealWidth: 355, maxWidth: 500)
                resultsColumn
                    .frame(minWidth: 260, maxWidth: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .task { store.perform { try await store.loadWorks() } }
        .task(id: store.api.baseURL + "|" + store.params.character) {
            do {
                try await store.loadComposerVersions()
                try await store.loadRefs(character: store.params.character)
            } catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .task(id: store.params.character + "|" + store.params.engine) {
            do { try await store.loadComposerWeights() }
            catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .onChange(of: store.params.character) {
            selectedEmotionFilter = ""
        }
        .onChange(of: store.params.seed) {
            if store.params.seed >= 0 { lastFixedSeed = store.params.seed }
        }
        .sheet(item: $addingItem) { item in AddToWorkView(item: item) }
        .sheet(item: $debugItem) { item in DebugView(item: item) }
    }

    // MARK: - Top Bar

    private var topBar: some View {
        @Bindable var store = store
        return VStack(alignment: .leading, spacing: 8) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    modePicker.frame(width: 170)
                    characterPicker.frame(minWidth: 110, maxWidth: 150)
                    versionPicker.frame(minWidth: 180, maxWidth: 360)
                    Spacer(minLength: 4)
                    manageCharacterButton
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        modePicker.frame(minWidth: 140, maxWidth: 180)
                        characterPicker.frame(minWidth: 100, maxWidth: 160)
                        Spacer(minLength: 4)
                        manageCharacterButton
                    }
                    versionPicker
                        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                }
            }
            if let message = store.composerVersions.message {
                Text(message).font(.caption).foregroundStyle(.orange)
            }
        }
    }

    private var modePicker: some View {
        @Bindable var store = store
        return Picker("模式", selection: $store.generateMode) {
            ForEach(GenerateMode.allCases) { Text($0.rawValue).tag($0) }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    private var characterPicker: some View {
        Picker("角色", selection: Binding(
            get: { store.params.character },
            set: { store.selectCharacter($0) }
        )) {
            ForEach(store.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
        }
    }

    private var versionPicker: some View {
        ModelVersionPicker(
            rows: store.composerVersions.rows,
            selection: Binding(
                get: { store.composerVersions.selectedID },
                set: { store.selectComposerVersion($0) }
            ),
            loading: store.composerVersions.loading,
            manual: store.composerManual
        )
    }

    private var manageCharacterButton: some View {
        Button("管理此角色", systemImage: "person.crop.circle") {
            store.openCharacter(store.params.character, tab: .versions)
        }
        .fixedSize()
        .disabled(store.params.character.isEmpty)
    }

    // MARK: - Left Column: References

    private var referenceColumn: some View {
        @Bindable var store = store
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("参考音频").font(.headline)
                Spacer()
                Text("\(filteredRefs.count) 条").font(.caption).foregroundStyle(.secondary).monospacedDigit()
                Button {
                    store.perform { try await store.loadRefs(character: store.params.character) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("刷新参考音频")
            }

            TextField("搜索参考台词或标签…", text: $refSearch)
                .textFieldStyle(.roundedBorder)

            if !quickEmotions.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        emotionChip(title: "全部", value: "")
                        ForEach(quickEmotions, id: \.self) { tag in
                            emotionChip(title: tag, value: tag)
                        }
                    }
                    .padding(.vertical, 1)
                }
            }

            Toggle("按所选情绪随机", isOn: Binding(
                get: { store.referenceMode == "情绪" },
                set: { enabled in
                    if enabled {
                        store.referenceMode = "情绪"
                        if !selectedEmotionFilter.isEmpty && eligibleEmotions.contains(selectedEmotionFilter) {
                            store.emotion = selectedEmotionFilter
                        } else if !eligibleEmotions.contains(store.emotion), let first = eligibleEmotions.first {
                            store.emotion = first
                            selectedEmotionFilter = first
                        }
                    } else {
                        if let refID = store.params.ref_id, !refID.isEmpty {
                            store.referenceMode = "参考库"
                        } else {
                            store.referenceMode = "默认"
                        }
                    }
                    store.scheduleRefPrefsSave()
                }
            ))
            .font(.callout)

            if store.referenceMode == "情绪" {
                if eligibleEmotions.isEmpty {
                    Text("当前角色暂无带情绪标签的 3–10 秒可用参考音频。")
                        .font(.caption)
                        .foregroundStyle(.orange)
                } else {
                    HStack(spacing: 6) {
                        Text("情绪").font(.caption).foregroundStyle(.secondary)
                        Picker("随机情绪", selection: Binding(
                            get: { eligibleEmotions.contains(store.emotion) ? store.emotion : (eligibleEmotions.first ?? "") },
                            set: { store.emotion = $0; selectedEmotionFilter = $0 }
                        )) {
                            ForEach(eligibleEmotions, id: \.self) { tag in
                                Text(tag).tag(tag)
                            }
                        }
                        .labelsHidden()
                    }
                }
            }

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    defaultReferenceRow
                    ForEach(filteredRefs) { ref in
                        referenceRow(ref)
                    }
                    if filteredRefs.isEmpty {
                        ContentUnavailableView(
                            "没有匹配的参考",
                            systemImage: "waveform.slash",
                            description: Text("清空搜索或切换情绪筛选。")
                        )
                        .padding(.vertical, 24)
                    }
                }
                .padding(.trailing, 4)
            }

            if auxRefsEnabledForCurrentRun && !store.params.aux_ref_ids.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("辅助参考融合（\(store.params.aux_ref_ids.count)）")
                            .font(.caption.weight(.medium))
                        Spacer()
                        Button("清空") {
                            store.params.aux_ref_ids = []
                            store.scheduleRefPrefsSave()
                        }
                        .buttonStyle(.link)
                        .font(.caption)
                    }
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(store.params.aux_ref_ids, id: \.self) { auxID in
                                let label = store.refs.first(where: { $0.id == auxID })?.label ?? auxID
                                HStack(spacing: 4) {
                                    Text(label).lineLimit(1)
                                    Button {
                                        store.toggleAuxRef(auxID)
                                    } label: {
                                        Image(systemName: "xmark.circle.fill")
                                    }
                                    .buttonStyle(.plain)
                                }
                                .font(.caption)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3)
                                .background(Color.accentColor.opacity(0.12), in: Capsule())
                            }
                        }
                    }
                }
            } else if !auxRefsEnabledForCurrentRun && !store.params.aux_ref_ids.isEmpty {
                Text("当前引擎不支持辅助参考融合，本次合成将自动忽略已选的 \(store.params.aux_ref_ids.count) 条辅助参考。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Divider()
            HStack {
                Button("打标 / 管理参考…", systemImage: "tag") {
                    store.openCharacter(store.params.character, tab: .references)
                }
                .buttonStyle(.link)
                .font(.callout)
                Spacer()
            }
        }
        .padding(.trailing, 10)
    }

    private func emotionChip(title: String, value: String) -> some View {
        let active = selectedEmotionFilter == value
        return Button(title) {
            selectedEmotionFilter = value
            if store.referenceMode == "情绪", !value.isEmpty {
                store.emotion = value
            }
        }
        .buttonStyle(.bordered)
        .tint(active ? .accentColor : .secondary)
        .controlSize(.small)
    }

    private var defaultReferenceRow: some View {
        let selected = store.referenceMode == "默认" || (store.referenceMode == "参考库" && store.params.ref_id == nil)
        return Button {
            store.selectPrimaryRef(nil)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: selected ? "checkmark.circle.fill" : "star")
                    .foregroundStyle(selected ? Color.accentColor : .secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text("默认参考").font(.callout.weight(.medium))
                    Text(store.defaultReferenceLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? Color.accentColor.opacity(0.12) : Color(nsColor: .controlBackgroundColor),
                in: RoundedRectangle(cornerRadius: 8)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(selected ? Color.accentColor.opacity(0.45) : Color.secondary.opacity(0.18))
            )
        }
        .buttonStyle(.plain)
    }

    private func referenceRow(_ ref: Ref) -> some View {
        let eligible = isPrimaryReferenceEligible(ref)
        let isPrimary = store.referenceMode == "参考库" && store.params.ref_id == ref.id
        let isDefault = store.character?.default_ref == ref.id
        let isAux = store.params.aux_ref_ids.contains(ref.id)
        let isPlaying = store.audio.currentID == "ref-" + ref.id && store.audio.playing

        return HStack(alignment: .top, spacing: 8) {
            Button {
                store.perform { try await store.playReference(ref, restart: store.audio.currentID != "ref-" + ref.id) }
            } label: {
                Image(systemName: isPlaying ? "pause.fill" : "play.fill")
                    .frame(width: 20, height: 20)
            }
            .buttonStyle(.borderless)
            .help("试听参考")

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 4) {
                    if isDefault {
                        Image(systemName: "star.fill")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    }
                    Text(ref.text.isEmpty ? ref.label : ref.text)
                        .font(.callout)
                        .lineLimit(2)
                }
                HStack(spacing: 6) {
                    Text("\(displaySeconds(ref.duration))s · \(ref.lang)")
                        .monospacedDigit()
                    if !ref.tags.isEmpty {
                        Text(ref.tags.joined(separator: " / "))
                            .lineLimit(1)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                if let reason = primaryReferenceIneligibleReason(ref) {
                    Text(reason)
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture {
                guard eligible else { return }
                store.selectPrimaryRef(ref.id)
            }

            if auxRefsEnabledForCurrentRun {
                Button {
                    store.toggleAuxRef(ref.id)
                } label: {
                    Image(systemName: isAux ? "checkmark.circle.fill" : "plus.circle")
                        .foregroundStyle(isAux ? Color.accentColor : .secondary)
                }
                .buttonStyle(.borderless)
                .disabled(isPrimary || (isDefault && store.referenceMode == "默认"))
                .help(isAux ? "移出辅助参考融合" : "加入辅助参考融合")
            }
        }
        .padding(8)
        .background(
            isPrimary ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.03),
            in: RoundedRectangle(cornerRadius: 8)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(isPrimary ? Color.accentColor.opacity(0.5) : .clear)
        )
        .opacity(eligible ? 1.0 : 0.55)
    }

    // MARK: - Center Column: Text, Params & Compute

    private var composerColumn: some View {
        @Bindable var store = store
        return ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 8) {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) {
                            Text("台词").font(.headline).fixedSize()
                            Spacer(minLength: 4)
                            langPicker.frame(minWidth: 72, maxWidth: 92)
                            splitPicker.frame(minWidth: 105, maxWidth: 130)
                        }
                        VStack(alignment: .leading, spacing: 6) {
                            Text("台词").font(.headline)
                            HStack(spacing: 8) {
                                langPicker.frame(minWidth: 0, maxWidth: .infinity)
                                splitPicker.frame(minWidth: 0, maxWidth: .infinity)
                            }
                        }
                    }
                    TextEditor(text: $store.params.text)
                        .font(.body)
                        .frame(minHeight: 105)
                        .padding(5)
                        .overlay(RoundedRectangle(cornerRadius: 7).stroke(.quaternary))
                        .accessibilityIdentifier("generation-text")
                }

                if store.generateMode == .compare {
                    GroupBox("参与对比的引擎") {
                        VStack(alignment: .leading, spacing: 8) {
                            LazyVGrid(columns: [GridItem(.adaptive(minimum: 110), alignment: .leading)], spacing: 6) {
                                ForEach(store.sortedAllEngines) { engine in
                                    Toggle(
                                        displayEngine(engine.id),
                                        isOn: Binding(
                                            get: { engine.id == store.params.engine || store.compareEngines.contains(engine.id) },
                                            set: { enabled in
                                                if enabled { store.compareEngines.insert(engine.id) }
                                                else { store.compareEngines.remove(engine.id) }
                                            }
                                        )
                                    )
                                    .toggleStyle(.checkbox)
                                    .disabled(engine.id == store.params.engine)
                                }
                            }
                            if store.comparisonEnginesList.count < 2 {
                                Text("请至少再勾选 1 个引擎与当前引擎对比。")
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                            }
                        }
                        .padding(6)
                    }
                }

                GroupBox {
                    VStack(alignment: .leading, spacing: 10) {
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 8) {
                                Text("角色参数").font(.headline).fixedSize()
                                paramsStateBadge
                                Spacer(minLength: 4)
                                synthParamsButtons
                            }
                            VStack(alignment: .leading, spacing: 6) {
                                HStack(spacing: 8) {
                                    Text("角色参数").font(.headline).fixedSize()
                                    paramsStateBadge
                                    Spacer(minLength: 0)
                                }
                                HStack(spacing: 8) {
                                    synthParamsButtons
                                    Spacer(minLength: 0)
                                }
                            }
                        }

                        sliderRow(title: "语速", value: $store.params.speed, range: 0.5...2.0, step: 0.05, format: "%.2fx")
                        sliderRow(title: "分段间隔", value: $store.params.fragment_interval, range: 0.01...2.0, step: 0.01, format: "%.2fs")
                        sliderRow(title: "变化幅度", value: $store.params.temperature, range: 0.1...2.0, step: 0.05, format: "%.2f")
                        sliderRow(title: "重复抑制", value: $store.params.repetition_penalty, range: 1.0...2.5, step: 0.05, format: "%.2f")
                        intSliderRow(title: "Top K", value: $store.params.top_k, range: 1...100)
                        sliderRow(title: "Top P", value: $store.params.top_p, range: 0.1...1.0, step: 0.05, format: "%.2f")
                    }
                    .padding(6)
                }

                DisclosureGroup("进阶设置", isExpanded: $showingAdvanced) {
                    VStack(alignment: .leading, spacing: 10) {
                        WrappingHStack(horizontalSpacing: 8, verticalSpacing: 6) {
                            fixedSeedToggle
                            fixedSeedField
                        }

                        if store.currentEngineSupportsSampleSteps {
                            HStack(spacing: 8) {
                                Text("采样步数").fixedSize()
                                TextField("引擎默认", value: $store.params.sample_steps, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(minWidth: 72, maxWidth: 110)
                                Spacer(minLength: 0)
                            }
                        }

                        Divider()
                        Text("手动指定权重").font(.caption).foregroundStyle(.secondary)
                        ComposerManualWeightsView()
                    }
                    .padding(.top, 8)
                }

                HStack {
                    Stepper("\(store.params.n) 个候选", value: $store.params.n, in: 1...16)
                    Spacer()
                }
                .font(.callout)

                Button {
                    store.startPrimaryGeneration()
                    candidateFocus = true
                } label: {
                    Label(
                        store.generationBusy
                            ? (store.generateMode == .compare ? "正在对比…" : "正在生成…")
                            : (store.generateMode == .compare ? "开始对比（\(store.comparisonEnginesList.count) 个引擎）" : "出候选"),
                        systemImage: store.generateMode == .compare ? "slider.horizontal.3" : "waveform.badge.plus"
                    )
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 5)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canSubmitPrimary)
                .accessibilityIdentifier("generate-candidates")

                if let reason = emotionRandomBlockReason {
                    Label(reason, systemImage: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                if store.generateMode == .compare {
                    ComputePicker(
                        planner: store.comparisonPlanner,
                        input: store.comparisonPlanInput(engines: store.comparisonEnginesList),
                        action: "对比",
                        ready: store.composerReady && store.comparisonEnginesList.count >= 2
                    )
                } else {
                    ComputePicker(
                        planner: store.generationPlanner,
                        input: store.generationPlanInput,
                        action: "生成",
                        ready: store.composerReady
                    )
                }

                Text("⌘↩ \(store.generateMode == .compare ? "开始对比" : "出候选") · 候选区：空格试听，↑↓ 切换，1–5 评分")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if store.generationBusy {
                    Button("取消生成 / 停止等待") { store.stopGeneration() }
                }

                ForEach(store.activeJobs.filter { $0.kind == "candidates" || $0.kind == "compare" }) {
                    JobProgressView(job: $0)
                }
            }
            .padding(.horizontal, 10)
            .padding(.bottom, 12)
        }
    }

    private var langPicker: some View {
        @Bindable var store = store
        return Picker("语言", selection: $store.params.text_lang) {
            Text("日语").tag("ja")
            Text("中文").tag("zh")
            Text("英语").tag("en")
            Text("韩语").tag("ko")
            Text("粤语").tag("yue")
            Text("自动").tag("auto")
        }
        .labelsHidden()
    }

    private var splitPicker: some View {
        @Bindable var store = store
        return Picker("切句", selection: $store.params.text_split_method) {
            ForEach(["cut5", "cut0", "cut1", "cut2", "cut3", "cut4"], id: \.self) {
                Text(displaySplit($0)).tag($0)
            }
        }
        .labelsHidden()
    }

    @ViewBuilder
    private var synthParamsButtons: some View {
        Button("存为角色默认") {
            store.perform { try await store.saveCharacterSynthParams() }
        }
        .controlSize(.small)
        .fixedSize()
        .disabled(store.savingPrefs || store.params.character.isEmpty || store.currentSynthParamsState == .saved)
        Button("恢复默认") {
            store.perform { try await store.resetCharacterSynthParams() }
        }
        .controlSize(.small)
        .fixedSize()
        .disabled(store.savingPrefs || store.params.character.isEmpty || store.currentSynthParamsState == .default)
    }

    private var fixedSeedToggle: some View {
        Toggle("固定种子", isOn: Binding(
            get: { store.params.seed >= 0 },
            set: { fixed in
                store.params.seed = fixed ? max(0, lastFixedSeed) : -1
            }
        ))
        .fixedSize()
    }

    @ViewBuilder
    private var fixedSeedField: some View {
        @Bindable var store = store
        if store.params.seed >= 0 {
            TextField("种子", value: $store.params.seed, format: .number)
                .textFieldStyle(.roundedBorder)
                .frame(minWidth: 80, maxWidth: 115)
        } else {
            Text("每次随机").font(.caption).foregroundStyle(.secondary).fixedSize()
        }
    }

    private var paramsStateBadge: some View {
        let state = store.currentSynthParamsState
        let color: Color = switch state {
        case .saved: .green
        case .modified: .orange
        case .default: .secondary
        }
        return Text(state.label)
            .font(.caption2)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.14), in: Capsule())
            .foregroundStyle(color)
            .fixedSize()
    }

    private func sliderRow(title: String, value: Binding<Double>, range: ClosedRange<Double>, step: Double, format: String) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.callout).frame(width: 58, alignment: .leading)
            Slider(value: value, in: range, step: step)
            Text(String(format: format, value.wrappedValue))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .trailing)
        }
    }

    private func intSliderRow(title: String, value: Binding<Int>, range: ClosedRange<Int>) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.callout).frame(width: 58, alignment: .leading)
            Slider(
                value: Binding(
                    get: { Double(value.wrappedValue) },
                    set: { value.wrappedValue = Int($0.rounded()) }
                ),
                in: Double(range.lowerBound)...Double(range.upperBound),
                step: 1
            )
            Text("\(value.wrappedValue)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .trailing)
        }
    }

    // MARK: - Right Column: Candidates / History

    private var resultsColumn: some View {
        @Bindable var store = store
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("结果区", selection: $store.generateRightTab) {
                    Text("本次候选（\(store.candidates.count)）").tag(GenerateRightTab.candidates)
                    Text("历史记录").tag(GenerateRightTab.history)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                Spacer(minLength: 0)
            }

            if store.generateRightTab == .candidates {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            if store.candidates.isEmpty {
                                ContentUnavailableView(
                                    "等一句台词",
                                    systemImage: "waveform",
                                    description: Text("生成后，候选会按评分排序出现在这里。")
                                )
                                .padding(.top, 36)
                            }
                            ForEach(Array(store.candidates.enumerated()), id: \.element.id) { index, item in
                                CandidateCard(
                                    item: item,
                                    best: index == 0,
                                    selected: item.id == store.selectedCandidate,
                                    onSelect: { store.selectedCandidate = item.id; candidateFocus = true },
                                    onDebug: { debugItem = item },
                                    extra: {
                                        AnyView(
                                            Button("加入作品") { addingItem = item }
                                                .controlSize(.small)
                                                .disabled(store.workBusy)
                                        )
                                    }
                                )
                                .id(item.id)
                            }
                        }
                        .padding(.bottom, 12)
                    }
                    .focusable()
                    .focused($candidateFocus)
                    .onKeyPress(.space) {
                        if let item = store.currentCandidate {
                            store.perform { try await store.play(item) }
                            return .handled
                        }
                        return .ignored
                    }
                    .onKeyPress(.upArrow) { store.selectCandidate(offset: -1); return .handled }
                    .onKeyPress(.downArrow) { store.selectCandidate(offset: 1); return .handled }
                    .onKeyPress(characters: CharacterSet(charactersIn: "12345")) { key in
                        if let rating = Int(key.characters) { store.rateSelected(rating); return .handled }
                        return .ignored
                    }
                    .onChange(of: store.selectedCandidate) {
                        if let id = store.selectedCandidate { proxy.scrollTo(id, anchor: .center) }
                    }
                }
            } else {
                HistoryPanelView(onDebug: { debugItem = $0 })
            }
        }
        .padding(.leading, 10)
    }
}

struct ComposerAdvancedView: View {
    @Binding var compareEngines: Set<String>
    var body: some View {
        ComposerManualWeightsView()
    }
}

struct ComposerManualWeightsView: View {
    @Environment(StudioStore.self) private var store
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("引擎", selection: Binding(
                get: { store.params.engine },
                set: { store.useManualComposer(); store.params.engine = $0; store.params.gpt = nil; store.params.sovits = nil }
            )) {
                let installed = store.installedComputeEngines
                ForEach(store.sortedAllEngines) { EngineChoice(engine: $0, installed: installed.isEmpty || installed.contains($0.id)) }
            }
            Picker("GPT", selection: Binding(
                get: { store.params.gpt },
                set: { store.useManualComposer(); store.params.gpt = $0 }
            )) {
                Text("默认组合").tag(String?.none)
                if let name = store.params.gpt, !store.composerWeights.contains(where: { $0.type == "gpt" && $0.name == name }) {
                    Text("\(store.composerVersions.selected?.label ?? "当前模型") · GPT").tag(Optional(name))
                }
                ForEach(store.composerWeights.filter { $0.type == "gpt" }) { Text($0.modelLabel).tag(Optional($0.name)) }
            }
            Picker("SoVITS", selection: Binding(
                get: { store.params.sovits },
                set: { store.useManualComposer(); store.params.sovits = $0 }
            )) {
                Text("默认组合").tag(String?.none)
                if let name = store.params.sovits, !store.composerWeights.contains(where: { $0.type == "sovits" && $0.name == name }) {
                    Text("\(store.composerVersions.selected?.label ?? "当前模型") · SoVITS").tag(Optional(name))
                }
                ForEach(store.composerWeights.filter { $0.type == "sovits" }) { Text($0.modelLabel).tag(Optional($0.name)) }
            }
        }
    }
}
