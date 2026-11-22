import Foundation

struct SortOption: Identifiable {
    let id: String
    let label: String
    init(_ id: String, _ label: String) { self.id = id; self.label = label }
}
enum ListSorting {
    static let audio = [SortOption("created", "生成时间"), .init("score", "总分"), .init("duration", "时长"), .init("character", "角色"), .init("engine", "引擎"), .init("rating", "评分"), .init("reviewed_at", "评价时间")]
    static let references = [SortOption("id", "索引"), .init("duration", "时长"), .init("text", "文本"), .init("character", "角色")]
    static let works = [SortOption("created", "创建时间"), .init("updated", "更新时间"), .init("title", "名称"), .init("n_lines", "行数")]
    static let training = [SortOption("created", "创建时间"), .init("started", "开始时间"), .init("finished", "结束时间"), .init("kind", "任务类型"), .init("status", "状态"), .init("progress", "进度")]
    static let weights = [SortOption("created_at", "创建时间"), .init("score", "评测分"), .init("epoch", "训练轮数"), .init("engine", "引擎"), .init("character", "角色"), .init("size", "大小"), .init("name", "显示名"), .init("type", "类型"), .init("source", "来源")]
}
extension EngineInfo {
    var label: String { ([displayEngine(id)] + (zero_shot ? ["零样本"] : []) + (trainable ? ["可训练"] : [])).joined(separator: " · ") }
}
extension WeightEntry {
    var label: String { display_name.flatMap { $0.isEmpty ? nil : $0 } ?? name }
    var characterLabel: String { character.map(characterName) ?? "底模（零样本）" }
    var epochValue: Int { epoch ?? -1 }
    var scoreValue: Double { best_eval?.score_median ?? -.infinity }
    var sourceLabel: String { displaySource(source) }
    var typeLabel: String { type == "gpt" ? "GPT" : "SoVITS" }
    var sizeLabel: String { ByteCountFormatter.string(fromByteCount: Int64(size_bytes), countStyle: .file) }
}
extension ComboScore: Identifiable { var id: String { gpt + "|" + sovits } }
