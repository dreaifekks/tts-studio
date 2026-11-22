import Foundation

enum StudioPage: String, CaseIterable, Identifiable {
    case generate = "合成"
    case works = "作品"
    case characters = "角色"
    case training = "训练"
    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .generate: "waveform.badge.plus"
        case .works: "text.book.closed"
        case .characters: "person.2"
        case .training: "cpu"
        }
    }
}

enum CharacterTab: String, CaseIterable, Identifiable {
    case versions = "模型版本"
    case references = "参考音频"
    case blind = "盲听"
    case stats = "评价统计"
    var id: String { rawValue }
}

enum GenerateMode: String, CaseIterable, Identifiable {
    case candidates = "出候选"
    case compare = "对比引擎"
    var id: String { rawValue }
}

enum GenerateRightTab: String, CaseIterable, Identifiable {
    case candidates = "本次候选"
    case history = "历史记录"
    var id: String { rawValue }
}

enum ExportFormat: String, CaseIterable, Identifiable {
    case wav = "wav"
    case flac = "flac"
    case mp3 = "mp3"
    var id: String { rawValue }
    var label: String { rawValue.uppercased() }
}

enum SynthParamsState: Equatable {
    case `default`
    case saved
    case modified

    var label: String {
        switch self {
        case .default: "默认"
        case .saved: "已保存"
        case .modified: "未保存修改"
        }
    }
}

struct BlindPrefill: Equatable, Identifiable {
    let id = UUID()
    let character: String
    var versionA: String? = nil
    var versionB: String? = nil
    var comboA: BlindSide? = nil
    var comboB: BlindSide? = nil
}

extension EngineInfo {
    var supportsSampleSteps: Bool { ["v3", "v4", "v5turbo", "v5dev"].contains(id) }
    var isLora: Bool { train_defaults?.lora ?? ["v3", "v4", "v5turbo", "v5dev"].contains(id) }
}

func isPrimaryReferenceEligible(_ ref: Ref) -> Bool {
    ref.duration >= 3.0 && ref.duration <= 10.0
}

func primaryReferenceIneligibleReason(_ ref: Ref) -> String? {
    if ref.duration < 3.0 { return "短于 3 秒，不能作为主参考" }
    if ref.duration > 10.0 { return "超过 10 秒，不能作为主参考" }
    return nil
}

func eligibleEmotionTags(in refs: [Ref], for character: String) -> [String] {
    var seen: Set<String> = []
    var result: [String] = []
    for ref in refs where ref.character == character && isPrimaryReferenceEligible(ref) {
        for tag in ref.tags where !tag.isEmpty && seen.insert(tag).inserted {
            result.append(tag)
        }
    }
    return result
}

func hasEligiblePrimaryRef(for emotion: String, in refs: [Ref], character: String) -> Bool {
    guard !emotion.isEmpty else { return false }
    return refs.contains {
        $0.character == character && isPrimaryReferenceEligible($0) && $0.tags.contains(emotion)
    }
}

func normalizedAuxRefIDs(_ ids: [String], primaryRefID: String?, engineSupportsAuxRefs: Bool = true) -> [String] {
    guard engineSupportsAuxRefs else { return [] }
    var seen: Set<String> = primaryRefID.map { [$0] } ?? []
    var result: [String] = []
    for id in ids where !id.isEmpty && seen.insert(id).inserted {
        result.append(id)
    }
    return result
}

func applyTrainDefaults(for engine: EngineInfo, to request: inout TrainRequest) {
    request.engine = engine.id
    if let defaults = engine.train_defaults {
        request.sovits_epochs = defaults.sovits_epochs
        request.sovits_save_every = defaults.sovits_save_every
        request.sovits_batch = defaults.sovits_batch
        request.gpt_epochs = defaults.gpt_epochs
        request.gpt_save_every = defaults.gpt_save_every
        request.gpt_batch = defaults.gpt_batch
        request.lora_rank = defaults.lora ? (defaults.lora_rank ?? defaults.lora_ranks.first ?? 32) : nil
    } else {
        request.lora_rank = engine.isLora ? (request.lora_rank ?? 32) : nil
    }
}

func trainingBlockReason(
    request: TrainRequest,
    submitting: Bool,
    previewLoading: Bool,
    preview: DatasetPreview?,
    engines: [EngineInfo],
    targetMode: String = "new",
    existingSlugs: Set<String> = []
) -> String? {
    if submitting { return "正在提交训练任务…" }
    if request.dataset_id.isEmpty { return "请先选择数据集" }
    if request.min_sec > request.max_sec { return "最短时长不能大于最长时长" }
    if previewLoading { return "正在计算数据集过滤预览…" }
    if preview == nil { return "数据集过滤预览未就绪，请检查数据集或过滤条件" }
    let trimmed = request.slug.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { return "请选择已有角色或输入新角色 slug" }
    if trimmed.range(of: #"^[a-z0-9][a-z0-9-]{1,40}$"#, options: .regularExpression) == nil {
        return "角色 slug 须为 2–41 位小写英文、数字或连字符，且以字母或数字开头"
    }
    if targetMode == "new" && existingSlugs.contains(trimmed) {
        return "该角色已存在，请换一个 slug 或切到「已有角色」追加训练"
    }
    if !engines.contains(where: { $0.id == request.engine && $0.trainable }) {
        return "请选择支持训练的引擎"
    }
    if request.sovits_epochs < 1 || request.sovits_save_every < 1 || request.gpt_epochs < 1 || request.gpt_save_every < 1 {
        return "训练轮数和保存间隔必须大于 0"
    }
    if request.sovits_epochs < request.sovits_save_every || request.gpt_epochs < request.gpt_save_every {
        return "训练轮数不能小于保存间隔"
    }
    if request.sovits_batch < 1 || request.gpt_batch < 1 {
        return "Batch 大小必须大于 0"
    }
    if let maxLines = request.max_lines, maxLines < 10 {
        return "最多句数不能少于 10 句"
    }
    return nil
}

func sweepBlockReason(
    request: SweepRequest,
    submitting: Bool,
    allCompleted: Bool,
    allowedEngines: Set<String>? = nil
) -> String? {
    if submitting { return "正在提交扫参任务…" }
    if allCompleted { return "当前配置下的所有扫参组均已完成" }
    if request.characters.isEmpty { return "请先选择至少一个角色" }
    if request.engines.isEmpty { return "请先选择至少一个训练引擎" }
    if let allowedEngines {
        let invalid = request.engines.filter { !allowedEngines.contains($0) }
        if !invalid.isEmpty {
            let names = invalid.map(displayEngine).joined(separator: "、")
            return "已勾选不支持扫参的引擎（\(names)），请先取消勾选"
        }
    }
    if request.trains.isEmpty { return "请至少保留一组训练参数" }
    if request.min_sec > request.max_sec { return "最短时长不能大于最长时长" }
    if let maxLines = request.max_lines, maxLines < 10 { return "每组最多句数不能少于 10 句" }
    if request.keep < 1 { return "保留组合数必须至少为 1" }
    if request.samples < 0 { return "生成样本的组合数不能小于 0" }
    if request.samples > request.keep { return "生成样本的组合数不能大于保留组合数" }
    if request.trains.contains(where: { $0.sovits_epochs < 1 || $0.sovits_save_every < 1 || $0.gpt_epochs < 1 || $0.gpt_save_every < 1 }) {
        return "每组训练轮数和保存间隔必须大于 0"
    }
    if request.trains.contains(where: { $0.sovits_batch < 1 || $0.gpt_batch < 1 }) {
        return "每批大小（Batch）必须大于 0"
    }
    if request.trains.contains(where: { $0.sovits_epochs < $0.sovits_save_every || $0.gpt_epochs < $0.gpt_save_every }) {
        return "每组训练轮数都不能小于保存间隔"
    }
    return nil
}

extension Job {
    var kindLabel: String { ["blind":"盲听生成", "eval":"组合评测", "train":"训练", "sweep":"扫参", "bench":"测速", "candidates":"生成候选", "compare":"声音对比", "work_generate":"作品生成", "work_render":"作品渲染"][kind] ?? kind }
    var active: Bool { status == "queued" || status == "running" }
    var statusLabel: String {
        ["queued":"排队中", "running":"运行中", "succeeded":"已完成", "failed":"失败", "cancelled":"已取消", "interrupted":"已中断"][status] ?? status
    }
}
extension WorkLine {
    func matches(_ audio: AudioItem, engineSupportsAuxRefs: Bool = true) -> Bool {
        let expectedAux = normalizedAuxRefIDs(aux_ref_ids, primaryRefID: audio.ref_id, engineSupportsAuxRefs: engineSupportsAuxRefs)
        let actualAux = normalizedAuxRefIDs(audio.aux_ref_ids, primaryRefID: audio.ref_id, engineSupportsAuxRefs: true)
        return audio.text == text && audio.character == character && audio.engine == engine
            && audio.text_lang == lang && (ref_id == nil || audio.ref_id == ref_id)
            && abs((audio.fragment_interval ?? 0.3) - fragment_interval) < 0.001
            && actualAux == expectedAux
    }
    var synthesisInput: WorkLineIn {
        var value = input
        value.id = nil
        value.pause_after = 0
        return value
    }
    var input: WorkLineIn {
        WorkLineIn(engine: engine, ref_id: ref_id, emotion: emotion, gpt: gpt, sovits: sovits,
                   top_k: top_k, top_p: top_p, temperature: temperature, repetition_penalty: repetition_penalty,
                   seed: seed, speed: speed, fragment_interval: fragment_interval,
                   text_split_method: text_split_method, sample_steps: sample_steps,
                   aux_ref_ids: aux_ref_ids, version: version,
                   id: id.hasPrefix("draft-") ? nil : id, character: character, text: text, lang: lang, pause_after: pause_after)
    }
}
extension StatsGroup: Identifiable { var id: String { key } }
extension ComboEval: Identifiable { var id: String { gpt + "|" + sovits } }
let problemTags = ["吞字", "多字", "电音", "声调怪", "情绪不对", "节奏怪"]
func characterName(_ slug: String) -> String {
    slug
}
func number(_ value: Double?, digits: Int = 2) -> String {
    value.map { String(format: "%.*f", digits, $0) } ?? "—"
}

struct ReferenceAudition: Equatable {
    let token = UUID()
    let referenceID: String
    let restart: Bool
}

func scoreSummary(_ median: Double?, mean: Double?, worst: Double?, digits: Int = 2) -> String {
    "\(number(median, digits: digits)) · 均 \(number(mean, digits: digits)) · 最差 \(number(worst, digits: digits))"
}

func displayAccuracy(_ error: Double?) -> String { error.map { "\(Int((max(0, 1 - $0) * 100).rounded()))%" } ?? "—" }
func displaySimilarity(_ value: Double?) -> String { value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—" }
func displayScore(_ value: Double?) -> String { number(value, digits: 0) }
func displaySource(_ source: String) -> String { ["user":"自己训练", "july":"随附模型", "pretrained":"底模"][source] ?? "其他模型" }
func displaySplit(_ split: String) -> String { ["cut0":"整段生成", "cut1":"每四句一段", "cut2":"约五十字一段", "cut3":"按中文句号", "cut4":"按英文句号", "cut5":"按标点分句"][split] ?? "按标点分句" }
func displaySeconds(_ value: Double?) -> String { value.map { $0.formatted(.number.precision(.fractionLength(0...1))) } ?? "—" }
func displayHours(_ value: Double) -> String { let step = value >= 100 ? 10.0 : 1.0; return ((value / step).rounded() * step).formatted(.number.precision(.fractionLength(0))) }
extension WeightEntry {
    var modelLabel: String {
        display_name ?? "\(displayEngine(engine)) · \(displayDateTime(created_at, compact: true)) · \(typeLabel)" + (epoch.map { " 第 \($0) 轮" } ?? "")
    }
}
func scoreHelp(_ value: Double?) -> String { "评分：\(number(value, digits: 1))" }
func displayDateTime(_ value: String, compact: Bool = false) -> String {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = iso.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    if let date {
        let formatter = DateFormatter()
        formatter.dateFormat = compact ? "MM/dd HH:mm:ss" : "yyyy/MM/dd HH:mm"
        return formatter.string(from: date)
    }
    // Date-only catalog entries have no time to display.
    return String(value.prefix(10))
}
func modelVersionLabel(gpt: String, sovits: String, versions: [ModelVersion], weights: [WeightEntry] = []) -> String {
    if let version = versions.first(where: { $0.gpt == gpt && $0.sovits == sovits }) {
        return "\(version.label) · \(displayEngine(version.engine))"
    }
    func component(_ name: String, type: String, title: String) -> String {
        let entry = weights.first { $0.name == name && $0.type == type }
        if let label = entry?.display_name, !label.isEmpty { return "\(title) · \(label)" }
        let pattern = try! NSRegularExpression(pattern: #"(?i)(?:^|[-_])e(?:poch)?[_-]?(\d+)"#)
        let match = pattern.firstMatch(in: name, range: NSRange(name.startIndex..., in: name))
        let epoch = entry?.epoch ?? match.flatMap { Range($0.range(at: 1), in: name) }.flatMap { Int(name[$0]) }
        if let epoch {
            let date = entry.map { String($0.created_at.prefix(10)) } ?? ""
            return "\(title) " + (date.isEmpty ? "" : "\(date) · ") + "第 \(epoch) 轮"
        }
        if let entry { return "\(title) · \(displaySource(entry.source)) · \(displayDateTime(entry.created_at))" }
        return "\(title) 自定模型"
    }
    return component(gpt, type: "gpt", title: "GPT") + " · " + component(sovits, type: "sovits", title: "SoVITS")
}
