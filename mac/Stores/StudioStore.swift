import SwiftUI
import Observation
import UniformTypeIdentifiers

@MainActor @Observable
final class StudioStore {
    static let shared = StudioStore()
    var page: StudioPage = .generate
    var characterTab: CharacterTab = .versions
    var generateMode: GenerateMode = .candidates
    var generateRightTab: GenerateRightTab = .candidates
    var compareEngines: Set<String> = []
    var pendingBlindPrefill: BlindPrefill?
    var pendingComposerVersionID: String?
    var refCountsByCharacter: [String: Int] = [:]
    var characterVersionsCache: [String: [ModelVersion]] = [:]
    var refsByCharacterCache: [String: [Ref]] = [:]
    var savingPrefs = false
    var settingDefaultVersion = false
    var workSaving = false
    private var workEditRevision = 0
    private struct PendingRefPrefs {
        let slug: String
        let lastRefID: String?
        let auxRefIDs: [String]
        let generation: Int
    }
    private var pendingRefPrefs: PendingRefPrefs?
    private var refPrefsSaveTask: Task<Void, Never>?
    var api: APIClient
    let connection: StudioConnection
    @ObservationIgnored lazy var compute = ComputeStore(api: api, defaults: defaults)
    var settingsTab: SettingsTab = .compute
    let generationPlanner = TaskPlanner()
    let comparisonPlanner = TaskPlanner()
    let workPlanner = TaskPlanner()
    let composerVersions = ModelVersionStore()
    var composerManual = false
    let trainingPlanner = TaskPlanner()
    let sweepPlanner = TaskPlanner()
    let audio = AudioPlayer()
    let versions = VersionStore()
    let coreInstaller = CoreInstaller()
    @ObservationIgnored lazy var weightManager = WeightStore(api: api, defaults: defaults, character: params.character, engine: params.engine)
    @ObservationIgnored lazy var blindManager = BlindStore(api: api, onJobCreated: { [weak self] id in
        guard let self else { return }
        self.recordLocalJob(id)
        self.perform {
            let job: Job = try await self.api.get("/jobs/\(id)")
            self.updateJob(job)
            if job.active { self.monitor(job) }
        }
    })
    var models: [CharacterInfo] = []
    var engines: [EngineInfo] = []
    var composerWeights: [WeightEntry] = []
    var composerWeightsLoading = false
    var trainingJobs: [Job] = []
    var trainingJobID: String?
    var refs: [Ref] = []
    var selectedReferenceID: String?
    var referenceAudition: ReferenceAudition?
    var candidates: [AudioItem] = []
    var selectedCandidate: String?
    var jobs: [Job] = []
    private(set) var localJobIDs: Set<String> = []
    func recordLocalJob(_ id: String) {
        localJobIDs.insert(id)
        defaults.set(Array(localJobIDs), forKey: "localJobIDs")
    }
    var works: [WorkSummary] = []
    var work: Work?
    var workDirty = false
    var selectedWorkLineIDs: Set<String> = []
    var staleLines: [String: [String]] = [:]
    var obsoleteCandidates: [String: [String: [String]]] = [:]
    private(set) var trainingCatalogLoaded = false
    private let defaults: UserDefaults
    var dirtyRenders: Set<String> = []
    func invalidateRender(_ workID: String? = nil) {
        guard let id = workID ?? work?.id else { return }
        dirtyRenders.insert(id)
        defaults.set(Array(dirtyRenders), forKey: "dirtyWorkRenders")
        if work?.id == id { work?.render = nil }
    }
    func forgetWorkState(_ id: String) {
        staleLines.removeValue(forKey: id)
        obsoleteCandidates.removeValue(forKey: id)
        defaults.set(obsoleteCandidates, forKey: "obsoleteWorkCandidates")
        dirtyRenders.remove(id)
        defaults.set(staleLines, forKey: "staleWorkLines")
        defaults.set(Array(dirtyRenders), forKey: "dirtyWorkRenders")
    }
    func changeLine(_ value: WorkLine) {
        guard let id = work?.id, let index = work?.lines.firstIndex(where: { $0.id == value.id }) else { return }
        if work!.lines[index].synthesisInput != value.synthesisInput {
            let oldIDs = work!.lines[index].candidates.map(\.id)
            if !oldIDs.isEmpty {
                var rows = obsoleteCandidates[id] ?? [:]
                rows[value.id] = Array(Set((rows[value.id] ?? []) + oldIDs))
                obsoleteCandidates[id] = rows
                defaults.set(obsoleteCandidates, forKey: "obsoleteWorkCandidates")
            }
            staleLines[id] = Array(Set((staleLines[id] ?? []) + [value.id]))
            defaults.set(staleLines, forKey: "staleWorkLines")
        }
        work?.lines[index] = value
        invalidateRender()
        markWorkEdited()
    }
    func markWorkEdited() {
        workEditRevision += 1
        workDirty = true
    }
    func needsRegeneration(_ line: WorkLine) -> Bool {
        (staleLines[work?.id ?? ""] ?? []).contains(line.id) || line.picked == nil
    }
    func canPick(_ audio: AudioItem, for line: WorkLine) -> Bool {
        line.matches(audio, engineSupportsAuxRefs: engineSupportsAuxRefs(line.engine))
            && !(obsoleteCandidates[work?.id ?? ""]?[line.id] ?? []).contains(audio.id)
    }
    func isDeletedTrainingCharacter(_ job: Job) -> Bool {
        job.status == "succeeded" && trainingCatalogLoaded && !models.contains(where: { $0.slug == job.target })
    }
    private func refreshTrainingCatalog() async throws {
        trainingCatalogLoaded = false
        try await reloadModels(resetSelection: false)
        trainingCatalogLoaded = true
    }
    private func clearStaleLines(workID: String, lineIDs: [String]) {
        staleLines[workID]?.removeAll { lineIDs.contains($0) }
        defaults.set(staleLines, forKey: "staleWorkLines")
    }
    var datasets: [DatasetInfo] = []
    var history = AudioPage(total: 0, items: [])
    var stats: ReviewStats?
    var evaluation: TrainEval?
    var computeSummary: String {
        if connection.waking { return "\(connection.controllerName) 唤醒中" }
        if connected { return compute.summary }
        if connection.localCoreMissing { return "本机未安装主控服务" }
        return "主控不在线"
    }
    var unavailableTitle: String {
        if connection.localCoreMissing { return "本机未安装主控服务" }
        return connection.waking ? "\(connection.controllerName) 唤醒中" : "主控不在线"
    }
    var unavailableDescription: String {
        if connection.localCoreMissing { return "这台电脑还没安装主控服务，可以在设置里改选「连接到另一台」。" }
        return connection.waking ? "连接后会继续显示任务和资料库。" : "连接主控后继续。"
    }
    var shouldPromptLocalLibrarySelection: Bool { !connection.isClient }
    var status = "准备就绪"
    private var waitingRequests: [UUID: (message: String, time: Date)] = [:]
    var waitingStatus: String? { waitingRequests.values.max { $0.time < $1.time }?.message }
    var needsLibrarySelection = false
    var errorMessage: String?
    var connected = false
    var generationBusy = false
    var generationTask: Task<Void, Never>?
    var workTask: Task<Void, Never>?
    private(set) var generationJobID: String?
    private var tagQueue: Task<Void, Never>?
    private var reviewWrites: [String: Task<Void, Error>] = [:]
    private struct ReviewSnapshot { let value: Review? }
    private var reviewSnapshots: [String: ReviewSnapshot] = [:]
    var reviewRevision = 0
    var workBusy = false
    var params = CandidatesParams(text: "おはよう。今日はいい天気だね。")
    var referenceMode = "默认"
    var emotion = "平静"
    private var started = false
    private var initialLoading = false
    private var initialTask: Task<Void, Never>?
    private var probeGeneration = 0
    private var healthFailures = 0
    private var monitors: [String: Task<Void, Never>] = [:]
    private var computeRefreshTask: Task<Void, Never>?

    init(defaults: UserDefaults = .standard, connection: StudioConnection? = nil) {
        self.defaults = defaults
        localJobIDs = Set(defaults.stringArray(forKey: "localJobIDs") ?? [])
        dirtyRenders = Set(defaults.stringArray(forKey: "dirtyWorkRenders") ?? [])
        obsoleteCandidates = defaults.dictionary(forKey: "obsoleteWorkCandidates") as? [String: [String: [String]]] ?? [:]
        staleLines = defaults.dictionary(forKey: "staleWorkLines") as? [String: [String]] ?? [:]
        self.connection = connection ?? StudioConnection(defaults: defaults)
        api = APIClient(baseURL: self.connection.baseURL)
        api.onStatus = { [weak self] message in self?.status = message }
        api.onWaitState = { [weak self] id, message in
            if let message { self?.waitingRequests[id] = (message, Date()) }
            else { self?.waitingRequests.removeValue(forKey: id) }
        }
        let savedCount = defaults.integer(forKey: "defaultCandidateCount")
        params.n = (1...16).contains(savedCount) ? savedCount : 6
        let savedCommon = Set(defaults.stringArray(forKey: "commonModels") ?? ["v2proplus"])
        params.engine = ComputeStore.resolvePreferredEngine(commonModels: savedCommon, availableEngines: [])
        coreInstaller.onSucceeded = { [weak self] version, port in
            await self?.handleCoreInstallSucceeded(version: version, port: port)
        }
    }
    func handleCoreInstallSucceeded(version: String, port: Int) async {
        connection.role = "control"
        connection.localPort = port
        connection.notifyLocalCoreChanged()
        connection.save()
        let target = "http://127.0.0.1:\(port)"
        if api.baseURL != target {
            try? setBaseURL(target)
            initialTask?.cancel(); initialTask = nil
            initialLoading = false
        } else {
            api.generation += 1
            probeGeneration += 1
            initialTask?.cancel(); initialTask = nil
            initialLoading = false
            generationTask?.cancel(); workTask?.cancel(); weightManager.evaluationTask?.cancel()
            computeRefreshTask?.cancel(); computeRefreshTask = nil
            for task in monitors.values { task.cancel() }
            monitors.removeAll()
            connected = false
            started = false
            healthFailures = 0
            versions.checking = false
            versions.error = nil
        }
        if !version.isEmpty {
            versions.serverVersion = version
        }
        await refreshConnection()
        if connected {
            await versions.readCurrent(api: api)
        }
    }
    var defaultCandidateCount: Int {
        get {
            let value = defaults.integer(forKey: "defaultCandidateCount")
            return (1...16).contains(value) ? value : 6
        }
        set {
            let clamped = min(16, max(1, newValue))
            defaults.set(clamped, forKey: "defaultCandidateCount")
        }
    }
    var defaultExportFormat: ExportFormat {
        get { ExportFormat(rawValue: defaults.string(forKey: "defaultExportFormat") ?? "wav") ?? .wav }
        set { defaults.set(newValue.rawValue, forKey: "defaultExportFormat") }
    }
    var activeCharacter: String {
        get { params.character }
        set { selectCharacter(newValue) }
    }
    var character: CharacterInfo? { models.first { $0.slug == params.character } }
    var currentEngine: EngineInfo? { engines.first { $0.id == params.engine } }
    var installedComputeEngines: Set<String> {
        let deviceEngines = Set(compute.devices.flatMap(\.engines))
        if !deviceEngines.isEmpty { return deviceEngines }
        return Set(compute.engines)
    }
    var trainableComputeEngines: Set<String> {
        let trainableDevices = compute.devices.filter { $0.type == "local" || $0.type == "cloud" }
        if !trainableDevices.isEmpty {
            return Set(trainableDevices.flatMap(\.engines))
        }
        if !compute.devices.isEmpty {
            return []
        }
        return Set(compute.engines)
    }
    var allKnownEngines: [String] {
        let combined = compute.engines + engines.map(\.id)
        return sortedEngines(combined)
    }
    var sortedAllEngines: [EngineInfo] {
        sortedEngineInfos(engines, installed: installedComputeEngines)
    }
    var sortedTrainableEngines: [EngineInfo] {
        sortedEngineInfos(engines.filter(\.trainable), installed: trainableComputeEngines)
    }
    var preferredEngine: String {
        let pool = compute.engines.isEmpty ? engines.map(\.id) : compute.engines
        return ComputeStore.resolvePreferredEngine(commonModels: compute.commonModels, availableEngines: pool)
    }
    func preferredTrainableEngine(from candidates: [EngineInfo]? = nil) -> EngineInfo? {
        let list = candidates ?? sortedTrainableEngines
        guard !list.isEmpty else { return nil }
        let installed = trainableComputeEngines
        let installedList = installed.isEmpty ? list : list.filter { installed.contains($0.id) }
        let scope = installedList.isEmpty ? list : installedList
        let preferred = ComputeStore.resolvePreferredEngine(
            commonModels: compute.commonModels,
            availableEngines: scope.map(\.id)
        )
        return scope.first(where: { $0.id == preferred }) ?? scope.first
    }
    func engineSupportsAuxRefs(_ engineID: String) -> Bool {
        engines.first(where: { $0.id == engineID })?.supports_aux_refs ?? false
    }
    var currentEngineSupportsAuxRefs: Bool { engineSupportsAuxRefs(params.engine) }
    var currentEngineSupportsSampleSteps: Bool { ["v3", "v4", "v5turbo", "v5dev"].contains(params.engine) }
    var comparisonEnginesList: [String] { sortedEngines(compareEngines.union([params.engine])) }
    func effectiveAuxRefIDs(for engineIDs: [String], primaryRefID: String? = nil) -> [String] {
        guard !engineIDs.isEmpty, engineIDs.allSatisfy({ engineSupportsAuxRefs($0) }) else { return [] }
        return normalizedAuxRefIDs(params.aux_ref_ids, primaryRefID: primaryRefID, engineSupportsAuxRefs: true)
    }
    var effectivePrimaryRefIDForCurrentMode: String? {
        switch referenceMode {
        case "情绪":
            return nil
        case "默认":
            return character?.default_ref
        default:
            return params.ref_id ?? character?.default_ref
        }
    }
    func effectiveGenerationCapabilities(for engineIDs: [String]) -> (auxRefIDs: [String], fragmentInterval: Double?) {
        let aux = effectiveAuxRefIDs(for: engineIDs, primaryRefID: effectivePrimaryRefIDForCurrentMode)
        let interval = abs(params.fragment_interval - 0.3) > 1e-9 ? params.fragment_interval : nil
        return (aux, interval)
    }
    var generationPlanInput: PlanInput {
        let caps = effectiveGenerationCapabilities(for: [params.engine])
        return PlanInput(
            kind: "gen",
            engine: params.engine,
            items: params.n,
            character: params.character.isEmpty ? nil : params.character,
            version: params.version,
            gpt: params.gpt,
            sovits: params.sovits,
            ref_id: effectivePrimaryRefIDForCurrentMode,
            aux_ref_ids: caps.auxRefIDs.isEmpty ? nil : caps.auxRefIDs,
            fragment_interval: caps.fragmentInterval
        )
    }
    func comparisonPlanInput(engines: [String]) -> PlanInput {
        let caps = effectiveGenerationCapabilities(for: engines)
        return PlanInput(
            kind: "gen",
            engines: engines,
            items: params.n,
            character: params.character.isEmpty ? nil : params.character,
            version: params.version,
            gpt: params.gpt,
            sovits: params.sovits,
            ref_id: effectivePrimaryRefIDForCurrentMode,
            aux_ref_ids: caps.auxRefIDs.isEmpty ? nil : caps.auxRefIDs,
            fragment_interval: caps.fragmentInterval
        )
    }
    var hasValidEmotionReference: Bool {
        guard referenceMode == "情绪" else { return true }
        return hasEligiblePrimaryRef(for: emotion, in: refs, character: params.character)
    }
    var weights: EngineWeights? { character?.engines[params.engine] }
    var composerIsZeroShot: Bool { !composerWeightsLoading && !composerWeights.isEmpty && !composerWeights.contains { $0.character == params.character } }
    func loadComposerWeights() async throws {
        composerWeightsLoading = true
        defer { composerWeightsLoading = false }
        let character = params.character, engine = params.engine
        let result = try await visibleWeights(character: character, engine: engine)
        try Task.checkCancellation()
        guard params.character == character && params.engine == engine else { return }
        composerWeights = result
    }
    func visibleWeights(character: String, engine: String, evalSet: String = "hard") async throws -> [WeightEntry] {
        async let owned: WeightList = api.get("/weights", query: ["character":character,"engine":engine,"hidden":"false","eval_set":evalSet])
        async let base: WeightList = api.get("/weights", query: ["source":"pretrained","engine":engine,"hidden":"false","eval_set":evalSet])
        let (a, b) = try await (owned, base)
        var seen: Set<String> = []
        return (a.weights + b.weights).filter { seen.insert($0.id).inserted }
    }
    var modelCompletionKey: String { jobs.filter { !$0.active && ["train", "sweep", "eval"].contains($0.kind) }.map { $0.id + $0.status }.joined(separator: "|") }
    var currentCandidate: AudioItem? { candidates.first { $0.id == selectedCandidate } }
    var activeJobs: [Job] { jobs.filter(\.active) }
    var localActiveJobs: [Job] { activeJobs.filter { localJobIDs.contains($0.id) } }
    var otherActiveJobs: [Job] { activeJobs.filter { !localJobIDs.contains($0.id) } }

    @discardableResult
    func perform(_ action: @escaping @MainActor () async throws -> Void) -> Task<Void, Never> {
        let token = api.generation
        return Task {
            guard api.isCurrent(token) else { return }
            do { try await action() }
            catch is CancellationError { }
            catch {
                api.ifCurrent(token) {
                    errorMessage = error.localizedDescription; status = error.localizedDescription
                    if let failure = error as? APIError, failure.code == "library_not_selected" { settingsTab = .library; needsLibrarySelection = true }
                }
            }
        }
    }
    var composerReady: Bool { composerManual || composerVersions.selected != nil }
    func startGeneration() {
        guard composerReady, !generationBusy else { return }
        generationTask = perform { try await self.generate() }
    }
    func performWork(_ action: @escaping @MainActor () async throws -> Void) { workTask = perform(action) }
    func stopGeneration() {
        generationTask?.cancel()
        if let job = jobs.first(where: { $0.id == generationJobID && $0.active }) { perform { try await self.cancel(job) } }
    }
    func stopWork() {
        workTask?.cancel()
        for job in activeJobs where job.target == work?.id { perform { try await self.cancel(job) } }
    }
    func loadInitial() async {
        guard !started, !initialLoading else { return }
        let token = api.generation
        let probe = probeGeneration
        initialLoading = true
        defer { if api.isCurrent(token) && probe == probeGeneration { initialLoading = false } }
        if !connected { await refreshConnection() }
        guard api.isCurrent(token), probe == probeGeneration, connected, !Task.isCancelled else { return }
        do {
            let result: ModelsResponse = try await api.get("/models")
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            models = result.characters; engines = result.engines; applyDefaults()
            let jobList: JobList = try await api.get("/jobs")
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            jobs = jobList.jobs
            for job in activeJobs { monitor(job) }
            started = true
            computeRefreshTask?.cancel(); computeRefreshTask = nil
            await compute.refresh()
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            await versions.checkLatest(api: api)
        } catch is CancellationError { }
        catch {
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
            if let failure = error as? APIError, failure.code == "library_not_selected" { settingsTab = .library; needsLibrarySelection = true }
        }
    }
    func refreshConnection() async {
        let token = api.generation
        let probe = probeGeneration
        let health: Health
        do {
            health = try await api.getWithoutRetry("/health")
        } catch {
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            healthFailures += 1
            if healthFailures >= 2 { connected = false }
            if error is URLError {
                do { try connection.wakeIfNeeded() } catch { errorMessage = error.localizedDescription }
            }
            return
        }
        guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
        let wasConnected = connected
        healthFailures = 0
        connected = health.status == "ok"
        connection.retryWake()
        trainingJobID = health.training_job
        if connected && !wasConnected {
            errorMessage = nil
            status = health.training_job.map { "正在训练：\($0)" } ?? "准备就绪"
        } else if let training = health.training_job {
            status = "正在训练：\(training)"
        }
        if connected && !started { await loadInitial(); return }
        do {
            try await compute.refreshDevices()
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            let library: LibraryInfo = try await api.getWithoutRetry("/library")
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            compute.library = library
            compute.error = nil
        } catch is CancellationError { }
        catch {
            guard api.isCurrent(token), probe == probeGeneration, !Task.isCancelled else { return }
            if let failure = error as? APIError, failure.code == "library_not_selected" { settingsTab = .library; needsLibrarySelection = true }
            else { compute.error = error.localizedDescription }
        }
    }
    func applyConnection(role: String, address: String, alias: String, wakeMAC: String, broadcast: String) throws {
        let trimmedAddress = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let target = role == "control" ? "http://127.0.0.1:\(connection.localPort)" : trimmedAddress
        _ = try APIClient(baseURL: target).url("/health")
        if role == "client", !wakeMAC.isEmpty { _ = try WakePacket.bytes(mac: wakeMAC) }
        connection.role = role
        connection.address = trimmedAddress
        connection.alias = alias
        connection.wakeMAC = wakeMAC
        connection.broadcast = broadcast
        connection.save()
        try setBaseURL(target)
    }
    func setBaseURL(_ string: String) throws {
        let cleaned = string.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let probe = APIClient(baseURL: cleaned)
        _ = try probe.url("/health")
        if cleaned == api.baseURL {
            if !connected {
                probeGeneration += 1
                initialTask?.cancel(); initialTask = nil
                initialLoading = false
                errorMessage = nil; compute.error = nil; healthFailures = 0
                initialTask = perform { await self.refreshConnection() }
            }
            return
        }
        api.baseURL = cleaned
        defaults.set(cleaned, forKey: "baseURL")
        api.generation += 1
        probeGeneration += 1
        initialTask?.cancel(); initialTask = nil
        initialLoading = false
        generationTask?.cancel(); workTask?.cancel(); weightManager.evaluationTask?.cancel()
        computeRefreshTask?.cancel(); computeRefreshTask = nil
        for task in monitors.values { task.cancel() }
        monitors.removeAll()
        compute.reset()
        versions.checking = false; versions.error = nil
        errorMessage = nil; status = "准备就绪"; trainingJobID = nil
        models = []; engines = []; jobs = []; trainingJobs = []
        refs = []; selectedReferenceID = nil; resetReferenceAudition()
        refCountsByCharacter = [:]; characterVersionsCache = [:]; refsByCharacterCache = [:]
        refPrefsSaveTask?.cancel(); refPrefsSaveTask = nil; pendingRefPrefs = nil
        settingDefaultVersion = false; workSaving = false
        pendingBlindPrefill = nil; pendingComposerVersionID = nil
        generationPlanner.reset(); comparisonPlanner.reset(); workPlanner.reset(); trainingPlanner.reset(); sweepPlanner.reset(); composerVersions.reset()
        weightManager.planner.reset(); weightManager.versions.reset(); weightManager.pairRows = []; weightManager.gptID = ""; weightManager.sovitsID = ""; weightManager.manualPair = false; blindManager.planner.reset(); blindManager.regenerationPlanner.reset()
        connected = false; started = false; healthFailures = 0
        initialTask = perform { await self.loadInitial() }
    }
    func reloadModels(resetSelection: Bool = true) async throws {
        let result: ModelsResponse = try await api.get("/models")
        models = result.characters
        engines = result.engines
        if resetSelection { applyDefaults() }
    }
    func applyDefaults() {
        if character == nil, let first = models.first { params.character = first.slug }
        if !engines.isEmpty && !engines.contains(where: { $0.id == params.engine }) {
            let preferred = preferredEngine
            params.engine = engines.contains(where: { $0.id == preferred })
                ? preferred
                : (sortedEngines(engines.map(\.id)).first ?? engines[0].id)
        }
        params.gpt = nil
        params.sovits = nil
        params.version = nil
        composerManual = false
        weightManager.character = params.character
        weightManager.pairCharacter = params.character
        if blindManager.character.isEmpty { blindManager.character = params.character }
        applyCharacterPrefs(for: params.character)
        if composerVersions.character == params.character {
            if let version = composerVersions.selected { selectComposerVersion(version.id) }
        } else { composerVersions.reset() }
    }
    func selectCharacter(_ slug: String, applyPrefs: Bool = true) {
        let changed = params.character != slug
        if changed {
            flushPendingRefPrefs()
        }
        params.character = slug
        weightManager.character = slug
        weightManager.pairCharacter = slug
        blindManager.character = slug
        guard changed else { return }
        refs = []
        selectedReferenceID = nil
        resetReferenceAudition()
        params.gpt = nil
        params.sovits = nil
        params.version = nil
        composerManual = false
        composerVersions.reset()
        if applyPrefs { applyCharacterPrefs(for: slug) }
    }
    func applyCharacterPrefs(for slug: String) {
        let prefs = models.first(where: { $0.slug == slug })?.prefs
        let sp = prefs?.synth_params
        params.speed = sp?.speed ?? 1.0
        params.fragment_interval = sp?.fragment_interval ?? 0.3
        params.top_k = sp?.top_k ?? 15
        params.top_p = sp?.top_p ?? 1.0
        params.temperature = sp?.temperature ?? 1.0
        params.repetition_penalty = sp?.repetition_penalty ?? 1.35
        params.text_split_method = sp?.text_split_method ?? "cut5"
        params.sample_steps = sp?.sample_steps
        params.text_lang = sp?.text_lang ?? "ja"
        if let lastRef = prefs?.last_ref_id, !lastRef.isEmpty {
            referenceMode = "参考库"
            params.ref_id = lastRef
        } else {
            referenceMode = "默认"
            params.ref_id = nil
        }
        params.aux_ref_ids = prefs?.aux_ref_ids ?? []
    }
    func loadRefs(character: String, sort: String? = nil, order: String? = nil) async throws {
        let result: RefList = try await api.get("/refs", query: ["character": character, "sort":sort ?? defaults.string(forKey: "refsSort") ?? "id", "order":order ?? defaults.string(forKey: "refsOrder") ?? "asc"])
        try Task.checkCancellation()
        refs = result.refs
        refCountsByCharacter[character] = result.count
        refsByCharacterCache[character] = result.refs
    }
    func fetchRefs(for character: String) async throws -> [Ref] {
        let result: RefList = try await api.get("/refs", query: ["character": character, "sort": "id", "order": "asc"])
        try Task.checkCancellation()
        refCountsByCharacter[character] = result.count
        refsByCharacterCache[character] = result.refs
        return result.refs
    }
    func generate() async throws {
        if let message = composerVersions.message { throw APIError(status: 409, message: message, code: "version_unavailable") }
        guard !generationBusy else { return }
        generationBusy = true
        defer { generationBusy = false }
        var request = params
        request.return_json = true
        if referenceMode == "情绪" { request.ref_id = nil; request.emotion = emotion }
        else { request.emotion = nil; if referenceMode == "默认" { request.ref_id = nil } }
        let caps = effectiveGenerationCapabilities(for: [request.engine])
        request.aux_ref_ids = caps.auxRefIDs
        if !["v3", "v4", "v5turbo", "v5dev"].contains(request.engine) { request.sample_steps = nil }
        candidates = []
        selectedCandidate = nil
        generationJobID = nil
        let job: Job
        do { job = try await generationPlanner.submit(api: api, path: "/tts/candidates/jobs", input: request) }
        catch let failure as APIError where failure.code == "version_unavailable" {
            params.version = nil; composerManual = false
            try await composerVersions.invalidate(api: api, character: request.character)
            throw APIError(status: 409, message: "这个模型版本已不可用，请重新选择。", code: failure.code)
        }
        recordLocalJob(job.id)
        generationJobID = job.id
        let result = try await follow(job)
        candidates = (result.candidates_result?.candidates ?? candidates).map { item in
            var value = item
            value.review = candidates.first(where: { $0.id == item.id })?.review ?? item.review
            return value
        }.sorted { ($0.score ?? -.infinity) > ($1.score ?? -.infinity) }
        if selectedCandidate == nil { selectedCandidate = candidates.first?.id }
        generateRightTab = .candidates
        status = "已生成 \(candidates.count) 个候选"
    }
    func follow(_ job: Job) async throws -> Job {
        let token = api.generation
        let result = try await api.follow(job) { [weak self] update in
            guard let self, self.api.isCurrent(token) else { throw CancellationError() }
            try await self.consumeProgress(update)
        }
        guard api.isCurrent(token) else { throw CancellationError() }
        if localJobIDs.contains(job.id) { status = "任务完成" }
        if ["train", "sweep", "eval"].contains(result.kind) {
            // Metadata refresh is outside the job event stream and cannot change its outcome.
            do {
                if result.kind != "eval" { try await refreshTrainingCatalog() }
                guard api.isCurrent(token) else { throw CancellationError() }
                if models.contains(where: { $0.slug == params.character }) { try await loadComposerVersions() }
                guard api.isCurrent(token) else { throw CancellationError() }
                if models.contains(where: { $0.slug == weightManager.pairCharacter }) { try await weightManager.refreshVersions(studio: self) }
            } catch is CancellationError { throw CancellationError() }
            catch { NSLog("任务已完成，模型列表刷新失败：%@", error.localizedDescription) }
        }
        return result
    }
    func consumeProgress(_ job: Job) async throws {
        updateJob(job)
        // completed is cumulative: a coalesced SSE snapshot may add several items.
        if job.id == generationJobID {
            for item in job.completed where !candidates.contains(where: { $0.id == item.audio_id }) {
                let audio: AudioItem = try await api.get("/audio/\(item.audio_id)/meta")
                candidates.append(audio)
                candidates.sort { ($0.score ?? -.infinity) > ($1.score ?? -.infinity) }
                if selectedCandidate == nil { selectedCandidate = candidates.first?.id }
            }
        } else if work?.id == job.target {
            for item in job.completed {
                guard let lineID = item.line_id,
                      let line = work?.lines.first(where: { $0.id == lineID }),
                      !line.candidates.contains(where: { $0.id == item.audio_id }) else { continue }
                let audio: AudioItem = try await api.get("/audio/\(item.audio_id)/meta")
                if work?.id == job.target, let index = work?.lines.firstIndex(where: { $0.id == lineID }) {
                    work?.lines[index].candidates.append(audio)
                    work?.lines[index].candidates.sort { ($0.score ?? -.infinity) > ($1.score ?? -.infinity) }
                }
            }
        }
    }
    private func scheduleComputeRefresh() {
        guard connected else { return }
        let token = api.generation
        computeRefreshTask?.cancel()
        computeRefreshTask = Task { [weak self] in
            guard let self, self.api.isCurrent(token), !Task.isCancelled else { return }
            await self.compute.refresh(refreshAccounts: false)
        }
    }
    func updateJob(_ job: Job) {
        let previous = jobs.first(where: { $0.id == job.id })
        if job.kind == "train" {
            if let index = trainingJobs.firstIndex(where: { $0.id == job.id }) { trainingJobs[index] = job }
            else { trainingJobs.insert(job, at: 0) }
        }
        if let i = jobs.firstIndex(where: { $0.id == job.id }) { jobs[i] = job }
        else { jobs.insert(job, at: 0) }
        if job.active && localJobIDs.contains(job.id) { status = job.stage }
        let enteredActive = job.active && previous?.active != true
        let statusChangedWhileActive = job.active && previous?.active == true && previous?.status != job.status
        let becameTerminal = !job.active && previous?.active == true
        if enteredActive || statusChangedWhileActive || becameTerminal {
            scheduleComputeRefresh()
        }
    }
    func monitor(_ job: Job) {
        guard monitors[job.id] == nil else { return }
        if job.active { scheduleComputeRefresh() }
        let token = api.generation
        monitors[job.id] = Task {
            defer { if api.isCurrent(token) { monitors[job.id] = nil } }
            do {
                _ = try await follow(job)
            }
            catch is CancellationError { }
            catch { if api.isCurrent(token), localJobIDs.contains(job.id) { errorMessage = error.localizedDescription } }
        }
    }
    var activeMonitorCount: Int { monitors.count }
    func cancel(_ job: Job) async throws {
        let cancelled: Job = try await api.post("/jobs/\(job.id)/cancel")
        updateJob(cancelled)
    }
    func play(_ item: AudioItem) async throws { try await play(path: item.audio_url, id: item.id) }
    func play(path: String, id: String) async throws {
        if audio.currentID == id { audio.toggle(); return }
        let data = try await api.data(api.request(path))
        try Task.checkCancellation()
        try audio.play(id: id, data: data)
        status = "正在播放"
    }
    func resetReferenceAudition() {
        referenceAudition = nil
        if audio.currentID?.hasPrefix("ref-") == true { audio.stop() }
    }
    func auditionReference(_ id: String?, restart: Bool = true) {
        selectedReferenceID = id
        referenceAudition = id.map { ReferenceAudition(referenceID: $0, restart: restart) }
    }
    func moveReference(offset: Int, visibleIDs: [String]) {
        guard !visibleIDs.isEmpty else { return }
        let index = visibleIDs.firstIndex { $0 == selectedReferenceID } ?? (offset > 0 ? -1 : visibleIDs.count)
        let next = min(max(0, index + offset), visibleIDs.count - 1)
        auditionReference(visibleIDs[next])
    }
    func playReference(_ ref: Ref, restart: Bool = true) async throws {
        let id = "ref-" + ref.id
        if !restart && audio.currentID == id { audio.toggle(); return }
        audio.stop()
        let data = try await api.data(api.request(ref.audio_url, query: ["format":"mp3"]))
        try Task.checkCancellation()
        try audio.play(id: id, data: data)
        status = "正在试听参考：\(ref.id)"
    }
    @discardableResult
    func tagAndAdvance(_ tag: String, visibleIDs: [String]) -> Task<Void, Never>? {
        guard let index = visibleIDs.firstIndex(where: { $0 == selectedReferenceID }),
              let ref = refs.first(where: { $0.id == selectedReferenceID }) else { return nil }
        auditionReference(visibleIDs.indices.contains(index + 1) ? visibleIDs[index + 1] : nil)
        return queueTag(ref, tag: tag)
    }
    @discardableResult
    func queueTag(_ ref: Ref, tag: String, remove: Bool = false) -> Task<Void, Never> {
        let previous = tagQueue
        let task = perform {
            await previous?.value
            let current = self.refs.first(where: { $0.id == ref.id }) ?? ref
            let tags = remove ? current.tags.filter { $0 != tag } : Array(Set(current.tags + [tag])).sorted()
            try await self.tag(current, tags: tags)
        }
        tagQueue = task
        return task
    }
    func selectCandidate(offset: Int) {
        guard !candidates.isEmpty else { return }
        let current = candidates.firstIndex { $0.id == selectedCandidate } ?? 0
        selectedCandidate = candidates[min(max(0, current + offset), candidates.count - 1)].id
    }
    private func currentReview(_ item: AudioItem) -> Review? {
        let copies = candidates.filter { $0.id == item.id } + history.items.filter { $0.id == item.id }
            + (work?.lines.flatMap(\.candidates).filter { $0.id == item.id } ?? [])
        if let newest = copies.max(by: { ($0.review?.updated ?? "") < ($1.review?.updated ?? "") }) { return newest.review }
        if let snapshot = reviewSnapshots[item.id] { return snapshot.value }
        return item.review
    }
    private func writeReview(_ item: AudioItem, operation: @escaping @MainActor (Review?) async throws -> Review?) async throws {
        let previous = reviewWrites[item.id]
        let task = Task { @MainActor in
            if let previous { _ = try? await previous.value }
            let result = try await operation(self.currentReview(item))
            self.updateReview(item.id, review: result)
        }
        reviewWrites[item.id] = task
        try await task.value
    }
    private func updateReview(_ id: String, review: Review?) {
        reviewSnapshots[id] = ReviewSnapshot(value: review)
        if let i = candidates.firstIndex(where: { $0.id == id }) { candidates[i].review = review }
        if let i = history.items.firstIndex(where: { $0.id == id }) { history.items[i].review = review }
        if var work {
            for i in work.lines.indices {
                if let j = work.lines[i].candidates.firstIndex(where: { $0.id == id }) { work.lines[i].candidates[j].review = review }
            }
            self.work = work
        }
        reviewRevision += 1
    }
    func review(_ item: AudioItem, rating: Int?, tags: [String]) async throws {
        try await writeReview(item) { current in
            try await self.api.send("/audio/\(item.id)/review", body: ReviewIn(rating: rating, verdict: current?.verdict, tags: tags, note: current?.note ?? ""))
        }
        status = "评价已保存"
    }
    func setRating(_ item: AudioItem, rating: Int) async throws {
        try await writeReview(item) { current in
            try await self.api.send("/audio/\(item.id)/review", body: ReviewIn(rating: rating, verdict: current?.verdict, tags: current?.tags ?? [], note: current?.note ?? ""))
        }
        status = "评价已保存"
    }
    func toggleReviewTag(_ item: AudioItem, tag: String) async throws {
        try await writeReview(item) { current in
            var tags = current?.tags ?? []
            if tags.contains(tag) { tags.removeAll { $0 == tag } } else { tags.append(tag) }
            return try await self.api.send("/audio/\(item.id)/review", body: ReviewIn(rating: current?.rating, verdict: current?.verdict, tags: tags, note: current?.note ?? ""))
        }
        status = "评价已保存"
    }
    func clearReview(_ item: AudioItem) async throws {
        try await writeReview(item) { _ in
            _ = try await self.api.data(self.api.request("/audio/\(item.id)/review", method: "DELETE"))
            return nil
        }
        status = "评价已清除"
    }
    func rateSelected(_ rating: Int) {
        guard let item = currentCandidate else { return }
        perform { try await self.setRating(item, rating: rating) }
    }
    func tag(_ ref: Ref, tags: [String]) async throws {
        let updated: Ref = try await api.send("/refs/\(ref.id)", method: "PUT", body: TagsBody(tags: tags))
        if let i = refs.firstIndex(where: { $0.id == updated.id }) { refs[i] = updated }
        status = "标签已保存：\(tags.joined(separator: "、"))"
    }
    func loadWorks() async throws {
        let result: WorkList = try await api.get("/works", query: ["sort":defaults.string(forKey: "worksSort") ?? "created", "order":defaults.string(forKey: "worksOrder") ?? "desc"])
        try Task.checkCancellation()
        works = result.works
    }
    func openWork(_ id: String) async throws {
        work = try await api.get("/works/\(id)")
        if dirtyRenders.contains(id) { work?.render = nil }
        workDirty = false
    }
    func createWork(title: String) async throws {
        work = try await api.send("/works", body: WorkCreate(title: title))
        workDirty = false
        try await loadWorks()
    }
    func saveWork() async throws {
        guard let work else { return }
        let targetID = work.id
        let editRevision = workEditRevision
        let token = api.generation
        workSaving = true
        defer { workSaving = false }
        let saved: Work = try await api.send("/works/\(work.id)", method: "PATCH", body: WorkPatch(title: work.title, lines: work.lines.map(\.input)))
        guard api.isCurrent(token), self.work?.id == targetID else { return }
        let ids = Dictionary(uniqueKeysWithValues: zip(work.lines, saved.lines).map { ($0.id, $1.id) })
        selectedWorkLineIDs = Set(selectedWorkLineIDs.compactMap { ids[$0] ?? $0 })
        staleLines[work.id] = (staleLines[work.id] ?? []).compactMap { ids[$0] ?? $0 }
        if let oldRows = obsoleteCandidates[work.id] {
            obsoleteCandidates[work.id] = Dictionary(uniqueKeysWithValues: oldRows.map { oldID, audioIDs in
                (ids[oldID] ?? oldID, audioIDs)
            })
            defaults.set(obsoleteCandidates, forKey: "obsoleteWorkCandidates")
        }
        defaults.set(staleLines, forKey: "staleWorkLines")
        if workEditRevision == editRevision {
            self.work = saved
            if dirtyRenders.contains(saved.id) { self.work?.render = nil }
            workDirty = false
        } else if var latest = self.work {
            // Preserve edits made while the save request was in flight; only remap draft IDs.
            for index in latest.lines.indices {
                if let mappedID = ids[latest.lines[index].id] {
                    latest.lines[index].id = mappedID
                }
            }
            self.work = latest
            workDirty = true
        }
        try await loadWorks()
    }
    func deleteWork() async throws {
        guard let work else { return }
        _ = try await api.data(api.request("/works/\(work.id)", method: "DELETE"))
        self.work = nil
        forgetWorkState(work.id)
        try await loadWorks()
    }
    func addLine() {
        let slug = params.character
        let prefs = models.first(where: { $0.slug == slug })?.prefs
        let recommended = characterVersionsCache[slug]?.first(where: \.recommended)
            ?? (composerVersions.character == slug ? composerVersions.selected : nil)
        let useManual = recommended == nil && composerManual
        let engine = recommended?.engine ?? (useManual ? params.engine : preferredEngine)
        let gpt = recommended?.gpt ?? (useManual ? params.gpt : nil)
        let sovits = recommended?.sovits ?? (useManual ? params.sovits : nil)
        let aux = engineSupportsAuxRefs(engine) ? (prefs?.aux_ref_ids ?? []) : []
        work?.lines.append(WorkLine(
            engine: engine,
            gpt: gpt,
            sovits: sovits,
            fragment_interval: prefs?.synth_params.fragment_interval ?? 0.3,
            aux_ref_ids: aux,
            version: recommended?.id,
            id: "draft-\(UUID().uuidString)",
            character: slug,
            text: "",
            lang: prefs?.synth_params.text_lang ?? params.text_lang
        ))
        invalidateRender()
        markWorkEdited()
    }
    func addCandidateToWork(_ item: AudioItem, workID: String?, lineID: String? = nil, position: Int? = nil) async throws {
        let draftPosition = work?.id == workID ? work?.lines.firstIndex(where: { $0.id == lineID }) : nil
        if workDirty { try await saveWork() }
        let targetID: String
        if let workID { targetID = workID }
        else { try await createWork(title: "新作品"); targetID = work!.id }
        let updated: Work
        if let lineID {
            let resolvedID = draftPosition.map { work!.lines[$0].id } ?? lineID
            updated = try await api.send("/works/\(targetID)/lines/\(resolvedID)/candidates", body: AttachRequest(audio_id: item.id))
            // This is an explicit new audio choice, not an implicit reuse after edits.
            clearStaleLines(workID: targetID, lineIDs: [resolvedID])
            obsoleteCandidates[targetID]?[resolvedID]?.removeAll { $0 == item.id }
            defaults.set(obsoleteCandidates, forKey: "obsoleteWorkCandidates")
        } else {
            updated = try await api.send("/works/\(targetID)/lines", body: AddLineRequest(audio_id: item.id, position: position))
        }
        work = updated
        invalidateRender(targetID)
        workDirty = false
        selectedWorkLineIDs = []
        try await loadWorks()
        page = .works
        status = lineID == nil ? "已加入新行并选用此音频" : "已挂到该行并选用此音频"
    }
    func generateWork(lineIDs: [String]?) async throws {
        workBusy = true
        defer { workBusy = false }
        let positions = lineIDs.map { ids in work?.lines.enumerated().filter { ids.contains($0.element.id) }.map(\.offset) ?? [] }
        try await saveWork()
        guard let work else { return }
        let resolvedIDs = positions.map { indices in indices.map { work.lines[$0].id } }
        workPlanner.previewKind = "gen"
        workPlanner.taskSummary = "生成作品（\(computeTaskContent(workPlan(lineIDs: resolvedIDs), title: work.title))）"
        let job: Job = try await workPlanner.submit(api: api, path: "/works/\(work.id)/generate", input: GenerateRequest(line_ids: resolvedIDs, n: 3))
        invalidateRender(work.id)
        recordLocalJob(job.id)
        _ = try await follow(job)
        clearStaleLines(workID: work.id, lineIDs: resolvedIDs ?? work.lines.map(\.id))
        if self.work?.id == work.id { try await openWork(work.id) }
        try await loadWorks()
    }
    func regenerate(_ line: WorkLine) async throws {
        workBusy = true
        defer { workBusy = false }
        try await saveWork()
        guard let work else { return }
        workPlanner.previewKind = "gen"; workPlanner.previewEngine = line.engine
        workPlanner.taskSummary = "重新生成（\(work.title) · 3 句 \(displayEngine(line.engine))）"
        let job: Job = try await workPlanner.submit(api: api, path: "/works/\(work.id)/lines/\(line.id)/regenerate", input: RegenerateRequest(n: 3))
        invalidateRender(work.id)
        recordLocalJob(job.id)
        _ = try await follow(job)
        clearStaleLines(workID: work.id, lineIDs: [line.id])
        if self.work?.id == work.id { try await openWork(work.id) }
    }
    func pick(_ audio: AudioItem, line: WorkLine) async throws {
        guard let work, let currentLine = work.lines.first(where: { $0.id == line.id }) else { return }
        guard canPick(audio, for: currentLine) else {
            throw APIError(status: 0, message: "这条候选来自旧台词或声音设置，只可试听；请选用当前设置的候选。")
        }
        self.work = try await api.send("/works/\(work.id)/lines/\(line.id)/pick", method: "PUT", body: PickRequest(audio_id: audio.id))
        invalidateRender(work.id)
    }
    func renderWork() async throws {
        workBusy = true
        defer { workBusy = false }
        try await saveWork()
        guard let work else { return }
        guard work.lines.allSatisfy({ !needsRegeneration($0) }) else { throw APIError(status: 0, message: "请先为修改过的台词重新生成候选。") }
        let job: Job = try await api.post("/works/\(work.id)/render")
        recordLocalJob(job.id)
        _ = try await follow(job)
        dirtyRenders.remove(work.id)
        defaults.set(Array(dirtyRenders), forKey: "dirtyWorkRenders")
        if self.work?.id == work.id { try await openWork(work.id) }
        try await loadWorks()
    }
    func export(path: String, extension ext: String) async throws {
        let panel = NSSavePanel()
        panel.title = "导出作品"
        panel.nameFieldStringValue = "\(work?.title ?? "作品").\(ext)"
        panel.allowedContentTypes = [UTType(filenameExtension: ext) ?? .data]
        guard await panel.begin() == .OK, let url = panel.url else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try await api.data(api.request(path))
        try data.write(to: url, options: .atomic)
        status = "已导出：\(url.lastPathComponent)"
    }
    func loadDatasets() async throws {
        let list: DatasetList = try await api.get("/datasets")
        datasets = list.datasets
    }
    func loadTrainingJobs() async throws {
        async let catalog: Void = refreshTrainingCatalog()
        let result: JobList = try await api.get("/jobs", query: ["kind":"train", "limit":"500", "sort":defaults.string(forKey: "trainingSort") ?? "created", "order":defaults.string(forKey: "trainingOrder") ?? "desc"])
        try Task.checkCancellation()
        let sweeps: JobList = try await api.get("/jobs", query: ["kind":"sweep", "limit":"500", "sort":defaults.string(forKey: "trainingSort") ?? "created", "order":defaults.string(forKey: "trainingOrder") ?? "desc"])
        var seen: Set<String> = []
        trainingJobs = (result.jobs + sweeps.jobs).filter { seen.insert($0.id).inserted }
        let field = defaults.string(forKey: "trainingSort") ?? "created"
        let ascending = defaults.string(forKey: "trainingOrder") == "asc"
        trainingJobs.sort { a, b in
            if field == "progress" { return ascending ? a.progress < b.progress : a.progress > b.progress }
            func value(_ job: Job) -> String { switch field { case "started": job.started ?? ""; case "finished": job.finished ?? ""; case "kind": job.kind; case "status": job.status; default: job.created } }
            return ascending ? value(a) < value(b) : value(a) > value(b)
        }
        for job in trainingJobs { updateJob(job); if job.active { monitor(job) } }
        try await catalog
    }
    func submitTraining(_ request: TrainRequest, lines: Int? = nil) async throws {
        trainingPlanner.previewKind = "train"; trainingPlanner.previewEngine = request.engine
        trainingPlanner.taskSummary = "训练 \(request.slug) · \(displayEngine(request.engine))" + ((lines ?? request.max_lines).map { "（\($0) 句）" } ?? "")
        let job: Job = try await trainingPlanner.submit(api: api, path: "/train", input: request)
        recordLocalJob(job.id)
        updateJob(job)
        monitor(job)
    }
    func loadEvaluation(_ job: Job) async throws {
        try await refreshTrainingCatalog()
        guard !isDeletedTrainingCharacter(job) else { status = "角色已删除"; return }
        evaluation = try await api.get("/train/\(job.id)/eval")
    }
    func tryCombination(_ combo: ComboEval, evaluation: TrainEval) async throws {
        try await reloadModels(resetSelection: false)
        guard models.contains(where: { $0.slug == evaluation.character }) else { status = "角色已删除"; return }
        if params.character != evaluation.character {
            selectCharacter(evaluation.character, applyPrefs: true)
        }
        useManualComposer()
        params.engine = evaluation.engine
        params.gpt = combo.gpt
        params.sovits = combo.sovits
        params.aux_ref_ids = []
        if !evaluation.ref_id.isEmpty {
            params.ref_id = evaluation.ref_id
            referenceMode = "参考库"
        } else {
            params.ref_id = nil
            referenceMode = "默认"
        }
        generateMode = .candidates
        page = .generate
    }
}

extension StudioStore {
    func loadComposerVersions(preferRecommended: Bool = false) async throws {
        let character = params.character
        try await composerVersions.load(
            api: api,
            character: character,
            chooseDefault: !composerManual && (composerVersions.character != character || composerVersions.message == nil),
            preferRecommended: preferRecommended
        )
        try Task.checkCancellation()
        guard params.character == character else { return }
        characterVersionsCache[character] = composerVersions.rows
        if let pendingID = pendingComposerVersionID {
            pendingComposerVersionID = nil
            if composerVersions.rows.contains(where: { $0.id == pendingID }) {
                selectComposerVersion(pendingID)
                return
            }
        }
        if preferRecommended || !composerManual { selectComposerVersion(composerVersions.selectedID) }
    }
    func loadVersions(for slug: String) async throws -> [ModelVersion] {
        let values: [ModelVersion] = try await api.get("/characters/\(slug)/versions")
        try Task.checkCancellation()
        characterVersionsCache[slug] = values
        return values
    }
    func selectComposerVersion(_ id: String?) {
        composerVersions.selectedID = id
        guard let version = composerVersions.selected else { params.version = nil; return }
        composerVersions.message = nil
        composerManual = false
        params.engine = version.engine; params.gpt = version.gpt; params.sovits = version.sovits; params.version = version.id
    }
    func useManualComposer() { params.version = nil; composerVersions.selectedID = nil; composerVersions.message = nil; composerManual = true }
    var defaultReferenceLabel: String { refs.first { $0.id == character?.default_ref }?.label ?? "默认参考音频" }
    func startPrimaryGeneration() {
        switch generateMode {
        case .candidates:
            startGeneration()
        case .compare:
            let targets = comparisonEnginesList
            guard targets.count >= 2 else { return }
            startComparison(engines: targets)
        }
    }
    func startComparison(engines: [String]) {
        guard composerReady, !generationBusy else { return }
        generationTask = perform { try await self.compare(engines: engines) }
    }
    func compare(engines: [String]) async throws {
        if let message = composerVersions.message { throw APIError(status: 409, message: message, code: "version_unavailable") }
        guard !generationBusy else { return }
        generationBusy = true; defer { generationBusy = false }
        var request = params
        request.return_json = true
        if referenceMode == "情绪" { request.ref_id = nil; request.emotion = emotion }
        else { request.emotion = nil; if referenceMode == "默认" { request.ref_id = nil } }
        let caps = effectiveGenerationCapabilities(for: engines)
        request.aux_ref_ids = caps.auxRefIDs
        var fields = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as! [String: Any]
        fields["engines"] = engines; fields["n"] = params.n
        let input = try JSONDecoder().decode(CompareParams.self, from: JSONSerialization.data(withJSONObject: fields))
        candidates = []; selectedCandidate = nil; generationJobID = nil
        let job: Job
        do { job = try await comparisonPlanner.submit(api: api, path: "/compare/jobs", input: input) }
        catch let failure as APIError where failure.code == "version_unavailable" {
            params.version = nil; composerManual = false
            try await composerVersions.invalidate(api: api, character: input.character)
            throw APIError(status: 409, message: "这个模型版本已不可用，请重新选择。", code: failure.code)
        }
        recordLocalJob(job.id); generationJobID = job.id
        let result = try await follow(job)
        candidates = (result.compare_result?.results.flatMap(\.candidates) ?? candidates).sorted { ($0.score ?? -.infinity) > ($1.score ?? -.infinity) }
        selectedCandidate = candidates.first?.id
        generateRightTab = .candidates
        status = "对比完成"
    }
}

extension StudioStore {
    // MARK: - Character preferences & reference selection

    var currentSynthParamsState: SynthParamsState {
        let saved = character?.prefs?.synth_params
        let baseSpeed = saved?.speed ?? 1.0
        let baseInterval = saved?.fragment_interval ?? 0.3
        let baseTopK = saved?.top_k ?? 15
        let baseTopP = saved?.top_p ?? 1.0
        let baseTemp = saved?.temperature ?? 1.0
        let baseRep = saved?.repetition_penalty ?? 1.35
        let baseSplit = saved?.text_split_method ?? "cut5"
        let baseSteps = saved?.sample_steps
        let baseLang = saved?.text_lang ?? "ja"

        let matchesBase =
            abs(params.speed - baseSpeed) < 0.001 &&
            abs(params.fragment_interval - baseInterval) < 0.001 &&
            params.top_k == baseTopK &&
            abs(params.top_p - baseTopP) < 0.001 &&
            abs(params.temperature - baseTemp) < 0.001 &&
            abs(params.repetition_penalty - baseRep) < 0.001 &&
            params.text_split_method == baseSplit &&
            params.sample_steps == baseSteps &&
            params.text_lang == baseLang

        if !matchesBase { return .modified }
        if let saved, !saved.isEmpty { return .saved }
        return .default
    }

    func currentCharacterSynthParams() -> CharacterSynthParams {
        CharacterSynthParams(
            speed: params.speed,
            fragment_interval: params.fragment_interval,
            top_k: params.top_k,
            top_p: params.top_p,
            temperature: params.temperature,
            repetition_penalty: params.repetition_penalty,
            text_split_method: params.text_split_method,
            sample_steps: currentEngineSupportsSampleSteps ? params.sample_steps : nil,
            text_lang: params.text_lang
        )
    }

    func applyUpdatedPrefs(_ prefs: CharacterPrefs) {
        if let index = models.firstIndex(where: { $0.slug == prefs.slug }) {
            models[index].prefs = prefs
            if let explicitDefault = prefs.default_ref {
                models[index].default_ref = explicitDefault
            }
        }
    }

    func saveCharacterSynthParams() async throws {
        let targetSlug = params.character
        guard !targetSlug.isEmpty else { return }
        let token = api.generation
        let snapshot = currentCharacterSynthParams()
        savingPrefs = true
        defer { savingPrefs = false }
        let patch = CharacterPrefsPatch(
            synth_params: .some(snapshot),
            reset_synth_params: true
        )
        let updated: CharacterPrefs = try await api.send("/characters/\(targetSlug)/prefs", method: "PATCH", body: patch)
        guard api.isCurrent(token) else { return }
        applyUpdatedPrefs(updated)
        if params.character == targetSlug {
            status = "已存为 \(characterName(targetSlug)) 的默认参数"
        }
    }

    func resetCharacterSynthParams() async throws {
        let targetSlug = params.character
        guard !targetSlug.isEmpty else { return }
        let token = api.generation
        savingPrefs = true
        defer { savingPrefs = false }
        let patch = CharacterPrefsPatch(reset_synth_params: true)
        let updated: CharacterPrefs = try await api.send("/characters/\(targetSlug)/prefs", method: "PATCH", body: patch)
        guard api.isCurrent(token) else { return }
        applyUpdatedPrefs(updated)
        guard params.character == targetSlug else { return }
        params.speed = 1.0
        params.fragment_interval = 0.3
        params.top_k = 15
        params.top_p = 1.0
        params.temperature = 1.0
        params.repetition_penalty = 1.35
        params.text_split_method = "cut5"
        params.sample_steps = nil
        params.text_lang = "ja"
        status = "已恢复默认参数"
    }

    func setCharacterDefaultRef(_ refID: String?, for slug: String? = nil) async throws {
        let target = slug ?? params.character
        guard !target.isEmpty else { return }
        let token = api.generation
        let patch = CharacterPrefsPatch(default_ref: .some(refID))
        let updated: CharacterPrefs = try await api.send("/characters/\(target)/prefs", method: "PATCH", body: patch)
        guard api.isCurrent(token) else { return }
        applyUpdatedPrefs(updated)
        if refID == nil {
            try? await reloadModels(resetSelection: false)
        }
        if target == params.character || refs.first?.character == target {
            try? await loadRefs(character: target)
        }
        status = refID == nil ? "已恢复自动默认参考" : "已设为默认参考"
    }

    func persistRefPrefsNow(slug: String? = nil) async throws {
        let target = slug ?? params.character
        guard !target.isEmpty else { return }
        let token = api.generation
        let lastRef: String? = (referenceMode == "参考库") ? params.ref_id : nil
        let auxRefs = params.aux_ref_ids
        if pendingRefPrefs?.slug == target {
            pendingRefPrefs = nil
            refPrefsSaveTask?.cancel()
            refPrefsSaveTask = nil
        }
        try await sendRefPrefsPatch(slug: target, lastRefID: lastRef, auxRefIDs: auxRefs, token: token)
    }

    private func sendRefPrefsPatch(slug: String, lastRefID: String?, auxRefIDs: [String], token: Int) async throws {
        guard api.isCurrent(token) else { return }
        let patch = CharacterPrefsPatch(last_ref_id: .some(lastRefID), aux_ref_ids: auxRefIDs)
        let updated: CharacterPrefs = try await api.send("/characters/\(slug)/prefs", method: "PATCH", body: patch)
        guard api.isCurrent(token) else { return }
        applyUpdatedPrefs(updated)
    }

    func scheduleRefPrefsSave(delayMs: Int = 350) {
        let slug = params.character
        guard !slug.isEmpty else { return }
        let snapshot = PendingRefPrefs(
            slug: slug,
            lastRefID: (referenceMode == "参考库") ? params.ref_id : nil,
            auxRefIDs: params.aux_ref_ids,
            generation: api.generation
        )
        pendingRefPrefs = snapshot
        refPrefsSaveTask?.cancel()
        refPrefsSaveTask = Task {
            do {
                try await Task.sleep(for: .milliseconds(delayMs))
                try Task.checkCancellation()
            } catch { return }
            guard let pending = self.pendingRefPrefs,
                  pending.slug == snapshot.slug,
                  pending.generation == snapshot.generation else { return }
            self.pendingRefPrefs = nil
            self.refPrefsSaveTask = nil
            // Detach the in-flight network PATCH from the debounce timer so character switches cannot cancel it.
            Task {
                do {
                    try await self.sendRefPrefsPatch(
                        slug: pending.slug,
                        lastRefID: pending.lastRefID,
                        auxRefIDs: pending.auxRefIDs,
                        token: pending.generation
                    )
                } catch is CancellationError { }
                catch {
                    // Non-fatal background preference sync (e.g. read-only library).
                    NSLog("角色参考偏好保存跳过：%@", error.localizedDescription)
                }
            }
        }
    }

    @discardableResult
    func flushPendingRefPrefs() -> Task<Void, Never>? {
        refPrefsSaveTask?.cancel()
        refPrefsSaveTask = nil
        guard let pending = pendingRefPrefs else { return nil }
        pendingRefPrefs = nil
        guard api.isCurrent(pending.generation) else { return nil }
        return Task {
            do {
                try await self.sendRefPrefsPatch(
                    slug: pending.slug,
                    lastRefID: pending.lastRefID,
                    auxRefIDs: pending.auxRefIDs,
                    token: pending.generation
                )
            } catch is CancellationError { }
            catch {
                NSLog("角色参考偏好即时保存跳过：%@", error.localizedDescription)
            }
        }
    }

    func flushPendingRefPrefsNow() async {
        await flushPendingRefPrefs()?.value
    }

    func selectPrimaryRef(_ refID: String?) {
        if let refID, !refID.isEmpty {
            referenceMode = "参考库"
            params.ref_id = refID
            params.aux_ref_ids.removeAll { $0 == refID }
        } else {
            referenceMode = "默认"
            params.ref_id = nil
        }
        scheduleRefPrefsSave()
    }

    func toggleAuxRef(_ refID: String) {
        guard !refID.isEmpty else { return }
        if params.aux_ref_ids.contains(refID) {
            params.aux_ref_ids.removeAll { $0 == refID }
        } else if refID != (params.ref_id ?? character?.default_ref) {
            params.aux_ref_ids.append(refID)
        }
        scheduleRefPrefsSave()
    }

    // MARK: - Cross-workspace navigation & history reuse

    func openCharacter(_ slug: String, tab: CharacterTab = .versions) {
        if !slug.isEmpty { selectCharacter(slug) }
        characterTab = tab
        page = .characters
    }

    func synthesizeWith(character slug: String, versionID: String? = nil, refID: String? = nil) {
        if !slug.isEmpty { selectCharacter(slug) }
        if let versionID {
            pendingComposerVersionID = versionID
            if composerVersions.character == params.character && composerVersions.rows.contains(where: { $0.id == versionID }) {
                pendingComposerVersionID = nil
                selectComposerVersion(versionID)
            }
        }
        if let refID, !refID.isEmpty {
            selectPrimaryRef(refID)
        }
        generateMode = .candidates
        page = .generate
    }

    func startBlindPK(character slug: String, versionA: String? = nil, versionB: String? = nil, comboA: BlindSide? = nil, comboB: BlindSide? = nil) {
        if !slug.isEmpty { selectCharacter(slug) }
        pendingBlindPrefill = BlindPrefill(character: slug, versionA: versionA, versionB: versionB, comboA: comboA, comboB: comboB)
        characterTab = .blind
        page = .characters
    }

    func reuseHistoryItem(_ item: AudioItem) {
        if params.character != item.character {
            selectCharacter(item.character, applyPrefs: false)
        }
        params.text = item.text
        if let lang = item.text_lang, !lang.isEmpty { params.text_lang = lang }
        params.speed = item.speed ?? 1.0
        params.fragment_interval = item.fragment_interval ?? 0.3
        params.temperature = item.temperature ?? 1.0
        params.repetition_penalty = item.repetition_penalty ?? 1.35
        params.top_k = item.top_k ?? 15
        params.top_p = item.top_p ?? 1.0
        params.text_split_method = item.text_split_method ?? "cut5"
        params.sample_steps = item.sample_steps
        params.seed = item.seed
        if !item.ref_id.isEmpty {
            referenceMode = "参考库"
            params.ref_id = item.ref_id
        } else {
            referenceMode = "默认"
            params.ref_id = nil
        }
        params.aux_ref_ids = item.aux_ref_ids

        let matchingVersion = (characterVersionsCache[item.character] ?? composerVersions.rows).first {
            $0.engine == item.engine && $0.gpt == item.gpt && $0.sovits == item.sovits
        }
        if let matchingVersion {
            pendingComposerVersionID = matchingVersion.id
            if composerVersions.character == item.character {
                pendingComposerVersionID = nil
                selectComposerVersion(matchingVersion.id)
            } else {
                params.engine = item.engine
                params.gpt = item.gpt
                params.sovits = item.sovits
                params.version = matchingVersion.id
                composerManual = false
            }
        } else {
            pendingComposerVersionID = nil
            useManualComposer()
            params.engine = item.engine
            params.gpt = item.gpt
            params.sovits = item.sovits
        }
        generateMode = .candidates
        page = .generate
        status = "已复用参数与种子（#\(item.seed)）"
    }

    func exportAudio(_ item: AudioItem, format: String) async throws {
        let panel = NSSavePanel()
        panel.title = "导出音频"
        let prefix = "\(item.character)-\(item.engine)-\(item.seed)"
        panel.nameFieldStringValue = "\(prefix).\(format)"
        panel.allowedContentTypes = [UTType(filenameExtension: format) ?? .audio]
        guard await panel.begin() == .OK, let url = panel.url else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let data = try await api.data(api.request(item.audio_url, query: ["format": format]))
        try data.write(to: url, options: .atomic)
        status = "已导出：\(url.lastPathComponent)"
    }

    func setDefaultVersion(character slug: String, engine: String, gpt: String, sovits: String) async throws {
        guard !settingDefaultVersion else { return }
        let token = api.generation
        settingDefaultVersion = true
        defer { settingDefaultVersion = false }

        let weights = try await visibleWeights(character: slug, engine: engine)
        guard api.isCurrent(token) else { return }
        guard let gptEntry = weights.first(where: { $0.type == "gpt" && $0.name == gpt }),
              let sovitsEntry = weights.first(where: { $0.type == "sovits" && $0.name == sovits }) else {
            throw APIError(status: 404, message: "找不到该版本对应的权重文件，无法设为默认。")
        }

        let _: WeightEntry = try await api.send("/weights/\(gptEntry.id)", method: "PATCH", body: WeightPatch(is_default: true, character: slug))
        do {
            guard api.isCurrent(token) else { return }
            let _: WeightEntry = try await api.send("/weights/\(sovitsEntry.id)", method: "PATCH", body: WeightPatch(is_default: true, character: slug))
        } catch {
            if api.isCurrent(token) { try? await reloadModels(resetSelection: false) }
            throw APIError(status: 0, message: "GPT 默认已更新，SoVITS 设置未完成：\(error.localizedDescription)")
        }
        guard api.isCurrent(token) else { return }
        try await reloadModels(resetSelection: false)
        guard api.isCurrent(token) else { return }
        if params.character == slug { try? await loadComposerVersions(preferRecommended: true) }
        if weightManager.pairCharacter == slug { try? await weightManager.refreshVersions(studio: self, preferRecommended: true) }
        try? await weightManager.load()
        status = "已设为 \(characterName(slug)) 的默认声音"
    }
}

extension StudioStore {
    /// Computes the work plan across all lines (matching "生成全部").
    var workPickerInput: PlanInput { workPlan(lineIDs: nil) }
    var workPickerAction: String { "生成全部 \(work?.lines.count ?? 0) 行" }
    func workPlan(lineIDs: [String]?) -> PlanInput {
        let lines = (work?.lines ?? []).filter { (lineIDs == nil || lineIDs!.contains($0.id)) && !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let counts = Dictionary(grouping: lines, by: \.engine).mapValues { $0.count * 3 }
        let auxRefs = Array(Set(lines.flatMap { line in
            normalizedAuxRefIDs(line.aux_ref_ids, primaryRefID: line.ref_id, engineSupportsAuxRefs: engineSupportsAuxRefs(line.engine))
        })).sorted()
        let customInterval = lines.first(where: { abs($0.fragment_interval - 0.3) > 1e-9 })?.fragment_interval
        return PlanInput(
            kind: "gen",
            parts: counts.keys.sorted().map { PlanPart(engine: $0, items: counts[$0]!) },
            items: counts.values.reduce(0, +),
            aux_ref_ids: auxRefs.isEmpty ? nil : auxRefs,
            fragment_interval: customInterval,
            cloud_capable: false
        )
    }
}
