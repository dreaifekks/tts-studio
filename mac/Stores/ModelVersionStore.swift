import Foundation
import Observation

struct ModelVersion: Codable, Sendable, Identifiable {
    var id: String
    var label: String
    var engine: String
    var gpt: String
    var sovits: String
    var score: Double?
    var score_set: String?
    var recommended: Bool
    var source_run: String?
    var created: String
    var optionLabel: String { "\(label) · \(displayEngine(engine))" + (recommended ? " · 推荐" : "") }
    var scoreLabel: String? {
        guard let score, let score_set else { return nil }
        return "\(displayEvalSet(score_set)) · 评分 \(displayScore(score))"
    }
    var zeroShot: Bool { label == "底模（零样本）" }
}
@MainActor @Observable
final class ModelVersionStore {
    private(set) var character = ""
    var rows: [ModelVersion] = []
    var selectedID: String?
    var loading = false
    var message: String?
    private var revision = 0
    var selected: ModelVersion? { rows.first { $0.id == selectedID } }
    func reset() { revision += 1; character = ""; rows = []; selectedID = nil; message = nil; loading = false }
    func load(api: APIClient, character: String, chooseDefault: Bool = true, preferRecommended: Bool = false) async throws {
        if self.character != character { reset(); self.character = character }
        revision += 1
        let token = revision
        loading = true
        defer { if token == revision { loading = false } }
        let values: [ModelVersion] = try await api.get("/characters/\(character)/versions")
        try Task.checkCancellation()
        guard revision == token else { return }
        rows = values
        if preferRecommended {
            selectedID = (values.first { $0.recommended } ?? values.first)?.id
        } else if !values.contains(where: { $0.id == selectedID }) {
            selectedID = chooseDefault ? (values.first { $0.recommended } ?? values.first)?.id : nil
        }
    }
    func invalidate(api: APIClient, character: String) async throws {
        selectedID = nil
        try await load(api: api, character: character, chooseDefault: false)
        message = "这个模型版本已不可用，请重新选择。"
    }
}
