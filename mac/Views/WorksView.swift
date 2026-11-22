import SwiftUI

struct WorksView: View {
    @Environment(StudioStore.self) private var store
    @AppStorage("worksSort") private var sort = "created"
    @AppStorage("worksOrder") private var order = "desc"
    @State private var newTitle = ""
    @State private var srt: String?
    @State private var deleteConfirmation = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            PageHeading(title: "作品", subtitle: "把台词排成一段对话，逐行挑选声音，再渲染成整轨。")
            HSplitView {
                VStack(spacing: 10) {
                    HStack {
                        TextField("新作品名称", text: $newTitle).textFieldStyle(.roundedBorder)
                        Button {
                            store.perform {
                                if store.workDirty { try await store.saveWork() }
                                try await store.createWork(title: newTitle.isEmpty ? "未命名作品" : newTitle)
                                newTitle = ""
                            }
                        } label: { Image(systemName: "plus") }
                            .help("新建作品")
                            .accessibilityLabel("新建作品")
                            .accessibilityIdentifier("create-work")
                    }
                    SortControls(options: ListSorting.works, field: $sort, order: $order)
                    List(selection: Binding(get: { store.work?.id }, set: { id in
                        if let id {
                            store.perform {
                                if store.workDirty { try await store.saveWork() }
                                try await store.openWork(id)
                                store.selectedWorkLineIDs = []
                            }
                        }
                    })) {
                        ForEach(store.works) { work in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(work.title).fontWeight(.medium)
                                Text("\(work.n_lines) 行 · \(work.n_picked) 行已选")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 4)
                            .tag(work.id)
                        }
                    }
                    .listStyle(.inset)
                }
                .frame(maxHeight: .infinity, alignment: .top)
                .frame(minWidth: 180, idealWidth: 205, maxWidth: 250)
                .padding(.trailing, 10)
                .disabled(store.workBusy || store.workSaving)

                if store.work != nil {
                    editor
                        .padding(.leading, 12)
                        .frame(minWidth: 520)
                        .frame(maxHeight: .infinity, alignment: .top)
                } else {
                    ContentUnavailableView(
                        "做一段对话",
                        systemImage: "text.book.closed",
                        description: Text("新建作品，然后添加台词。")
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(maxHeight: .infinity, alignment: .top)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .task(id: sort + "|" + order) {
            do { try await store.loadWorks() }
            catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .onDisappear { store.workPlanner.resolve(false) }
        .confirmationDialog("删除这个作品？", isPresented: $deleteConfirmation) {
            Button("删除作品", role: .destructive) { store.perform { try await store.deleteWork() } }
        }
        .sheet(isPresented: Binding(get: { srt != nil }, set: { if !$0 { srt = nil } })) {
            VStack(alignment: .leading) {
                HStack { Text("SRT 字幕").font(.title2.bold()); Spacer(); Button("完成") { srt = nil } }
                ScrollView {
                    Text(srt ?? "")
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 12)
            .frame(width: 650, height: 500)
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                TextField("作品名称", text: Binding(
                    get: { store.work?.title ?? "" },
                    set: { store.work?.title = $0; store.markWorkEdited() }
                ))
                .font(.title2.bold())
                .textFieldStyle(.plain)
                .disabled(store.workBusy || store.workSaving)
                if store.workDirty { Text("未保存").font(.caption).foregroundStyle(.orange) }
                Button(store.workSaving ? "保存中…" : "保存") { store.perform { try await store.saveWork() } }.disabled(store.workBusy || store.workSaving)
                Button(role: .destructive) { deleteConfirmation = true } label: { Image(systemName: "trash") }
                    .help("删除作品")
                    .accessibilityLabel("删除作品")
                    .disabled(store.workBusy || store.workSaving)
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    addLineButton
                    Spacer(minLength: 4)
                    workActionButtons
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        addLineButton
                        Spacer(minLength: 0)
                    }
                    HStack(spacing: 8) {
                        workActionButtons
                        Spacer(minLength: 0)
                    }
                }
            }
            .disabled(store.workBusy || store.workSaving)

            ComputePicker(
                planner: store.workPlanner,
                input: store.workPickerInput,
                action: store.workPickerAction,
                contextKey: store.work?.id ?? "",
                taskTitle: store.work?.title ?? "",
                ready: store.work?.lines.contains { !$0.text.isEmpty } == true
            )

            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(store.work?.lines ?? []) { line in
                        WorkLineView(
                            line: lineBinding(line),
                            selected: Binding(
                                get: { store.selectedWorkLineIDs.contains(line.id) },
                                set: {
                                    if $0 { store.selectedWorkLineIDs.insert(line.id) }
                                    else { store.selectedWorkLineIDs.remove(line.id) }
                                }
                            ),
                            remove: {
                                store.work?.lines.removeAll { $0.id == line.id }
                                store.invalidateRender()
                                store.selectedWorkLineIDs.remove(line.id)
                                store.markWorkEdited()
                            }
                        )
                        .draggable(line.id)
                        .dropDestination(for: String.self) { items, _ in
                            guard let from = items.first,
                                  let source = store.work?.lines.firstIndex(where: { $0.id == from }),
                                  let destination = store.work?.lines.firstIndex(where: { $0.id == line.id }) else { return false }
                            store.work?.lines.move(fromOffsets: IndexSet(integer: source), toOffset: destination > source ? destination + 1 : destination)
                            store.invalidateRender()
                            store.markWorkEdited()
                            return true
                        }
                    }
                    if store.work?.lines.isEmpty == true {
                        Button("添加第一行", systemImage: "plus") { store.addLine() }.padding(40)
                    }
                }
                .padding(.bottom, 12)
            }
            .disabled(store.workBusy || store.workSaving)

            if store.workBusy { Button("取消任务 / 停止等待") { store.stopWork() } }
            ForEach(store.activeJobs.filter { $0.target == store.work?.id }) { JobProgressView(job: $0) }

            if let render = store.work?.render, !store.workDirty, store.work?.lines.allSatisfy({ !store.needsRegeneration($0) }) == true {
                Divider()
                HStack {
                    Button("播放整轨", systemImage: "play.fill") {
                        store.perform { try await store.play(path: render.wav_url, id: render.wav_url) }
                    }
                    .accessibilityIdentifier("play-work")
                    Text("\(number(render.duration)) 秒").foregroundStyle(.secondary)
                    Button("查看 SRT") {
                        store.perform {
                            let data = try await store.api.data(store.api.request(render.srt_url))
                            srt = String(decoding: data, as: UTF8.self)
                        }
                    }
                    Spacer()
                    Menu("导出") {
                        Button("WAV…") { export(render.wav_url, "wav") }
                        Button("MP3…") { export(render.mp3_url, "mp3") }
                        Button("SRT…") { export(render.srt_url, "srt") }
                        Button("LRC…") { export(render.lrc_url, "lrc") }
                    }
                    .accessibilityIdentifier("export-work")
                }
                Text("拖动行左侧手柄可排序。生成全部会为每行重新生成候选；选中行可单独生成。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var addLineButton: some View {
        Button("添加行", systemImage: "plus") { store.addLine() }
            .fixedSize()
    }

    @ViewBuilder
    private var workActionButtons: some View {
        Button("生成选中行") {
            store.performWork { try await store.generateWork(lineIDs: Array(store.selectedWorkLineIDs)) }
        }
        .fixedSize()
        .disabled(store.selectedWorkLineIDs.isEmpty)
        Button("生成全部") {
            store.performWork { try await store.generateWork(lineIDs: store.work?.lines.map(\.id)) }
        }
        .fixedSize()
        .accessibilityIdentifier("generate-work")
        Button("渲染", systemImage: "waveform") {
            store.performWork { try await store.renderWork() }
        }
        .buttonStyle(.borderedProminent)
        .fixedSize()
        .disabled(store.work?.lines.contains { store.needsRegeneration($0) } != false || store.workDirty)
        .accessibilityIdentifier("render-work")
    }

    private func lineBinding(_ line: WorkLine) -> Binding<WorkLine> {
        Binding(
            get: { store.work?.lines.first { $0.id == line.id } ?? line },
            set: { value in store.changeLine(value) }
        )
    }

    private func export(_ path: String, _ ext: String) {
        store.perform { try await store.export(path: path, extension: ext) }
    }
}

struct WorkLineView: View {
    @Environment(StudioStore.self) private var store
    @Binding var line: WorkLine
    @Binding var selected: Bool
    let remove: () -> Void
    @State private var expanded = false
    @State private var showingRefPicker = false
    @State private var showingParamsPopover = false

    private var cachedVersions: [ModelVersion] {
        store.characterVersionsCache[line.character] ?? []
    }

    private var versionSummary: String {
        if let vid = line.version, let match = cachedVersions.first(where: { $0.id == vid }) {
            return "\(match.label) · \(displayEngine(match.engine))"
        }
        return displayEngine(line.engine)
    }

    private var referenceSummary: String {
        var base: String
        if let refID = line.ref_id, !refID.isEmpty {
            let refs = store.refsByCharacterCache[line.character] ?? (store.refs.first?.character == line.character ? store.refs : [])
            base = refs.first(where: { $0.id == refID })?.label ?? "已选参考"
        } else if let emotion = line.emotion, !emotion.isEmpty {
            base = "情绪：\(emotion)"
        } else {
            base = "默认参考"
        }
        if !line.aux_ref_ids.isEmpty && store.engineSupportsAuxRefs(line.engine) {
            base += " +\(line.aux_ref_ids.count) 辅参"
        }
        return base
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "line.3.horizontal").foregroundStyle(.secondary).help("拖动排序")
                Toggle("选中行", isOn: $selected).labelsHidden()
                Picker("角色", selection: Binding(
                    get: { line.character },
                    set: { newSlug in
                        var updated = line
                        updated.character = newSlug
                        updated.gpt = nil
                        updated.sovits = nil
                        updated.version = nil
                        updated.ref_id = nil
                        updated.emotion = nil
                        if let prefs = store.models.first(where: { $0.slug == newSlug })?.prefs {
                            updated.fragment_interval = prefs.synth_params.fragment_interval ?? 0.3
                            updated.lang = prefs.synth_params.text_lang ?? updated.lang
                            updated.aux_ref_ids = store.engineSupportsAuxRefs(updated.engine) ? prefs.aux_ref_ids : []
                        } else {
                            updated.aux_ref_ids = []
                        }
                        if let rec = store.characterVersionsCache[newSlug]?.first(where: \.recommended) {
                            updated.version = rec.id
                            updated.engine = rec.engine
                            updated.gpt = rec.gpt
                            updated.sovits = rec.sovits
                            if !store.engineSupportsAuxRefs(rec.engine) { updated.aux_ref_ids = [] }
                        }
                        line = updated
                    }
                )) {
                    ForEach(store.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
                }
                .labelsHidden()
                .frame(width: 92)

                TextField("台词", text: $line.text, axis: .vertical)
                    .lineLimit(1...4)
                    .textFieldStyle(.roundedBorder)

                Menu {
                    if !cachedVersions.isEmpty {
                        Section("已配对版本") {
                            ForEach(cachedVersions) { version in
                                Button {
                                    var updated = line
                                    updated.version = version.id
                                    updated.engine = version.engine
                                    updated.gpt = version.gpt
                                    updated.sovits = version.sovits
                                    if !store.engineSupportsAuxRefs(version.engine) { updated.aux_ref_ids = [] }
                                    line = updated
                                } label: {
                                    Label(version.optionLabel, systemImage: line.version == version.id ? "checkmark" : "")
                                }
                            }
                        }
                    }
                    Section("按引擎默认") {
                        ForEach(store.sortedAllEngines) { engine in
                            Button {
                                var updated = line
                                updated.version = nil
                                updated.engine = engine.id
                                updated.gpt = nil
                                updated.sovits = nil
                                if !engine.supports_aux_refs { updated.aux_ref_ids = [] }
                                line = updated
                            } label: {
                                Label(displayEngine(engine.id), systemImage: (line.version == nil && line.engine == engine.id) ? "checkmark" : "")
                            }
                        }
                    }
                } label: {
                    Text(versionSummary).lineLimit(1)
                }
                .frame(minWidth: 96, maxWidth: 160)

                Button(action: remove) { Image(systemName: "minus.circle") }
                    .buttonStyle(.borderless)
                    .help("删除行")
                    .accessibilityLabel("删除行")
            }

            WrappingHStack(horizontalSpacing: 10, verticalSpacing: 8, alignLastTrailingOnSingleLine: true) {
                HStack(spacing: 8) {
                    refAndIntervalControls
                }
                HStack(spacing: 8) {
                    pauseAndRegenerateControls
                }
            }
            .font(.callout)

            if line.picked != nil && store.needsRegeneration(line) {
                Text("台词或声音设置已修改，请重新生成此行后再渲染。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            DisclosureGroup("候选（\(line.candidates.count)）", isExpanded: $expanded) {
                VStack(spacing: 8) {
                    ForEach(line.candidates) { item in
                        CandidateCard(
                            item: item,
                            best: line.candidates.first(where: { store.canPick($0, for: line) })?.id == item.id,
                            extra: {
                                AnyView(
                                    Button(line.picked == item.id ? "已选" : "选用") {
                                        let snapshot = line
                                        store.perform { try await store.pick(item, line: snapshot) }
                                    }
                                    .disabled(line.picked == item.id || store.workDirty || !store.canPick(item, for: line))
                                )
                            }
                        )
                        if !store.canPick(item, for: line) {
                            Text("旧台词或声音设置，仅供试听").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.top, 8)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
        .task(id: line.character) {
            if store.characterVersionsCache[line.character] == nil {
                _ = try? await store.loadVersions(for: line.character)
            }
        }
    }

    @ViewBuilder
    private var refAndIntervalControls: some View {
        Button {
            showingRefPicker = true
        } label: {
            Label(referenceSummary, systemImage: "waveform")
                .lineLimit(1)
        }
        .popover(isPresented: $showingRefPicker, arrowEdge: .bottom) {
            WorkLineReferencePopover(line: $line)
                .environment(store)
        }

        Button {
            showingParamsPopover = true
        } label: {
            Text("间隔 \(String(format: "%.2fs", line.fragment_interval))")
                .monospacedDigit()
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .fixedSize()
        .popover(isPresented: $showingParamsPopover, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 10) {
                Text("行内分段间隔").font(.headline)
                HStack {
                    Slider(value: $line.fragment_interval, in: 0.01...2.0, step: 0.01)
                    Text(String(format: "%.2fs", line.fragment_interval))
                        .font(.caption.monospacedDigit())
                        .frame(width: 46, alignment: .trailing)
                }
                HStack {
                    Spacer()
                    Button("恢复 0.30s") { line.fragment_interval = 0.3 }
                        .controlSize(.small)
                }
            }
            .padding(14)
            .frame(width: 260)
        }
    }

    @ViewBuilder
    private var pauseAndRegenerateControls: some View {
        HStack(spacing: 6) {
            Text("停顿").foregroundStyle(.secondary).fixedSize()
            TextField("秒", value: $line.pause_after, format: .number)
                .textFieldStyle(.roundedBorder)
                .frame(width: 54)
            Text("秒").foregroundStyle(.secondary).fixedSize()
        }
        Button("重新生成") {
            let snapshot = line
            store.performWork { try await store.regenerate(snapshot) }
        }
        .fixedSize()
        .disabled(line.id.hasPrefix("draft-") || line.text.isEmpty || store.workBusy)
    }
}

struct WorkLineReferencePopover: View {
    @Environment(StudioStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Binding var line: WorkLine

    @State private var refs: [Ref] = []
    @State private var loading = false
    @State private var search = ""

    private var supportsAux: Bool { store.engineSupportsAuxRefs(line.engine) }

    private var emotions: [String] {
        eligibleEmotionTags(in: refs, for: line.character)
    }

    private var filteredRefs: [Ref] {
        refs.filter { ref in
            search.isEmpty
                || ref.text.localizedCaseInsensitiveContains(search)
                || ref.label.localizedCaseInsensitiveContains(search)
                || ref.tags.contains(where: { $0.localizedCaseInsensitiveContains(search) })
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("选择参考 · \(characterName(line.character))").font(.headline)
                Spacer()
                Button("完成") { dismiss() }.controlSize(.small)
            }

            HStack(spacing: 8) {
                Button {
                    var updated = line
                    updated.ref_id = nil
                    updated.emotion = nil
                    line = updated
                    dismiss()
                } label: {
                    Label("默认参考", systemImage: (line.ref_id == nil && line.emotion == nil) ? "checkmark.circle.fill" : "star")
                }
                .buttonStyle(.bordered)
                .tint((line.ref_id == nil && line.emotion == nil) ? .accentColor : .secondary)

                if supportsAux && !line.aux_ref_ids.isEmpty {
                    Spacer()
                    Button("清空辅参（\(line.aux_ref_ids.count)）") {
                        var updated = line
                        updated.aux_ref_ids = []
                        line = updated
                    }
                    .controlSize(.small)
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("按情绪随机（仅含 3–10 秒可用参考）").font(.caption).foregroundStyle(.secondary)
                if emotions.isEmpty {
                    Text("暂无带情绪标签的 3–10 秒参考音频").font(.caption).foregroundStyle(.secondary)
                } else {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(emotions, id: \.self) { tag in
                                let active = line.ref_id == nil && line.emotion == tag
                                Button(tag) {
                                    var updated = line
                                    updated.ref_id = nil
                                    updated.emotion = tag
                                    line = updated
                                    dismiss()
                                }
                                .buttonStyle(.bordered)
                                .tint(active ? .accentColor : .secondary)
                                .controlSize(.small)
                            }
                        }
                    }
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("选定具体参考").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if loading { ProgressView().controlSize(.mini) }
                }
                TextField("搜索参考台词或标签…", text: $search)
                    .textFieldStyle(.roundedBorder)

                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        ForEach(filteredRefs) { ref in
                            let eligible = isPrimaryReferenceEligible(ref)
                            let isSelected = line.ref_id == ref.id
                            let isAux = line.aux_ref_ids.contains(ref.id)
                            let isPlaying = store.audio.currentID == "ref-" + ref.id && store.audio.playing
                            HStack(alignment: .top, spacing: 8) {
                                Button {
                                    store.perform { try await store.playReference(ref, restart: store.audio.currentID != "ref-" + ref.id) }
                                } label: {
                                    Image(systemName: isPlaying ? "pause.fill" : "play.fill")
                                        .frame(width: 18, height: 18)
                                }
                                .buttonStyle(.borderless)

                                VStack(alignment: .leading, spacing: 2) {
                                    Text(ref.text.isEmpty ? ref.label : ref.text)
                                        .font(.callout)
                                        .lineLimit(2)
                                    HStack(spacing: 6) {
                                        Text("\(displaySeconds(ref.duration))s · \(ref.lang)").monospacedDigit()
                                        if !ref.tags.isEmpty { Text(ref.tags.joined(separator: " / ")) }
                                    }
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    if let reason = primaryReferenceIneligibleReason(ref) {
                                        Text(reason).font(.caption2).foregroundStyle(.orange)
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    guard eligible else { return }
                                    var updated = line
                                    updated.ref_id = ref.id
                                    updated.emotion = nil
                                    updated.aux_ref_ids.removeAll { $0 == ref.id }
                                    line = updated
                                    dismiss()
                                }

                                if supportsAux {
                                    Button {
                                        var updated = line
                                        if updated.aux_ref_ids.contains(ref.id) {
                                            updated.aux_ref_ids.removeAll { $0 == ref.id }
                                        } else if updated.ref_id != ref.id {
                                            updated.aux_ref_ids.append(ref.id)
                                        }
                                        line = updated
                                    } label: {
                                        Image(systemName: isAux ? "checkmark.circle.fill" : "plus.circle")
                                            .foregroundStyle(isAux ? Color.accentColor : .secondary)
                                    }
                                    .buttonStyle(.borderless)
                                    .disabled(isSelected)
                                    .help(isAux ? "移出辅助参考" : "加入辅助参考")
                                }
                            }
                            .padding(6)
                            .background(isSelected ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.03), in: RoundedRectangle(cornerRadius: 6))
                            .opacity(eligible ? 1 : 0.55)
                        }
                        if filteredRefs.isEmpty && !loading {
                            Text("没有匹配的参考音频").font(.caption).foregroundStyle(.secondary).padding(.vertical, 12)
                        }
                    }
                }
                .frame(height: 240)
            }
        }
        .padding(14)
        .frame(width: 400)
        .task(id: line.character) {
            if let cached = store.refsByCharacterCache[line.character] {
                refs = cached
            }
            loading = true
            defer { loading = false }
            if let fetched = try? await store.fetchRefs(for: line.character) {
                refs = fetched
            }
        }
    }
}
