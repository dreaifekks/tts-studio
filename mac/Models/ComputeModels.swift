import Foundation

struct EngineSpeed: Codable, Sendable {
    var sec_per_item: Double?
    var source: String
    var sourceLabel: String { ["measured":"已实测", "reference":"参考值", "untested":"未测"][source] ?? source }
    var hasValidPositiveSpeed: Bool {
        guard source != "untested", let seconds = sec_per_item else { return false }
        return seconds > 0
    }
    func effectiveSource(allowZero: Bool = false) -> String {
        guard source != "untested", let seconds = sec_per_item else { return "untested" }
        if allowZero {
            return seconds >= 0 ? source : "untested"
        }
        return seconds > 0 ? source : "untested"
    }
    func effectiveSourceLabel(allowZero: Bool = false) -> String {
        let resolved = effectiveSource(allowZero: allowZero)
        return ["measured":"已实测", "reference":"参考值", "untested":"未测"][resolved] ?? resolved
    }
    var grade: String? {
        guard hasValidPositiveSpeed, let seconds = sec_per_item else { return nil }
        return seconds <= 4 ? "快" : seconds <= 12 ? "中" : "慢"
    }
}
struct ComputeHardware: Codable, Sendable {
    var accel: String?
    var os: String? = nil
    var cores: Int? = nil
    var gpu: String? = nil
    var osLabel: String { ["darwin":"macOS", "linux":"Linux", "windows":"Windows"][os ?? ""] ?? os ?? "暂未识别" }
    var accelerationLabel: String {
        if accel == "cuda" { return gpu.map { "NVIDIA · " + $0 } ?? "NVIDIA 显卡" }
        if accel == "mps" { return "Apple 芯片" }
        return cores.map { "CPU · \($0) 核" } ?? "CPU"
    }
}
struct DeviceWake: Codable, Sendable {
    var mac: String; var broadcast: String? = nil
    var jsonValue: JSONValue { .object(["mac":.string(mac), "broadcast":broadcast.map(JSONValue.string) ?? .null]) }
}
struct ComputeDevice: Codable, Sendable, Identifiable {
    var id: String
    var name: String
    var type: String
    var status: String
    var status_reason: String?
    var activity: String?
    var address: String?
    var engines: [String]
    var speeds: [String: EngineSpeed]
    var gpu: String?
    var wake: DeviceWake?
    var wakeable: Bool? = nil
    var hw: ComputeHardware? = nil
    var last_seen: String? = nil
    var caps: [String]? = nil
    var warm: Int? = nil
    var disk_free_bytes: Int64? = nil
    var disk_total_bytes: Int64? = nil
    var cache_bytes: Int64? = nil
    var statusLabel: String {
        if status == "offline", wakeable == true { return "睡眠中" }
        if (status == "busy" || status == "training"), let activity, !activity.isEmpty { return activity }
        return ["idle":"空闲", "busy":"忙", "training":"正在训练", "offline":"离线", "paused":"已暂停", "unavailable":"未开", "waking":"唤醒中"][status] ?? status
    }
    var typeLabel: String { ["local":"本机", "lan":"局域网设备", "cloud":"云端 GPU"][type] ?? type }
    var isCloudActive: Bool { type == "cloud" && ["busy", "training", "waking"].contains(status) }
    var isCloudWarm: Bool { type == "cloud" && (warm ?? 0) > 0 }
    var isDiskLow: Bool {
        guard let free = disk_free_bytes else { return false }
        return free < StorageOverview.lowDiskThresholdBytes
    }
    var diskSummary: String? {
        guard let free = disk_free_bytes, let total = disk_total_bytes, total > 0 else { return nil }
        var text = "本机磁盘可用 \(displayBytes(free)) / \(displayBytes(total))"
        if let cache = cache_bytes, cache > 0 {
            text += " · 缓存 \(displayBytes(cache))"
        }
        return text
    }
}
struct AutoSettings: Codable, Sendable {
    var mode = "auto"
    var device: String?
    var ask_over: Double = 2
    var ask_month: Double = 500
}
struct ComputeList: Decodable, Sendable { var devices: [ComputeDevice]; var auto: AutoSettings; var engines: [String]; var month_used: Double }
struct MachineRef: Codable, Sendable { var machine_id: String; var name: String }
struct LibraryCounts: Codable, Sendable {
    var characters: Int; var refs: Int; var datasets: Int; var weights: Int
    var evals: Int; var blind: Int; var works: Int; var runs: Int; var bench: Int
    var summary: String { "\(characters) 个角色 · \(weights) 个模型版本 · \(refs) 条参考音频 · \(datasets) 个训练数据集 · \(evals) 次评测 · \(blind) 场盲听" }
}
struct LibraryInfo: Decodable, Sendable {
    var selected: Bool
    var error: String?
    var path: String?
    var kind: String?
    var owner: MachineRef?
    var owned: Bool
    var this_machine: MachineRef
    var counts: LibraryCounts?
    var conflicts: Int
    var conflict_files: [String]
    var broken_files: [String]
    var scanned_at: String?
}
struct PlanPart: Codable, Sendable { var engine: String; var items: Int }
struct PlanInput: Codable, Sendable {
    var kind: String
    var engine: String? = nil
    var engines: [String]? = nil
    var parts: [PlanPart]? = nil
    var items = 1
    var lines = 0
    var gpt_epochs: Int? = nil
    var sovits_epochs: Int? = nil
    var gpt_save_every: Int? = nil
    var sovits_save_every: Int? = nil
    var character: String? = nil
    var version: String? = nil
    var gpt: String? = nil
    var sovits: String? = nil
    var ref_id: String? = nil
    var aux_ref_ids: [String]? = nil
    var fragment_interval: Double? = nil
    var cloud_capable: Bool? = nil
}
typealias PlanRequest = PlanInput
struct PlanCandidate: Codable, Sendable, Identifiable {
    var device: String
    var name: String
    var type: String
    var gpu: String?
    var seconds: Int?
    var units: Double?
    var speed_source: String?
    var available: Bool
    var reason_code: String?
    var reason: String?
    var idle: Bool
    var premium: Bool
    var wake: Bool? = nil
    var prep_sec: Double? = nil
    var id: String { device }
    var unavailableReason: String {
        if reason_code == "worker_outdated", let reason, !reason.isEmpty { return reason }
        return ["offline":"设备离线", "paused":"已暂停使用", "training":"正在训练", "unavailable":"暂时不能使用", "kind_unsupported":"不支持这项任务", "cloud_only":"这项任务需要云端 GPU", "no_rate":"暂时没有报价", "insufficient_balance":"剩余单元不够", "cloud_inputs_missing":"训练资料还没同步完成", "engine_missing":"没有这个模型", "not_enough_memory":"可用内存不足", "untested":"还没有速度数据", "over_max_minutes":"预计时间超过了最长使用时间", "worker_outdated":"局域网设备版本较旧，请先更新"][reason_code ?? ""] ?? "暂时不能使用"
    }
    var cost: String { type == "cloud" ? units.map { "约 \(displayUnits($0)) 单元" } ?? "暂无报价" : "免费" }
}
struct ComputePlan: Codable, Sendable {
    var recommended: String?
    var reason_code: String
    var reason: String
    var chosen: String?
    var candidates: [PlanCandidate]
    var confirm: [String]
    var month_used: Double
    var ask_over: Double
    var ask_month: Double
    var selected: PlanCandidate? { candidates.first { $0.device == chosen } }
    var reminder: String { reminder(for: selected) }
    func reminder(for candidate: PlanCandidate?) -> String {
        guard let candidate, candidate.type == "cloud", let units = candidate.units else { return "" }
        var lines: [String] = []
        if ask_over == 0 || units > ask_over { lines.append("一次 \(displayUnits(ask_over)) 单元") }
        if ask_month == 0 || month_used + units > ask_month { lines.append("本月 \(displayUnits(ask_month)) 单元") }
        let limits = "一次 \(displayUnits(ask_over)) 单元、本月 \(displayUnits(ask_month)) 单元"
        let reminder = lines.isEmpty ? (candidate.premium ? "你设的提醒线是「\(limits)」。" : "") : "超过了你设的「" + lines.joined(separator: "、") + "」提醒线。"
        return reminder + (candidate.premium ? "这张卡的单元花费较高。" : "")
    }
}
struct SweepPlan: Decodable, Sendable { var plan: ComputePlan?; var groups: [SweepGroup] }
struct SweepGroup: Codable, Sendable, Identifiable {
    var slug: String; var character: String; var engine: String; var status: String
    var lora_rank: Int? = nil
    var error: String?
    var id: String { slug }
}
struct SweepTrain: Codable, Sendable, Identifiable, Equatable {
    var engine: String? = nil
    var sovits_epochs = 8; var gpt_epochs = 15
    var sovits_save_every = 4; var gpt_save_every = 5
    var sovits_batch = 8; var gpt_batch = 8
    var lora_rank: Int? = nil
    var id: String { "\(sovits_epochs)-\(gpt_epochs)-\(sovits_save_every)-\(gpt_save_every)-\(sovits_batch)-\(gpt_batch)-\(lora_rank ?? 0)" }
}
struct SweepRequest: Codable, Sendable {
    var characters: [String] = []
    var engines: [String] = []
    var trains: [SweepTrain] = [SweepTrain()]
    var min_sec: Double = 1; var max_sec: Double = 12
    var exclude_interjections = true
    var max_lines: Int? = nil
    var keep = 3; var samples = 2
    var ready: Bool { !characters.isEmpty && !engines.isEmpty && !trains.isEmpty }
}
struct JobCompute: Codable, Sendable { var device: String; var name: String; var type: String }
struct CloudAccount: Codable, Sendable, Identifiable {
    var id: String; var label: String; var email: String?; var balance: Double?
    var month_used_estimate: Double; var gpus: [String]; var disabled: Bool; var error: String?
    var displayName: String { email ?? label }
}
struct CloudGPU: Codable, Sendable, Identifiable {
    var gpu: String; var label: String; var account_count: Int
    var units_per_hour: Double?; var hours_left: Double?; var available: Bool
    var reason: String?; var speeds: [String: EngineSpeed]
    var id: String { gpu }
}
struct CloudSettings: Codable, Sendable, Equatable {
    var gpu_pref = "auto"
    var max_minutes: Int? = nil
    var keep_minutes = 0
    enum CodingKeys: String, CodingKey { case gpu_pref, max_minutes, keep_minutes }
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(gpu_pref, forKey: .gpu_pref)
        try container.encode(keep_minutes, forKey: .keep_minutes)
        if let max_minutes { try container.encode(max_minutes, forKey: .max_minutes) }
        else { try container.encodeNil(forKey: .max_minutes) }
    }
}
struct CloudUsage: Codable, Sendable, Identifiable { var month: String; var by_account: [String: Double]; var id: String { month } }
struct CloudInfo: Codable, Sendable {
    var installed: Bool
    var accounts: [CloudAccount]; var gpus: [CloudGPU]
    var total_balance: Double; var month_used_estimate: Double
    var enabledAccountCount: Int { accounts.filter { !$0.disabled }.count }
    var usage_monthly: [CloudUsage]; var account_labels: [String: String]; var settings: CloudSettings
}
struct CloudLogin: Decodable, Sendable { var login_id: String; var auth_url: URL; var needs_code: Bool }
struct CloudLoginStatus: Decodable, Sendable { var state: String; var account: CloudAccount?; var added: Bool? = nil; var error: String? }
struct SystemVersions: Decodable, Sendable {
    struct Studio: Decodable, Sendable { var current: String; var latest: String?; var notes_url: URL?; var error: String? }
    struct Core: Decodable, Sendable { var current: String }
    var studio: Studio; var core: Core
}
let canonicalEngineOrder = ["v2proplus", "v2pro", "v4", "v5turbo", "v5dev", "v3", "v2", "v1"]

func displayEngine(_ engine: String) -> String {
    switch engine {
    case "v2pro": "v2Pro"
    case "v2proplus": "v2ProPlus"
    case "v5turbo": "v5 Turbo"
    case "v5dev": "v5 Dev"
    case "v4", "v3", "v2", "v1": engine
    default: engine
    }
}

func engineSortRank(_ engine: String) -> Int {
    canonicalEngineOrder.firstIndex(of: engine) ?? canonicalEngineOrder.count
}

func sortedEngines<S: Sequence>(_ engines: S) -> [String] where S.Element == String {
    var seen: Set<String> = []
    let unique = engines.filter { !$0.isEmpty && seen.insert($0).inserted }
    return unique.sorted { lhs, rhs in
        let l = engineSortRank(lhs)
        let r = engineSortRank(rhs)
        if l != r { return l < r }
        return lhs < rhs
    }
}

func sortedEngineInfos(_ engines: [EngineInfo], installed: Set<String> = []) -> [EngineInfo] {
    engines.sorted { lhs, rhs in
        if !installed.isEmpty {
            let lInstalled = installed.contains(lhs.id)
            let rInstalled = installed.contains(rhs.id)
            if lInstalled != rInstalled { return lInstalled }
        }
        let lRank = engineSortRank(lhs.id)
        let rRank = engineSortRank(rhs.id)
        if lRank != rRank { return lRank < rRank }
        return lhs.id < rhs.id
    }
}

func engineKeysForSpeedTable(engines: [String], speeds: [String: EngineSpeed]) -> [String] {
    let speedEngines = speeds.keys.filter { $0 != "train" && $0 != "scorer" }
    return sortedEngines(engines + speedEngines)
}

func displaySpeedSeconds(_ seconds: Double) -> String {
    if seconds < 9.95 {
        return String(format: "%.1f", seconds)
    }
    return String(Int(seconds.rounded()))
}

func displayModelSpeed(_ speed: EngineSpeed?) -> String {
    guard let speed, speed.hasValidPositiveSpeed, let seconds = speed.sec_per_item else { return "未测" }
    return "约 \(displaySpeedSeconds(seconds)) 秒/句"
}

func displayTrainSpeed(_ speed: EngineSpeed?) -> String {
    guard let speed, speed.hasValidPositiveSpeed, let seconds = speed.sec_per_item else { return "未测" }
    let totalSeconds = Int((seconds * 1000).rounded())
    let thousandLabel: String
    if totalSeconds >= 7200 {
        thousandLabel = "千句约 \(Int((Double(totalSeconds) / 3600).rounded())) 小时"
    } else {
        thousandLabel = "千句约 \(max(1, Int((Double(totalSeconds) / 60).rounded()))) 分钟"
    }
    return "约 \(displaySpeedSeconds(seconds)) 秒/行 · \(thousandLabel)"
}

func displayScorerSpeed(_ speed: EngineSpeed?) -> String {
    guard let speed, speed.effectiveSource(allowZero: true) != "untested", let seconds = speed.sec_per_item else { return "未测" }
    if seconds == 0 {
        return "已含在评测耗时内"
    }
    return "约 \(displaySpeedSeconds(seconds)) 秒/条"
}
func displayUnits(_ units: Double) -> String {
    let scale = units < 10 ? 10.0 : 1.0
    let value = (units * scale).rounded(.toNearestOrAwayFromZero) / scale
    return value.formatted(.number.precision(.fractionLength(value == value.rounded() ? 0 : 1)))
}
func displayDuration(_ seconds: Int?) -> String {
    guard let seconds else { return "时间待测" }
    if seconds >= 7200 { return "约 \(Int((Double(seconds) / 3600).rounded())) 小时" }
    return "约 \(max(1, Int((Double(seconds) / 60).rounded()))) 分钟"
}

func displayGPU(_ gpu: String) -> String { gpu == "G4" ? "RTX PRO 6000" : gpu }

func displayEvalSet(_ value: String) -> String { ["hard":"难句评测", "mini":"快速评测"][value] ?? value }
func computeReason(_ code: String) -> String {
    ["local_fast_enough":"本机就很快", "lan_faster":"这台空闲、能更快完成", "cloud_halves":"云端能省一半以上时间", "train_needs_gpu":"训练用云端更快", "local_untested":"时间待测", "fastest":"这台更合适", "only_cloud":"这项任务用云端 GPU", "pinned":"已固定这台"][code] ?? ""
}
func computeFailure(_ detail: String) -> String {
    detail.replacingOccurrences(of: "compute_unavailable", with: "设备暂时不可用")
        .replacingOccurrences(of: "Not Found", with: "暂时找不到这项内容")
        .replacingOccurrences(of: "timed out", with: "连接超时")
        .replacingOccurrences(of: "worker", with: "设备")
}

struct SystemInfo: Codable, Sendable {
    var web_url: URL
    var host: String
    var port: Int
    var lan_reachable: Bool
    var web_available: Bool
    var machine: String
}

struct ComputeInvite: Decodable, Sendable {
    struct Commands: Decodable, Sendable { var unix: String; var windows: String? }
    var id: String
    var code: String
    var expires_at: String
    var expires_in: Double
    var host: String
    var commands: Commands
}
struct ComputeInviteStatus: Decodable, Sendable {
    var id: String
    var state: String
    var expires_at: String?
    var expires_in: Double
    /// 配对码已核销：那台电脑已经连回来，在等它装好、报第一次心跳
    var redeemed: Bool
    var device: ComputeDevice?
    var installing: Bool { state == "waiting" && redeemed }
}

/// Formats a byte count into human-readable decimal units (matching macOS Finder / SI conventions).
func displayBytes(_ bytes: Int64) -> String {
    guard bytes > 0 else { return "0 B" }
    let value = Double(bytes)
    if value < 1_000 {
        return "\(bytes) B"
    }
    let posix = Locale(identifier: "en_US_POSIX")
    func formatUnit(_ scaled: Double, unit: String, keepDecimalAboveTen: Bool) -> String {
        if keepDecimalAboveTen || scaled < 10 {
            let rounded = (scaled * 10).rounded(.toNearestOrAwayFromZero) / 10
            if rounded == rounded.rounded() {
                return "\(Int64(rounded)) \(unit)"
            }
            return String(format: "%.1f %@", locale: posix, rounded, unit)
        }
        return "\(Int64(scaled.rounded(.toNearestOrAwayFromZero))) \(unit)"
    }
    if value < 1_000_000 {
        let kb = value / 1_000
        if kb >= 999.5 { return "1 MB" }
        return formatUnit(kb, unit: "KB", keepDecimalAboveTen: false)
    }
    if value < 1_000_000_000 {
        let mb = value / 1_000_000
        if mb >= 999.5 { return "1 GB" }
        return formatUnit(mb, unit: "MB", keepDecimalAboveTen: false)
    }
    if value < 1_000_000_000_000 {
        let gb = value / 1_000_000_000
        if gb >= 999.95 { return "1 TB" }
        return formatUnit(gb, unit: "GB", keepDecimalAboveTen: true)
    }
    let tb = value / 1_000_000_000_000
    return formatUnit(tb, unit: "TB", keepDecimalAboveTen: true)
}

struct StorageCategory: Codable, Sendable, Identifiable, Equatable {
    var key: String
    var label: String
    var bytes: Int64
    var local_bytes: Int64? = nil
    var files: Int? = nil

    var id: String { key }

    /// True when local cached size differs significantly (>5% and visibly) from logical size.
    var hasNotableLocalDifference: Bool {
        guard let localBytes = local_bytes, bytes > 0 else { return false }
        let ratio = Double(abs(bytes - localBytes)) / Double(bytes)
        return ratio > 0.05 && displayBytes(localBytes) != displayBytes(bytes)
    }

    func sizeLabel(isDrive: Bool) -> String {
        let logical = displayBytes(bytes)
        if isDrive, hasNotableLocalDifference, let localBytes = local_bytes {
            return "\(logical)（本机 \(displayBytes(localBytes))）"
        }
        return logical
    }
}

struct DiskUsage: Codable, Sendable, Equatable {
    var path: String? = nil
    var total_bytes: Int64
    var used_bytes: Int64
    var free_bytes: Int64

    var usedFraction: Double {
        guard total_bytes > 0 else { return 0 }
        return min(1, max(0, Double(used_bytes) / Double(total_bytes)))
    }

    var isLowSpace: Bool {
        free_bytes < StorageOverview.lowDiskThresholdBytes
    }
}

struct LibraryStorage: Codable, Sendable, Equatable {
    var selected: Bool
    var kind: String? = nil
    var path: String? = nil
    var total_bytes: Int64
    var local_bytes: Int64? = nil
    var categories: [StorageCategory]

    init(
        selected: Bool,
        kind: String? = nil,
        path: String? = nil,
        total_bytes: Int64 = 0,
        local_bytes: Int64? = nil,
        categories: [StorageCategory] = []
    ) {
        self.selected = selected
        self.kind = kind
        self.path = path
        self.total_bytes = total_bytes
        self.local_bytes = local_bytes
        self.categories = categories
    }

    enum CodingKeys: String, CodingKey {
        case selected, kind, path, total_bytes, local_bytes, categories
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        selected = try container.decode(Bool.self, forKey: .selected)
        kind = try container.decodeIfPresent(String.self, forKey: .kind)
        path = try container.decodeIfPresent(String.self, forKey: .path)
        total_bytes = try container.decodeIfPresent(Int64.self, forKey: .total_bytes) ?? 0
        local_bytes = try container.decodeIfPresent(Int64.self, forKey: .local_bytes)
        categories = try container.decodeIfPresent([StorageCategory].self, forKey: .categories) ?? []
    }

    var isDrive: Bool { kind == "drive" }

    var summaryLabel: String {
        if isDrive {
            let totalText = "Google Drive · 文件总大小 \(displayBytes(total_bytes))"
            guard let localBytes = local_bytes else { return totalText }
            return "\(totalText) · 本机占用 \(displayBytes(localBytes))"
        }
        return "资料库共 \(displayBytes(total_bytes))"
    }
}

struct LocalStorage: Codable, Sendable, Equatable {
    var machine: String
    var total_bytes: Int64
    var local_bytes: Int64? = nil
    var categories: [StorageCategory]

    init(
        machine: String = "",
        total_bytes: Int64 = 0,
        local_bytes: Int64? = nil,
        categories: [StorageCategory] = []
    ) {
        self.machine = machine
        self.total_bytes = total_bytes
        self.local_bytes = local_bytes
        self.categories = categories
    }

    enum CodingKeys: String, CodingKey {
        case machine, total_bytes, local_bytes, categories
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        machine = try container.decodeIfPresent(String.self, forKey: .machine) ?? ""
        total_bytes = try container.decodeIfPresent(Int64.self, forKey: .total_bytes) ?? 0
        local_bytes = try container.decodeIfPresent(Int64.self, forKey: .local_bytes)
        categories = try container.decodeIfPresent([StorageCategory].self, forKey: .categories) ?? []
    }

    var sectionTitle: String {
        let trimmed = machine.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "本机存储" : "\(trimmed) 本机存储"
    }
}

struct StorageOverview: Codable, Sendable, Equatable {
    static let lowDiskThresholdBytes: Int64 = 20_000_000_000

    var measured_at: String? = nil
    var disk: DiskUsage
    var library: LibraryStorage
    var local: LocalStorage
}
