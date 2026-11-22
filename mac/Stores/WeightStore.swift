import Foundation
import Observation

@MainActor @Observable
final class WeightStore {
    private let api: APIClient
    private let defaults: UserDefaults
    var rows: [WeightEntry] = []
    var pairRows: [WeightEntry] = []
    var evalSet = "hard" { didSet { if evalSet != oldValue { rows = []; pairRows = [] } } }
    let planner = TaskPlanner()
    let versions = ModelVersionStore()
    var manualPair = false
    var evaluationItems = 36
    var evaluationSet = "hard"
    var total = 0
    var selectedID: String?
    var character = ""
    var engine = ""
    var source = ""
    var type = ""
    var showHidden = false
    var sort: String { didSet { defaults.set(sort, forKey: "weightsSort") } }
    var order: String { didSet { defaults.set(order, forKey: "weightsOrder") } }
    var pairCharacter: String { didSet { if pairCharacter != oldValue { gptID = ""; sovitsID = ""; versions.reset(); manualPair = false } } }
    var pairEngine: String { didSet { if pairEngine != oldValue { gptID = ""; sovitsID = "" } } }
    var gptID = ""
    var sovitsID = ""
    var busy = false
    var evaluationBusy = false
    var evaluationJobID: String?
    var evaluationTask: Task<Void, Never>?
    var notice: String?
    init(api: APIClient, defaults: UserDefaults = .standard, character: String, engine: String) {
        self.api = api
        self.defaults = defaults
        self.character = character
        self.engine = engine
        pairCharacter = character
        pairEngine = engine
        sort = defaults.string(forKey: "weightsSort") ?? "created_at"
        order = defaults.string(forKey: "weightsOrder") ?? "desc"
    }
    var selected: WeightEntry? { rows.first { $0.id == selectedID } }
    var queryKey: String { [character,engine,source,type,String(showHidden),sort,order,evalSet].joined(separator: "|") }
    var pairKey: String { api.baseURL + "|" + pairCharacter + "|" + pairEngine + "|" + evalSet + "|" + (versions.selectedID ?? "manual") }
    var pairGPT: WeightEntry? { pairRows.first {  $0.id == gptID && $0.type == "gpt" && $0.engine == pairEngine && ($0.character == nil || $0.character == pairCharacter) } }
    var pairSoVITS: WeightEntry? { pairRows.first {  $0.id == sovitsID && $0.type == "sovits" && $0.engine == pairEngine && ($0.character == nil || $0.character == pairCharacter) } }
    var pairReady: Bool { !pairCharacter.isEmpty && pairGPT != nil && pairSoVITS != nil }
    var comparisons: [ComboScore] {
        var values: [String: ComboScore] = [:]
        for entry in pairRows where entry.engine == pairEngine {
            for score in entry.evals where score.character == pairCharacter && score.eval_set == evalSet {
                if values[score.id] == nil || values[score.id]!.evaluated_at < score.evaluated_at { values[score.id] = score }
            }
        }
        return values.values.sorted { $0.score_median > $1.score_median }
    }
    func load() async throws {
        let key = queryKey
        var query = ["sort":sort,"order":order,"eval_set":evalSet]
        if !character.isEmpty { query["character"] = character }
        if !engine.isEmpty { query["engine"] = engine }
        if !source.isEmpty { query["source"] = source }
        if !type.isEmpty { query["type"] = type }
        if !showHidden { query["hidden"] = "false" }
        let result: WeightList = try await api.get("/weights", query: query)
        try Task.checkCancellation()
        guard key == queryKey else { return }
        rows = result.weights
        total = result.total
        if let selectedID, !rows.contains(where: { $0.id == selectedID }) { self.selectedID = nil }
    }
    func loadPair(studio: StudioStore) async throws {
        if versions.character != pairCharacter {
            try await versions.load(api: api, character: pairCharacter)
            studio.characterVersionsCache[pairCharacter] = versions.rows
            if let selected = versions.selected { pairEngine = selected.engine }
        }
        let key = pairKey
        let result = try await studio.visibleWeights(character: pairCharacter, engine: pairEngine, evalSet: evalSet)
        try Task.checkCancellation()
        guard key == pairKey else { return }
        pairRows = result
        let currentDefaults = studio.models.first { $0.slug == pairCharacter }?.engines[pairEngine]
        if let version = versions.selected, !manualPair {
            gptID = result.first { $0.type == "gpt" && $0.name == version.gpt }?.id ?? "version-gpt:" + version.id
            sovitsID = result.first { $0.type == "sovits" && $0.name == version.sovits }?.id ?? "version-sovits:" + version.id
            return
        }
        if !result.contains(where: {  $0.id == gptID && $0.type == "gpt" && $0.engine == pairEngine && ($0.character == nil || $0.character == pairCharacter) }) {
            gptID = result.first { $0.type == "gpt" && $0.name == currentDefaults?.default_gpt }?.id ?? result.first { $0.type == "gpt" }?.id ?? ""
        }
        if !result.contains(where: {  $0.id == sovitsID && $0.type == "sovits" && $0.engine == pairEngine && ($0.character == nil || $0.character == pairCharacter) }) {
            sovitsID = result.first { $0.type == "sovits" && $0.name == currentDefaults?.default_sovits }?.id ?? result.first { $0.type == "sovits" }?.id ?? ""
        }
    }
    func patch(_ entry: WeightEntry, patch: WeightPatch) async throws {
        busy = true
        defer { busy = false }
        let _: WeightEntry = try await api.send("/weights/\(entry.id)", method: "PATCH", body: patch)
        try await load()
        notice = "模型信息已保存"
    }
    func setDefaultPair(studio: StudioStore) async throws {
        guard let gpt = pairGPT, let sovits = pairSoVITS, pairReady, !studio.settingDefaultVersion else { return }
        let targetCharacter = pairCharacter, targetEngine = pairEngine
        let token = api.generation
        studio.settingDefaultVersion = true
        busy = true
        defer {
            busy = false
            studio.settingDefaultVersion = false
        }
        let _: WeightEntry = try await api.send("/weights/\(gpt.id)", method: "PATCH", body: WeightPatch(is_default: true, character: targetCharacter))
        do {
            guard api.isCurrent(token) else { return }
            let _: WeightEntry = try await api.send("/weights/\(sovits.id)", method: "PATCH", body: WeightPatch(is_default: true, character: targetCharacter))
        } catch {
            if api.isCurrent(token) { try await studio.reloadModels(resetSelection: false) }
            throw APIError(status: 0, message: "GPT 默认已更新，SoVITS 设置未完成：\(error.localizedDescription)")
        }
        guard api.isCurrent(token) else { return }
        try await studio.reloadModels(resetSelection: false)
        guard api.isCurrent(token) else { return }
        try await load()
        try await refreshVersions(studio: studio, preferRecommended: true)
        if studio.params.character == targetCharacter {
            try? await studio.loadComposerVersions(preferRecommended: true)
        }
        notice = "已设为 \(characterName(targetCharacter)) / \(targetEngine) 的默认组合"
    }
    func delete(_ entry: WeightEntry, studio: StudioStore) async throws {
        guard entry.source == "user", entry.deletable else { throw APIError(status: 0, message: "只能删除自己添加的模型") }
        busy = true
        defer { busy = false }
        let result: DeletedWeight = try await api.getDeletedWeight(entry.id)
        try await studio.reloadModels(resetSelection: false)
        try await load()
        try await loadPair(studio: studio)
        notice = "已删除 \(entry.label)"
        if !result.was_default_for.isEmpty {
            notice = "已删除原默认模型，\(result.was_default_for.map(characterName).joined(separator: "、"))的默认声音已更新"
        }
    }
    func evaluate(studio: StudioStore) async throws {
        guard let gptName = versions.selected?.gpt ?? pairGPT?.name,
              let sovitsName = versions.selected?.sovits ?? pairSoVITS?.name else { return }
        let submittedSet = evaluationSet
        evaluationBusy = true
        evaluationJobID = nil
        defer { evaluationBusy = false }
        let job: Job
        do {
            job = try await planner.submit(api: api, path: "/weights/eval", input: EvalRequest(version: versions.selected?.id, engine: versions.selected?.engine ?? pairEngine, gpt: gptName, sovits: sovitsName, character: pairCharacter, eval_set: submittedSet))
        } catch let failure as APIError where failure.code == "version_unavailable" {
            try await versions.invalidate(api: api, character: pairCharacter)
            throw APIError(status: 409, message: "这个模型版本已不可用，请重新选择。", code: failure.code)
        }
        studio.recordLocalJob(job.id)
        evaluationJobID = job.id
        _ = try await studio.follow(job)
        do {
            try await load()
            try await loadPair(studio: studio)
        } catch {
            NSLog("评测已完成，评分列表刷新失败：%@", error.localizedDescription)
            notice = "\(displayEvalSet(submittedSet)) 组合评测完成；列表暂未刷新，可稍后点“刷新”。"
            return
        }
        notice = submittedSet == evalSet ? "\(displayEvalSet(submittedSet)) 组合评测完成，分数已刷新" : "\(displayEvalSet(submittedSet)) 组合评测完成；当前显示 \(displayEvalSet(evalSet))，切换分数评测集查看本次结果"
    }
    func stopEvaluation(studio: StudioStore) {
        evaluationTask?.cancel()
        if let id = evaluationJobID, let job = studio.jobs.first(where: { $0.id == id && $0.active }) { studio.perform { try await studio.cancel(job) } }
    }
}

extension APIClient {
    func getDeletedWeight(_ id: String) async throws -> DeletedWeight {
        try JSONDecoder().decode(DeletedWeight.self, from: await data(request("/weights/\(id)", method: "DELETE")))
    }
}

extension WeightStore {
    func refreshVersions(studio: StudioStore, preferRecommended: Bool = false) async throws {
        guard !pairCharacter.isEmpty else { return }
        try await versions.load(api: api, character: pairCharacter, chooseDefault: !manualPair || preferRecommended, preferRecommended: preferRecommended)
        studio.characterVersionsCache[pairCharacter] = versions.rows
        if (!manualPair || preferRecommended), let version = versions.selected {
            manualPair = false
            pairEngine = version.engine
        }
        try await loadPair(studio: studio)
    }
    func evaluateVersion(_ version: ModelVersion, studio: StudioStore) async throws {
        selectVersion(version.id)
        try await evaluate(studio: studio)
    }
    func selectVersion(_ id: String?) {
        versions.selectedID = id; versions.message = nil; manualPair = false
        if let version = versions.selected { pairEngine = version.engine }
    }
    func useManualPair() { versions.selectedID = nil; versions.message = nil; manualPair = true }
}
