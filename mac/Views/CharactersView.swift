import SwiftUI

struct CharactersView: View {
    @Environment(StudioStore.self) private var store
    @State private var search = ""

    private var filteredCharacters: [CharacterInfo] {
        store.models.filter { character in
            search.isEmpty
                || characterName(character.slug).localizedCaseInsensitiveContains(search)
                || character.slug.localizedCaseInsensitiveContains(search)
        }
    }

    private var selectedSlug: String {
        if store.models.contains(where: { $0.slug == store.params.character }) {
            return store.params.character
        }
        return store.models.first?.slug ?? ""
    }

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 12) {
            HSplitView {
                characterSidebar
                    .frame(minWidth: 175, idealWidth: 205, maxWidth: 250)
                characterDetail
                    .frame(minWidth: 520, maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .task(id: store.api.baseURL + "|" + store.models.map(\.slug).joined(separator: ",")) {
            await preloadCharacterSummaries()
        }
    }

    private var characterSidebar: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("角色").font(.headline)
                Spacer()
                Text("\(store.models.count)").font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            TextField("搜索角色…", text: $search)
                .textFieldStyle(.roundedBorder)

            List(selection: Binding(
                get: { Optional(selectedSlug) },
                set: { slug in
                    if let slug, !slug.isEmpty { store.selectCharacter(slug) }
                }
            )) {
                ForEach(filteredCharacters, id: \.slug) { character in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(characterName(character.slug)).fontWeight(.medium)
                            Spacer(minLength: 4)
                            if character.source == "user" {
                                Text("自训")
                                    .font(.caption2)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 1)
                                    .background(.quaternary, in: Capsule())
                            }
                        }
                        Text(defaultVersionSummary(for: character))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        if let count = store.refCountsByCharacter[character.slug] {
                            Text("\(count) 条参考").font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                        }
                    }
                    .padding(.vertical, 4)
                    .tag(character.slug)
                }
            }
            .listStyle(.inset)
        }
        .padding(.trailing, 10)
    }

    @ViewBuilder
    private var characterDetail: some View {
        @Bindable var store = store
        if selectedSlug.isEmpty {
            ContentUnavailableView(
                "暂无角色",
                systemImage: "person.2",
                description: Text("请先在设置中选择资料库，或训练一个新角色。")
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        characterHeading
                        Spacer(minLength: 8)
                        characterTabPicker
                            .frame(minWidth: 260, maxWidth: 340)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        characterHeading
                        characterTabPicker
                            .frame(maxWidth: .infinity)
                    }
                }
                Divider()
                Group {
                    switch store.characterTab {
                    case .versions:
                        CharacterVersionsView(character: selectedSlug, manager: store.weightManager)
                    case .references:
                        ReferencesView(embeddedCharacter: selectedSlug)
                    case .blind:
                        BlindView(embeddedCharacter: selectedSlug)
                    case .stats:
                        StatsView(embeddedCharacter: selectedSlug)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .padding(.leading, 12)
        }
    }

    private var characterHeading: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(characterName(selectedSlug)).font(.title2.bold()).lineLimit(1)
            Text(selectedSlug).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private var characterTabPicker: some View {
        @Bindable var store = store
        return Picker("子视图", selection: $store.characterTab) {
            ForEach(CharacterTab.allCases) { Text($0.rawValue).tag($0) }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    private func defaultVersionSummary(for character: CharacterInfo) -> String {
        if let rec = store.characterVersionsCache[character.slug]?.first(where: \.recommended)
            ?? store.characterVersionsCache[character.slug]?.first {
            return "\(rec.label) · \(displayEngine(rec.engine))"
        }
        if let firstEngine = sortedEngines(character.engines.keys).first {
            return displayEngine(firstEngine)
        }
        return "待配置模型"
    }

    private func preloadCharacterSummaries() async {
        for character in store.models {
            if Task.isCancelled { return }
            if store.characterVersionsCache[character.slug] == nil {
                _ = try? await store.loadVersions(for: character.slug)
            }
            if store.refCountsByCharacter[character.slug] == nil {
                if let list: RefList = try? await store.api.get("/refs", query: ["character": character.slug]) {
                    store.refCountsByCharacter[character.slug] = list.count
                }
            }
        }
    }
}
