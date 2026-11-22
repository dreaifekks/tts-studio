import Foundation
import Observation

@MainActor @Observable
final class BlindStore {
    let api: APIClient
    var rows: [BlindSummary] = []
    var total = 0
    var character = ""
    var offset = 0
    let limit = 30
    var selectedID: String?
    var test: BlindTest?
    var result: BlindResult?
    var revealedResults: [String: BlindResult] = [:]
    var questionID: String?
    var evalSets: [EvalSet] = []
    var weights: [WeightEntry] = []
    let planner = TaskPlanner()
    let regenerationPlanner = TaskPlanner()
    var busy = false
    var regenerationNeedsVersion = false
    private var revision = 0
    private var listRevision = 0
    private var playback: Task<Void, Never>?

    private let onJobCreated: (String) -> Void
    init(api: APIClient, onJobCreated: @escaping (String) -> Void = { _ in }) {
        self.api = api
        self.onJobCreated = onJobCreated
    }
    var question: BlindQuestion? { test?.questions.first { $0.id == questionID } }
    var listKey: String { character + "|" + String(offset) }
    func prepare() async throws {
        async let sets: EvalSetList = api.get("/eval-sets")
        async let catalog: WeightList = api.get("/weights", query: ["hidden":"false", "eval_set":"hard"])
        (evalSets, weights) = try await (sets.sets, catalog.weights)
    }
    func poll(every interval: Duration, refresh: () async throws -> Void, report: (Error) -> Void) async {
        while !Task.isCancelled {
            do { try await refresh() }
            catch is CancellationError { return }
            catch {
                if Task.isCancelled { return }
                report(error)
            }
            do { try await Task.sleep(for: interval) }
            catch { return }
        }
    }
    func loadList() async throws {
        listRevision += 1
        let token = listRevision
        var query = ["limit":String(limit), "offset":String(offset)]
        if !character.isEmpty { query["character"] = character }
        let value: BlindList = try await api.get("/blind", query: query)
        try Task.checkCancellation()
        guard token == listRevision else { return }
        rows = value.tests
        total = value.total
        for row in value.tests where row.revealed {
            let value: BlindResult = try await api.get("/blind/\(row.id)/result")
            try Task.checkCancellation()
            guard token == listRevision else { return }
            revealedResults[row.id] = value
        }
    }
    func select(_ id: String?) {
        revision += 1
        selectedID = id
        regenerationNeedsVersion = false
        regenerationPlanner.reset()
        test = nil
        result = nil
        questionID = nil
    }
    func refresh() async throws {
        guard !busy, let id = selectedID else { return }
        try await reload(id: id, token: revision)
    }
    private func reload(id: String, token: Int) async throws {
        let value: BlindTest = try await api.get("/blind/\(id)")
        try Task.checkCancellation()
        guard selectedID == id, token == revision else { return }
        test = value
        if questionID == nil { questionID = value.questions.first(where: { $0.vote == nil })?.id ?? value.questions.first?.id }
        if value.revealed {
            let revealed: BlindResult = try await api.get("/blind/\(id)/result")
            try Task.checkCancellation()
            guard selectedID == id, token == revision else { return }
            result = revealed
            revealedResults[id] = revealed
        }
    }
    func create(_ input: BlindCreate) async throws {
        busy = true
        defer { busy = false }
        let value: BlindTest = try await planner.submit(api: api, path: "/blind", input: input)
        onJobCreated(value.job_id)
        select(value.id)
        test = value
        questionID = value.questions.first?.id
    }
    func vote(question id: String, choice: String, note: String) async throws {
        guard !busy, let testID = selectedID else { return }
        busy = true
        revision += 1
        let token = revision
        defer { busy = false }
        let _: BlindQuestion = try await api.send("/blind/\(testID)/questions/\(id)/vote", method: "PUT", body: BlindVote(choice: choice, note: note))
        try await reload(id: testID, token: token)
        if result == nil, let questions = test?.questions, let index = questions.firstIndex(where: { $0.id == id }) {
            questionID = Self.nextUnvoted(in: questions, after: index)?.id ?? id
        }
    }
    static func nextUnvoted(in questions: [BlindQuestion], after index: Int) -> BlindQuestion? {
        (Array(questions.dropFirst(index + 1)) + Array(questions.prefix(index + 1))).first { $0.vote == nil }
    }
    func move(_ delta: Int) {
        guard !busy, let questions = test?.questions, let index = questions.firstIndex(where: { $0.id == questionID }) else { return }
        let next = index + delta
        if questions.indices.contains(next) { questionID = questions[next].id }
    }
    func reveal() async throws {
        guard !busy, let id = selectedID else { return }
        busy = true
        revision += 1
        defer { busy = false }
        let value: BlindResult = try await api.post("/blind/\(id)/reveal")
        result = value
        revealedResults[id] = value
        try await reload(id: id, token: revision)
    }
    func regenerate() async throws {
        guard !busy, !regenerationNeedsVersion, let id = selectedID else { return }
        busy = true
        revision += 1
        defer { busy = false }
        let value: BlindTest
        regenerationPlanner.previewKind = "gen"
        regenerationPlanner.previewEngine = nil
        regenerationPlanner.taskSummary = "补生成（\(test.map { characterName($0.character) } ?? "盲听") · 还有 \(max(0, (test?.n_questions ?? 0) - (test?.n_ready ?? 0))) 题）"
        do { value = try await regenerationPlanner.submit(api: api, path: "/blind/\(id)/generate", input: BlindGenerateInput()) }
        catch let failure as APIError where failure.code == "version_unavailable" {
            regenerationNeedsVersion = true
            throw APIError(status: 409, message: "这个模型版本已不可用，请重新选择版本后新建盲听。", code: failure.code)
        }
        onJobCreated(value.job_id)
        test = value
    }
    func delete(_ id: String) async throws {
        busy = true
        defer { busy = false }
        _ = try await api.data(api.request("/blind/\(id)", method: "DELETE"))
        if selectedID == id { select(nil) }
        revealedResults.removeValue(forKey: id)
        try await loadList()
    }
    func stopAudio(studio: StudioStore) {
        playback?.cancel()
        playback = nil
        studio.audio.stop()
    }
    func play(_ path: String, identity: String, studio: StudioStore) {
        stopAudio(studio: studio)
        playback = studio.perform {
            let data = try await self.api.data(self.api.request(path, query: ["format":"mp3"]))
            try Task.checkCancellation()
            try studio.audio.play(id: identity, data: data)
        }
    }
    func label(_ combo: BlindCombo) -> String {
        "\(combo.engine) · \(weightName(combo.gpt, engine: combo.engine)) + \(weightName(combo.sovits, engine: combo.engine))"
    }
    func weightName(_ name: String, engine: String) -> String {
        weights.first { $0.engine == engine && $0.name == name }?.label ?? name
    }
}

func blindTitle(_ title: String, character: String, evalSet: String, created: String) -> String {
    let legacyAuto = title.hasPrefix(character + " ") && title.range(of: #"^[^ ]+ [^:]+:.+\+.+ vs [^:]+:.+\+.+$"#, options: .regularExpression) != nil
    let prefix = character + " · " + evalSet + " · "
    let coreAuto = title.hasPrefix(prefix) && String(title.dropFirst(prefix.count)).range(of: #"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"#, options: .regularExpression) != nil
    return title.isEmpty || legacyAuto || coreAuto ? "\(characterName(character)) · \(displayEvalSet(evalSet)) · \(created.prefix(10))" : title
}
func blindStatus(_ status: String, revealed: Bool) -> String {
    if revealed { return "已揭晓" }
    return ["generating":"生成中", "ready":"可以做", "failed":"生成失败"][status] ?? status
}
func blindVoteLabel(_ vote: String?) -> String {
    ["left":"左更好", "right":"右更好", "tie":"差不多"][vote ?? ""] ?? "未投票"
}
