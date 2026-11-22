import SwiftUI

struct HistoryView: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            PageHeading(title: "历史记录", subtitle: "找回生成过的声音，补充评价，一键复用参数与种子。")
            HistoryPanelView()
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
    }
}

struct HistoryPanelView: View {
    @Environment(StudioStore.self) private var store
    @AppStorage("historySort") private var historySort = "created"
    @AppStorage("historyOrder") private var historyOrder = "desc"
    @AppStorage("reviewsSort") private var reviewsSort = "created"
    @AppStorage("reviewsOrder") private var reviewsOrder = "desc"
    var onDebug: ((AudioItem) -> Void)?

    @State private var scopeCurrentCharacter = true
    @State private var reviewed = ""
    @State private var minimumRating = 0
    @State private var search = ""
    @State private var offset = 0
    @State private var loading = false
    @State private var addingItem: AudioItem?
    @State private var metadata: AudioItem?

    private var sort: String { minimumRating > 0 ? reviewsSort : historySort }
    private var order: String { minimumRating > 0 ? reviewsOrder : historyOrder }
    private var sortBinding: Binding<String> {
        Binding(
            get: { sort },
            set: { if minimumRating > 0 { reviewsSort = $0 } else { historySort = $0 } }
        )
    }
    private var orderBinding: Binding<String> {
        Binding(
            get: { order },
            set: { if minimumRating > 0 { reviewsOrder = $0 } else { historyOrder = $0 } }
        )
    }
    private var characterFilter: String {
        scopeCurrentCharacter ? store.params.character : ""
    }
    private var queryKey: String {
        "\(store.api.baseURL)|\(characterFilter)|\(reviewed)|\(minimumRating)|\(search)|\(offset)|\(sort)|\(order)|\(store.reviewRevision)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("搜索台词…", text: $search)
                .textFieldStyle(.roundedBorder)

            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    scopePicker.frame(minWidth: 130, maxWidth: 155)
                    reviewedPicker.frame(minWidth: 68, maxWidth: 82)
                    ratingPicker.frame(minWidth: 84, maxWidth: 96)
                    Spacer(minLength: 4)
                    SortControls(options: ListSorting.audio, field: sortBinding, order: orderBinding)
                }
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        scopePicker.frame(minWidth: 0, maxWidth: .infinity)
                        SortControls(options: ListSorting.audio, field: sortBinding, order: orderBinding)
                    }
                    HStack(spacing: 8) {
                        reviewedPicker.frame(minWidth: 0, maxWidth: .infinity)
                        ratingPicker.frame(minWidth: 0, maxWidth: .infinity)
                    }
                }
            }

            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(store.history.items) { item in
                        CandidateCard(
                            item: item,
                            onDebug: onDebug.map { handler in { handler(item) } } ?? {
                                store.perform { metadata = try await store.api.get("/audio/\(item.id)/meta") }
                            },
                            extra: {
                                AnyView(
                                    HStack(spacing: 6) {
                                        Button("复用参数") {
                                            store.reuseHistoryItem(item)
                                        }
                                        .controlSize(.small)
                                        .help("复用台词、参考、采样参数与固定种子")
                                        Button("加入作品") {
                                            addingItem = item
                                        }
                                        .controlSize(.small)
                                        .disabled(store.workBusy)
                                    }
                                )
                            }
                        )
                    }
                    if store.history.items.isEmpty && !loading {
                        ContentUnavailableView(
                            "暂无历史记录",
                            systemImage: "clock",
                            description: Text("调整筛选条件，或先生成一组候选。")
                        )
                        .padding(.top, 28)
                    }
                }
                .padding(.bottom, 12)
            }

            HStack(spacing: 8) {
                if store.history.total > 0 {
                    Text("共 \(store.history.total) 条 · \(offset + 1)–\(min(offset + 30, store.history.total))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                } else {
                    Text("共 0 条").font(.caption).foregroundStyle(.secondary)
                }
                if loading { ProgressView().controlSize(.mini) }
                Spacer()
                Button("上一页") { offset = max(0, offset - 30) }
                    .controlSize(.small)
                    .disabled(offset == 0)
                Button("下一页") { offset += 30 }
                    .controlSize(.small)
                    .disabled(offset + 30 >= store.history.total)
            }
        }
        .task(id: queryKey) {
            loading = true
            defer { loading = false }
            do {
                try await Task.sleep(for: .milliseconds(220))
                var query = ["limit": "30", "offset": String(offset), "sort": sort, "order": order]
                if !characterFilter.isEmpty { query["character"] = characterFilter }
                if !search.isEmpty { query["q"] = search }
                if minimumRating > 0 { query["min_rating"] = String(minimumRating) }
                else if !reviewed.isEmpty { query["reviewed"] = reviewed }
                let result: AudioPage = try await store.api.get(minimumRating > 0 ? "/reviews" : "/history", query: query)
                try Task.checkCancellation()
                store.history = result
            } catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
        .onChange(of: sort) { offset = 0 }
        .onChange(of: order) { offset = 0 }
        .onChange(of: characterFilter) { offset = 0 }
        .onChange(of: reviewed) { offset = 0 }
        .onChange(of: minimumRating) { offset = 0 }
        .onChange(of: search) { offset = 0 }
        .sheet(item: $addingItem) { item in AddToWorkView(item: item) }
        .sheet(item: $metadata) { item in MetadataView(item: item) }
    }

    private var scopePicker: some View {
        Picker("角色范围", selection: $scopeCurrentCharacter) {
            Text("当前角色").tag(true)
            Text("全部角色").tag(false)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    private var reviewedPicker: some View {
        Picker("评价", selection: $reviewed) {
            Text("全部").tag("")
            Text("已评").tag("true")
            Text("未评").tag("false")
        }
        .labelsHidden()
        .disabled(minimumRating > 0)
    }

    private var ratingPicker: some View {
        Picker("最低星", selection: $minimumRating) {
            Text("不限星级").tag(0)
            ForEach(1...5, id: \.self) { Text("≥ \($0) ★").tag($0) }
        }
        .labelsHidden()
    }
}

struct MetadataView: View {
    @Environment(\.dismiss) private var dismiss
    let item: AudioItem
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("生成参数").font(.title2.bold())
                Spacer()
                Button("完成") { dismiss() }
            }
            Form {
                LabeledContent("角色", value: characterName(item.character))
                LabeledContent("引擎", value: displayEngine(item.engine))
                LabeledContent("种子", value: String(item.seed))
                LabeledContent("时长", value: "\(displaySeconds(item.duration)) 秒")
                LabeledContent("语速", value: number(item.speed, digits: 2))
                LabeledContent("分段间隔", value: "\(number(item.fragment_interval ?? 0.3, digits: 2)) 秒")
                LabeledContent("变化幅度", value: number(item.temperature, digits: 2))
                LabeledContent("重复抑制", value: number(item.repetition_penalty, digits: 2))
                LabeledContent("Top K", value: item.top_k.map(String.init) ?? "—")
                LabeledContent("Top P", value: number(item.top_p, digits: 2))
                LabeledContent("切句方式", value: item.text_split_method.map(displaySplit) ?? "—")
                if let steps = item.sample_steps { LabeledContent("采样步数", value: String(steps)) }
                if !item.aux_ref_ids.isEmpty { LabeledContent("辅助参考", value: "\(item.aux_ref_ids.count) 条") }
                LabeledContent("台词") { Text(item.text).textSelection(.enabled) }
            }
            .formStyle(.grouped)
        }
        .padding(20)
        .frame(width: 560, height: 520)
    }
}
