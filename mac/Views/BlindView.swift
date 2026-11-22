import SwiftUI

struct BlindView: View {
    @Environment(StudioStore.self) private var studio
    var embeddedCharacter: String? = nil
    var body: some View {
        BlindManagerView(manager: studio.blindManager, embeddedCharacter: embeddedCharacter)
    }
}

private struct BlindManagerView: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var manager: BlindStore
    var embeddedCharacter: String? = nil

    @State private var creating = false
    @State private var activePrefill: BlindPrefill?
    @State private var deleting: BlindSummary?
    @State private var revealing = false
    @State private var scopeCurrentCharacter = true

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    blindHeaderTitle
                    Spacer(minLength: 4)
                    blindHeaderButtons
                }
                VStack(alignment: .leading, spacing: 8) {
                    blindHeaderTitle
                    HStack {
                        Spacer(minLength: 0)
                        blindHeaderButtons
                    }
                }
            }
            .disabled(manager.busy)

            if let test = manager.test, test.status == "failed" {
                if manager.regenerationNeedsVersion {
                    Button("重选版本…") { activePrefill = nil; creating = true }.disabled(manager.busy)
                } else {
                    BlindRegenerationControls(manager: manager)
                }
            }

            if let result = manager.result {
                BlindResultsView(manager: manager, result: result)
            } else if let test = manager.test {
                HStack {
                    Text("\(characterName(test.character)) · \(displayEvalSet(test.eval_set)) · \(blindStatus(test.status, revealed: test.revealed))")
                    Spacer()
                    Button("提前揭晓…") { revealing = true }.disabled(manager.busy)
                }
                HStack {
                    ProgressView(value: Double(test.n_ready), total: Double(max(1, test.n_questions)))
                    Text("已生成 \(test.n_ready)/\(test.n_questions) · 已投 \(test.n_voted)/\(test.n_questions)")
                        .monospacedDigit()
                        .fixedSize()
                }
                if let question = manager.question {
                    BlindQuestionView(manager: manager, question: question, total: test.n_questions)
                }
                Spacer(minLength: 0)
            } else if manager.selectedID != nil {
                ProgressView("正在读取盲听…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                list
            }
        }
        .padding(.horizontal, embeddedCharacter == nil ? 24 : 0)
        .padding(.vertical, embeddedCharacter == nil ? 12 : 0)
        .task {
            syncEmbeddedFilter()
            consumePendingPrefill()
            do { try await manager.prepare() }
            catch { studio.errorMessage = error.localizedDescription }
        }
        .task(id: manager.selectedID) {
            guard manager.selectedID != nil else { return }
            await manager.poll(every: .seconds(2), refresh: manager.refresh) { studio.errorMessage = $0.localizedDescription }
        }
        .onChange(of: embeddedCharacter) { syncEmbeddedFilter() }
        .onChange(of: scopeCurrentCharacter) { syncEmbeddedFilter() }
        .onChange(of: studio.pendingBlindPrefill) { consumePendingPrefill() }
        .onChange(of: manager.questionID) { manager.stopAudio(studio: studio) }
        .onChange(of: manager.result != nil) { manager.stopAudio(studio: studio) }
        .onAppear {
            manager.stopAudio(studio: studio)
            syncEmbeddedFilter()
            consumePendingPrefill()
        }
        .onDisappear { manager.stopAudio(studio: studio) }
        .sheet(isPresented: $creating, onDismiss: { activePrefill = nil }) {
            BlindCreateView(manager: manager, prefill: activePrefill)
        }
        .confirmationDialog("提前揭晓会显示模型身份，之后仍可改票。", isPresented: $revealing) {
            Button("揭晓结果") { studio.perform { try await manager.reveal() } }
        }
        .confirmationDialog("删除这次盲听？生成中的任务会一起取消。", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            if let row = deleting {
                Button("删除盲听", role: .destructive) { studio.perform { try await manager.delete(row.id); deleting = nil } }
            }
        }
    }

    @ViewBuilder
    private var blindHeaderTitle: some View {
        if embeddedCharacter == nil {
            PageHeading(title: "盲听", subtitle: "只凭声音选择；全部投完后揭晓。")
        } else {
            Text("只凭声音选择；全部投完后揭晓。").font(.callout).foregroundStyle(.secondary)
        }
    }

    private var blindHeaderButtons: some View {
        HStack(spacing: 8) {
            if manager.selectedID != nil {
                Button("返回列表") { manager.stopAudio(studio: studio); manager.select(nil) }
            }
            Button("新建盲听…", systemImage: "plus") {
                activePrefill = nil
                creating = true
            }
            .buttonStyle(.borderedProminent)
        }.fixedSize()
    }

    private func syncEmbeddedFilter() {
        guard let embeddedCharacter else { return }
        let target = scopeCurrentCharacter ? embeddedCharacter : ""
        if manager.character != target {
            manager.character = target
            manager.offset = 0
        }
    }

    private func consumePendingPrefill() {
        guard let prefill = studio.pendingBlindPrefill else { return }
        studio.pendingBlindPrefill = nil
        activePrefill = prefill
        creating = true
    }

    private var list: some View {
        VStack(alignment: .leading) {
            HStack {
                if embeddedCharacter != nil {
                    Picker("角色范围", selection: $scopeCurrentCharacter) {
                        Text("当前角色").tag(true)
                        Text("全部角色").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .frame(width: 170)
                } else {
                    Picker("角色", selection: $manager.character) {
                        Text("全部").tag("")
                        ForEach(studio.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
                    }.frame(width: 200)
                }
                Spacer()
                Button("刷新") { studio.perform { try await manager.loadList() } }
            }
            ScrollView {
                LazyVStack(spacing: 12) {
                    ForEach(manager.rows) { row in
                        HStack(alignment: .top, spacing: 18) {
                            Button {
                                manager.select(row.id)
                            } label: {
                                VStack(alignment: .leading, spacing: 7) {
                                    Text(blindTitle(row.title, character: row.character, evalSet: row.eval_set, created: row.created)).font(.headline)
                                    Text("\(characterName(row.character)) · \(displayEvalSet(row.eval_set)) · \(row.created)").font(.caption).foregroundStyle(.secondary)
                                    Text("\(blindStatus(row.status, revealed: row.revealed)) · 已投 \(row.n_voted)/\(row.n_questions) · 已生成 \(row.n_ready)/\(row.n_questions)")
                                    if row.revealed, let result = manager.revealedResults[row.id] {
                                        Text("A 胜 \(result.a.wins) 负 \(result.a.losses) 平 \(result.a.ties)").font(.callout.bold())
                                        Text("A：\(manager.label(result.a.combo))\nB：\(manager.label(result.b.combo))").font(.caption).textSelection(.enabled)
                                    }
                                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                            Button("删除…", role: .destructive) { deleting = row }
                        }.padding(16).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
                    }
                    if manager.rows.isEmpty {
                        ContentUnavailableView("还没有盲听", systemImage: "headphones", description: Text("新建一次盲听，或在模型版本页勾选 2 个版本发起盲听 PK。"))
                    }
                }
            }
            HStack {
                Text("共 \(manager.total) 次").foregroundStyle(.secondary)
                Spacer()
                Button("上一页") { manager.offset -= manager.limit }.disabled(manager.offset == 0)
                Button("下一页") { manager.offset += manager.limit }.disabled(manager.offset + manager.limit >= manager.total)
            }
        }
        .disabled(manager.busy)
        .onChange(of: manager.character) { manager.offset = 0 }
        .task(id: manager.listKey) {
            await manager.poll(every: .seconds(3), refresh: manager.loadList) { studio.errorMessage = $0.localizedDescription }
        }
    }
}

private struct BlindQuestionView: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var manager: BlindStore
    let question: BlindQuestion
    let total: Int
    @State private var note = ""
    @FocusState private var noteFocused: Bool
    @FocusState private var keyboardFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack {
                Text("第 \(question.index + 1) / \(total) 题").font(.title2.bold())
                Spacer()
                Button("上一题 [") { manager.move(-1) }.disabled(question.index == 0)
                Button("下一题 ]") { manager.move(1) }.disabled(question.index + 1 == total)
            }
            Text(question.text).font(.title2).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            Text(question.tests.joined(separator: " · ")).font(.callout).foregroundStyle(.secondary)
            if !question.ready { Label("生成中", systemImage: "waveform").foregroundStyle(.secondary) }
            HStack(spacing: 20) {
                playButton("播放左  ← / 1", side: "left", path: question.left_url)
                playButton("播放右  → / 2", side: "right", path: question.right_url)
            }
            Text("当前选择：\(blindVoteLabel(question.vote))").font(.headline).foregroundStyle(question.vote == nil ? Color.secondary : Color.accentColor)
            TextField("可选备注（最多 200 字）", text: $note, axis: .vertical)
                .textFieldStyle(.roundedBorder).lineLimit(2...3).focused($noteFocused)
                .onChange(of: note) { if note.unicodeScalars.count > 200 { note = String(note.unicodeScalars.prefix(200)) } }
            HStack(spacing: 16) {
                voteButton("左更好  A", choice: "left")
                voteButton("差不多  S", choice: "tie")
                voteButton("右更好  D", choice: "right")
            }.disabled(!question.ready)
            Text("← / 1 播左 · → / 2 播右 · A / S / D 投票 · [ / ] 切题\n投票后跳到下一道未投题，不自动播放。备注框中正常输入；按 Esc 回到快捷键。").font(.caption).foregroundStyle(.secondary)
        }
        .padding(24).background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 16))
        .disabled(manager.busy)
        .focusable().focused($keyboardFocused)
        .onKeyPress { press in
            guard !noteFocused, !manager.busy, press.modifiers.intersection([.command, .control, .option]).isEmpty else { return .ignored }
            switch press.key {
            case "[": manager.move(-1)
            case "]": manager.move(1)
            case .leftArrow, "1": play(side: "left", path: question.left_url)
            case .rightArrow, "2": play(side: "right", path: question.right_url)
            case "a", "A": vote("left")
            case "s", "S": vote("tie")
            case "d", "D": vote("right")
            default: return .ignored
            }
            return .handled
        }
        .onExitCommand { noteFocused = false; keyboardFocused = true }
        .onAppear { note = question.note; keyboardFocused = true }
        .onChange(of: question.id) { note = question.note; noteFocused = false; keyboardFocused = true }
    }

    private func playButton(_ title: String, side: String, path: String?) -> some View {
        Button { play(side: side, path: path) } label: {
            Label(title, systemImage: "play.fill").font(.title3).frame(maxWidth: .infinity, minHeight: 64)
        }.disabled(!question.ready).buttonStyle(.bordered)
    }

    private func voteButton(_ title: String, choice: String) -> some View {
        Button(title) { vote(choice) }.buttonStyle(.borderedProminent).tint(question.vote == choice ? .green : .accentColor).frame(maxWidth: .infinity)
    }

    private func play(side: String, path: String?) {
        guard question.ready, let path else { return }
        keyboardFocused = true
        manager.play(path, identity: "blind-\(question.id)-\(side)", studio: studio)
    }

    private func vote(_ choice: String) {
        guard question.ready else { return }
        keyboardFocused = true
        studio.perform { try await manager.vote(question: question.id, choice: choice, note: note) }
    }
}

private struct BlindResultsView: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var manager: BlindStore
    let result: BlindResult
    @State private var settingDefaultLabel: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let test = manager.test, test.n_ready < test.n_questions {
                    HStack {
                        Text("\(blindStatus(test.status, revealed: false)) · 已生成 \(test.n_ready)/\(test.n_questions)")
                    }
                }
                Text("结果 · \(displayEvalSet(result.eval_set)) · 已投 \(result.n_voted)/\(result.n_questions)").font(.title2.bold())
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 14) {
                        summary(result.a, other: result.b)
                        summary(result.b, other: result.a)
                    }
                    VStack(alignment: .leading, spacing: 12) {
                        summary(result.a, other: result.b)
                        summary(result.b, other: result.a)
                    }
                }
                ForEach(result.questions) { question in
                    BlindResultRow(manager: manager, question: question)
                }
            }
        }.disabled(manager.busy)
    }

    private func summary(_ combo: BlindComboResult, other: BlindComboResult) -> some View {
        let isWinner = (combo.win_rate ?? 0) > (other.win_rate ?? 0)
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(combo.label).font(.largeTitle.bold())
                if isWinner {
                    Label("胜出", systemImage: "crown.fill")
                        .font(.caption.bold())
                        .foregroundStyle(.orange)
                }
                Spacer()
            }
            Text(manager.label(combo.combo)).font(.headline).textSelection(.enabled)
            Text("胜 \(combo.wins) · 负 \(combo.losses) · 平 \(combo.ties)")
            Text("胜率 \(combo.win_rate.map { String(format: "%.1f%%", $0 * 100) } ?? "—")").font(.title2.bold())
            Text("自动分均值 \(number(combo.auto_score_mean, digits: 2))").foregroundStyle(.secondary).help(scoreHelp(combo.auto_score_mean))
            HStack {
                if isWinner {
                    Button(settingDefaultLabel == combo.label ? "设置中…" : "设为默认声音", systemImage: "star.fill") {
                        applyDefault(combo)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(settingDefaultLabel != nil || studio.settingDefaultVersion)
                } else {
                    Button(settingDefaultLabel == combo.label ? "设置中…" : "设为默认声音", systemImage: "star") {
                        applyDefault(combo)
                    }
                    .buttonStyle(.bordered)
                    .disabled(settingDefaultLabel != nil || studio.settingDefaultVersion)
                }
            }
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(20)
        .background(isWinner ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.07), in: RoundedRectangle(cornerRadius: 14))
    }

    private func applyDefault(_ combo: BlindComboResult) {
        settingDefaultLabel = combo.label
        studio.perform {
            defer { settingDefaultLabel = nil }
            try await studio.setDefaultVersion(
                character: result.character,
                engine: combo.combo.engine,
                gpt: combo.combo.gpt,
                sovits: combo.combo.sovits
            )
        }
    }
}

private struct BlindResultRow: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var manager: BlindStore
    let question: BlindRevealQuestion
    @State private var note = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("\(question.index + 1). \(question.text)").font(.headline).textSelection(.enabled)
            Text(question.tests.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
            Text("你的选择：\(blindVoteLabel(question.vote)) · 赢家：\(question.winner == "tie" ? "平" : question.winner ?? "—") · 左边是 \(question.left ?? "—")")
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    playPairButtons
                    Spacer(minLength: 4)
                    revoteButtons
                }
                VStack(alignment: .leading, spacing: 8) {
                    playPairButtons
                    revoteButtons
                }
            }
            TextField("备注", text: $note).textFieldStyle(.roundedBorder)
                .onChange(of: note) { if note.unicodeScalars.count > 200 { note = String(note.unicodeScalars.prefix(200)) } }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
        .onAppear { note = question.note }
    }

    private var playPairButtons: some View {
        HStack(spacing: 8) {
            Button("播放 A · \(number(question.a_score, digits: 2)) 分") {
                if let path = question.a_audio_url { manager.play(path, identity: "\(question.id)-A", studio: studio) }
            }.disabled(question.a_audio_url == nil).help(scoreHelp(question.a_score))
            Button("播放 B · \(number(question.b_score, digits: 2)) 分") {
                if let path = question.b_audio_url { manager.play(path, identity: "\(question.id)-B", studio: studio) }
            }.disabled(question.b_audio_url == nil).help(scoreHelp(question.b_score))
        }.fixedSize()
    }

    private var revoteButtons: some View {
        HStack(spacing: 6) {
            Text("改票：").foregroundStyle(.secondary)
            Button("左更好") { vote("left") }
            Button("差不多") { vote("tie") }
            Button("右更好") { vote("right") }
        }.disabled(question.left == nil).fixedSize()
    }

    private func vote(_ choice: String) {
        studio.perform { try await manager.vote(question: question.id, choice: choice, note: note) }
    }
}

struct BlindRegenerationControls: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var manager: BlindStore
    var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ComputePicker(planner: manager.regenerationPlanner, input: BlindGenerateInput(), action: "补生成", contextKey: manager.selectedID ?? "", taskTitle: "还有 \(max(0, (manager.test?.n_questions ?? 0) - (manager.test?.n_ready ?? 0))) 题", preview: false, expanded: expanded)
            HStack {
                Spacer()
                Button("补生成") { studio.perform { try await manager.regenerate() } }
                    .disabled(manager.busy || manager.regenerationNeedsVersion)
            }
        }
    }
}
