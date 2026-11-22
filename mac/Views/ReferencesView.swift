import SwiftUI
import UniformTypeIdentifiers

struct ReferencesView: View {
    @Environment(StudioStore.self) private var store
    var embeddedCharacter: String? = nil

    @AppStorage("refsSort") private var sort = "id"
    @AppStorage("refsOrder") private var order = "asc"
    @State private var standaloneCharacter: String?
    @State private var search = ""
    @State private var emotion = ""
    @State private var minimum = 0.0
    @State private var maximum = 15.0
    @State private var uploading = false
    @State private var refreshID = 0
    @AppStorage("tagKeys") private var tagKeys = "q,w,e,r,t"
    @AppStorage("tagNames") private var tagNames = "开心,生气,害羞,低落,平静"

    private var activeCharacter: String {
        if let embeddedCharacter { return embeddedCharacter }
        if let standaloneCharacter, store.models.contains(where: { $0.slug == standaloneCharacter }) {
            return standaloneCharacter
        }
        return store.character?.slug ?? store.models.first?.slug ?? ""
    }
    private var defaultRefID: String? { store.models.first(where: { $0.slug == activeCharacter })?.default_ref }
    private var mapping: [(String, String)] {
        let keys = tagKeys.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
        let names = tagNames.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        return Array(zip(keys, names)).filter { !$0.0.isEmpty && !$0.1.isEmpty }
    }
    private var filtered: [Ref] {
        store.refs.filter { ref in
            ref.character == activeCharacter &&
            (search.isEmpty || ref.text.localizedCaseInsensitiveContains(search) || ref.label.localizedCaseInsensitiveContains(search) || ref.id.contains(search)) &&
            (emotion.isEmpty || ref.tags.contains(emotion)) && ref.duration >= minimum && ref.duration <= maximum
        }
    }

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 12) {
            if embeddedCharacter == nil {
                PageHeading(title: "参考音频", subtitle: "进入时保持安静；点击或上下键试听，按快捷键打标并试听下一条。")
            }
            WrappingHStack(horizontalSpacing: 8, verticalSpacing: 8) {
                if embeddedCharacter == nil {
                    characterFilterPicker.frame(minWidth: 110, maxWidth: 150)
                }
                TextField("搜索参考音频", text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 150, idealWidth: 200, maxWidth: 260)
                TextField("情绪筛选", text: $emotion)
                    .frame(minWidth: 80, idealWidth: 100, maxWidth: 120)
                    .textFieldStyle(.roundedBorder)
                durationFilterFields
                filterActionButtons
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) {
                    quickTagButtons
                    Spacer(minLength: 4)
                    sortAndCountBar
                }
                VStack(alignment: .leading, spacing: 6) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        quickTagButtons
                    }
                    HStack(spacing: 8) {
                        Spacer(minLength: 0)
                        sortAndCountBar
                    }
                }
            }
            List(selection: Binding(get: { store.selectedReferenceID }, set: { store.auditionReference($0) })) {
                ForEach(filtered) { ref in
                    let eligible = isPrimaryReferenceEligible(ref)
                    let isDefault = defaultRefID == ref.id
                    let trailingControls = HStack(spacing: 6) {
                        ForEach(ref.tags, id: \.self) { tag in
                            Button { removeTag(tag, ref: ref) } label: {
                                Label(tag, systemImage: "xmark.circle.fill").font(.caption)
                            }
                            .buttonStyle(.borderless)
                        }
                        Button(isDefault ? "★ 已默认" : "设为默认") {
                            store.perform { try await store.setCharacterDefaultRef(ref.id, for: activeCharacter) }
                        }
                        .controlSize(.small)
                        .disabled(isDefault || !eligible)
                        .help(primaryReferenceIneligibleReason(ref) ?? (isDefault ? "当前已是角色默认参考" : "设为此角色的默认主参考"))

                        Button("去合成") {
                            store.synthesizeWith(character: activeCharacter, refID: ref.id)
                        }
                        .controlSize(.small)
                        .disabled(!eligible)
                        .help(primaryReferenceIneligibleReason(ref) ?? "使用这条参考前往合成")
                    }.fixedSize()

                    let rowText = VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            if isDefault {
                                Image(systemName: "star.fill").foregroundStyle(.orange).font(.caption)
                            }
                            Text(ref.text.isEmpty ? ref.label : ref.text).lineLimit(2)
                        }
                        HStack(spacing: 8) {
                            Text(ref.label)
                            if let reason = primaryReferenceIneligibleReason(ref) {
                                Text("· \(reason)").foregroundStyle(.orange)
                            }
                        }
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onTapGesture { store.auditionReference(ref.id) }

                    let playButton = Button {
                        store.auditionReference(ref.id, restart: store.selectedReferenceID != ref.id)
                    } label: {
                        Image(systemName: store.audio.currentID == "ref-" + ref.id && store.audio.playing ? "pause.fill" : "play.fill")
                            .frame(width: 24)
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("试听参考 \(ref.label)")

                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .top, spacing: 12) {
                            playButton
                            rowText
                            trailingControls
                        }
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .top, spacing: 10) {
                                playButton
                                rowText
                            }
                            HStack {
                                Spacer(minLength: 0)
                                trailingControls
                            }
                        }
                    }
                    .padding(.vertical, 5)
                    .tag(ref.id)
                }
            }
            .listStyle(.inset)
            .onKeyPress(.upArrow) { store.moveReference(offset: -1, visibleIDs: filtered.map(\.id)); return .handled }
            .onKeyPress(.downArrow) { store.moveReference(offset: 1, visibleIDs: filtered.map(\.id)); return .handled }
            .onKeyPress(characters: CharacterSet(charactersIn: tagKeys.replacingOccurrences(of: ",", with: ""))) { key in
                if let match = mapping.first(where: { $0.0 == key.characters.lowercased() }) { applyTag(match.1); return .handled }
                return .ignored
            }
            DisclosureGroup("自定义速标快捷键（与设置同步）") {
                HStack {
                    TextField("按键（逗号分隔）", text: $tagKeys)
                    TextField("对应标签（逗号分隔）", text: $tagNames)
                }.textFieldStyle(.roundedBorder)
            }
            .font(.caption)
        }
        .padding(.horizontal, embeddedCharacter == nil ? 24 : 0)
        .padding(.vertical, embeddedCharacter == nil ? 12 : 0)
        .task(id: activeCharacter + "|" + sort + "|" + order + "|" + String(refreshID)) {
            guard !activeCharacter.isEmpty else { return }
            store.resetReferenceAudition()
            do {
                try await store.loadRefs(character: activeCharacter, sort: sort, order: order)
                store.selectedReferenceID = filtered.first?.id
            } catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .task(id: store.referenceAudition) {
            guard let request = store.referenceAudition, let ref = store.refs.first(where: { $0.id == request.referenceID }) else { return }
            do { try await store.playReference(ref, restart: request.restart) }
            catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .onDisappear { store.resetReferenceAudition() }
        .sheet(isPresented: $uploading) { ReferenceUploadView(character: activeCharacter) }
    }

    private var characterFilterPicker: some View {
        Picker("角色", selection: Binding(get: { activeCharacter }, set: { standaloneCharacter = $0 })) {
            ForEach(store.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
        }
    }

    private var durationFilterFields: some View {
        HStack(spacing: 4) {
            TextField("最短秒", value: $minimum, format: .number).frame(width: 48).textFieldStyle(.roundedBorder)
            Text("–")
            TextField("最长秒", value: $maximum, format: .number).frame(width: 48).textFieldStyle(.roundedBorder)
            Text("秒").foregroundStyle(.secondary).fixedSize()
        }
    }

    private var filterActionButtons: some View {
        HStack(spacing: 8) {
            Button("刷新") { refreshID += 1 }
            Button("上传参考", systemImage: "square.and.arrow.up") { uploading = true }
        }.fixedSize()
    }

    private var quickTagButtons: some View {
        HStack(spacing: 6) {
            ForEach(mapping, id: \.0) { key, name in
                Button("\(key.uppercased())  \(name)") { applyTag(name) }
                    .controlSize(.small)
                    .fixedSize()
                    .help("添加标签并自动试听下一条")
            }
        }
    }

    private var sortAndCountBar: some View {
        HStack(spacing: 8) {
            SortControls(options: ListSorting.references, field: $sort, order: $order)
            Text("\(filtered.count) 条").font(.caption).foregroundStyle(.secondary).monospacedDigit().fixedSize()
        }
    }

    private func applyTag(_ tag: String) {
        store.tagAndAdvance(tag, visibleIDs: filtered.map(\.id))
    }
    private func removeTag(_ tag: String, ref: Ref) {
        store.queueTag(ref, tag: tag, remove: true)
    }
}

struct ReferenceUploadView: View {
    @Environment(StudioStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let character: String
    @State private var file: URL?
    @State private var text = ""
    @State private var lang = "ja"
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("上传参考音频").font(.title2.bold())
            Button(file?.lastPathComponent ?? "选择音频…") {
                let panel = NSOpenPanel()
                panel.allowedContentTypes = [.audio]
                panel.canChooseDirectories = false
                if panel.runModal() == .OK { file = panel.url }
            }
            TextField("音频中的原文", text: $text, axis: .vertical).lineLimit(3...6)
            Picker("语言", selection: $lang) { Text("日语").tag("ja"); Text("中文").tag("zh"); Text("英语").tag("en") }
            if let error { Text(error).foregroundStyle(.red) }
            HStack {
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button(busy ? "上传中…" : "上传") {
                    guard let file else { return }
                    busy = true
                    Task {
                        let scoped = file.startAccessingSecurityScopedResource()
                        defer { if scoped { file.stopAccessingSecurityScopedResource() }; busy = false }
                        do {
                            _ = try await store.api.upload(file: file, text: text, character: character, lang: lang)
                            try await store.loadRefs(character: character)
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.buttonStyle(.borderedProminent).disabled(busy || file == nil || text.isEmpty)
            }
        }.textFieldStyle(.roundedBorder).padding(24).frame(width: 520)
    }
}
