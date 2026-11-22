import XCTest
@testable import TTSStudio

private final class Stage4Fixture: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [URLRequest] = []
    let handler: @Sendable (URLRequest, Data?) -> (Int, Data)

    init(handler: @escaping @Sendable (URLRequest, Data?) -> (Int, Data)) {
        self.handler = handler
    }

    func respond(_ request: URLRequest) -> (Int, Data) {
        var copy = request
        if copy.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var body = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(contentsOf: buffer.prefix(count))
            }
            copy.httpBody = body
        }
        lock.lock()
        recorded.append(copy)
        lock.unlock()
        return handler(copy, copy.httpBody)
    }

    var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }
}

private final class Stage4URLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var fixture: Stage4Fixture!

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let fixture = Self.fixture!
        let req = request
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            let (status, data) = fixture.respond(req)
            guard let url = req.url, let response = HTTPURLResponse(
                url: url,
                statusCode: status,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            ) else { return }
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: data)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}
}

final class Stage4RefactorTests: XCTestCase {
    @MainActor
    private func makeStore(fixture: Stage4Fixture, defaults: UserDefaults = MemoryDefaults()) -> StudioStore {
        Stage4URLProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Stage4URLProtocol.self]
        let store = StudioStore(defaults: defaults)
        store.api = APIClient(baseURL: "http://stage4-fixture", session: URLSession(configuration: config))
        return store
    }

    private func sampleEngines() -> [EngineInfo] {
        [
            EngineInfo(
                id: "v2pro", version: "v2Pro", code: "fork", code_path: "",
                zero_shot: true, trainable: true, train_method: "full",
                pretrained_gpt: "g2.ckpt", pretrained_sovits: "s2.pth", has_july_weights: true,
                supports_aux_refs: true,
                train_defaults: TrainDefaults(
                    sovits_epochs: 8, sovits_save_every: 4, sovits_batch: 8,
                    gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8,
                    lora: false, lora_rank: nil, lora_ranks: []
                )
            ),
            EngineInfo(
                id: "v4", version: "v4", code: "fork", code_path: "",
                zero_shot: true, trainable: true, train_method: "lora",
                pretrained_gpt: "g4.ckpt", pretrained_sovits: "s4.pth", has_july_weights: false,
                supports_aux_refs: false,
                train_defaults: TrainDefaults(
                    sovits_epochs: 2, sovits_save_every: 1, sovits_batch: 2,
                    gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8,
                    lora: true, lora_rank: 32, lora_ranks: [16, 32, 64, 128]
                )
            ),
            EngineInfo(
                id: "v5turbo", version: "v5turbo", code: "v5", code_path: "",
                zero_shot: true, trainable: true, train_method: "lora",
                pretrained_gpt: "g5.ckpt", pretrained_sovits: "s5.pth", has_july_weights: false,
                supports_aux_refs: false,
                train_defaults: TrainDefaults(
                    sovits_epochs: 4, sovits_save_every: 2, sovits_batch: 4,
                    gpt_epochs: 12, gpt_save_every: 4, gpt_batch: 6,
                    lora: true, lora_rank: 64, lora_ranks: [16, 32, 64, 128]
                )
            ),
        ]
    }

    func testStudioPagesCollapsedToFourWorkspaces() {
        XCTAssertEqual(StudioPage.allCases, [.generate, .works, .characters, .training])
        XCTAssertEqual(StudioPage.allCases.map(\.rawValue), ["合成", "作品", "角色", "训练"])
        XCTAssertEqual(CharacterTab.allCases, [.versions, .references, .blind, .stats])
    }

    func testCharacterPrefsDecodingAndPatchEncodingSemantics() throws {
        let json = """
        {
          "characters": [
            {
              "slug": "alice",
              "source": "builtin",
              "dataset_id": "ds-1",
              "default_ref": "ref-1",
              "prefs": {
                "slug": "alice",
                "default_ref": "ref-1",
                "synth_params": {"speed": 1.15, "fragment_interval": 0.45, "top_k": 20},
                "last_ref_id": "ref-2",
                "aux_ref_ids": ["ref-3", "ref-4"]
              },
              "engines": {}
            }
          ],
          "engines": [
            {
              "id": "v4",
              "version": "v4",
              "code": "fork",
              "code_path": "",
              "zero_shot": true,
              "trainable": true,
              "train_method": "lora",
              "pretrained_gpt": "g",
              "pretrained_sovits": "s",
              "has_july_weights": false,
              "supports_aux_refs": false,
              "train_defaults": {
                "sovits_epochs": 2,
                "sovits_save_every": 1,
                "sovits_batch": 2,
                "gpt_epochs": 15,
                "gpt_save_every": 5,
                "gpt_batch": 8,
                "lora": true,
                "lora_rank": 32,
                "lora_ranks": [16, 32, 64, 128]
              }
            }
          ]
        }
        """
        let decoded = try JSONDecoder().decode(ModelsResponse.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.characters.first?.prefs?.last_ref_id, "ref-2")
        XCTAssertEqual(decoded.characters.first?.prefs?.aux_ref_ids, ["ref-3", "ref-4"])
        XCTAssertEqual(decoded.characters.first?.prefs?.synth_params.fragment_interval, 0.45)
        XCTAssertEqual(decoded.engines.first?.supports_aux_refs, false)
        XCTAssertEqual(decoded.engines.first?.train_defaults?.lora_rank, 32)
        XCTAssertEqual(decoded.engines.first?.train_defaults?.lora_ranks, [16, 32, 64, 128])

        // Patch encoding omits nil outer fields and encodes explicit null for inner nil.
        let clearLastRef = CharacterPrefsPatch(last_ref_id: .some(nil), aux_ref_ids: ["ref-9"])
        let encoded = try JSONEncoder().encode(clearLastRef)
        let obj = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertNil(obj["default_ref"])
        XCTAssertNil(obj["synth_params"])
        XCTAssertNil(obj["reset_synth_params"])
        XCTAssertTrue(obj["last_ref_id"] is NSNull)
        XCTAssertEqual(obj["aux_ref_ids"] as? [String], ["ref-9"])
    }

    @MainActor
    func testCharacterSwitchAppliesPrefsAndSaveResetPersistence() async throws {
        let fixture = Stage4Fixture { request, body in
            if request.httpMethod == "PATCH", request.url?.path == "/characters/alice/prefs",
               let body, let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                if obj["reset_synth_params"] as? Bool == true && obj["synth_params"] == nil {
                    let prefs = CharacterPrefs(slug: "alice", default_ref: "r-def", synth_params: CharacterSynthParams(), last_ref_id: "r-last", aux_ref_ids: ["r-aux"])
                    return (200, try! JSONEncoder().encode(prefs))
                }
                let sp = (obj["synth_params"] as? [String: Any]).flatMap { try? JSONSerialization.data(withJSONObject: $0) }
                    .flatMap { try? JSONDecoder().decode(CharacterSynthParams.self, from: $0) } ?? CharacterSynthParams()
                let prefs = CharacterPrefs(slug: "alice", default_ref: "r-def", synth_params: sp, last_ref_id: "r-last", aux_ref_ids: ["r-aux"])
                return (200, try! JSONEncoder().encode(prefs))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.engines = sampleEngines()
        store.models = [
            CharacterInfo(
                slug: "alice", source: "builtin", default_ref: "r-def",
                prefs: CharacterPrefs(
                    slug: "alice",
                    default_ref: "r-def",
                    synth_params: CharacterSynthParams(speed: 1.2, fragment_interval: 0.5, top_k: 25, top_p: 0.9, temperature: 0.85, repetition_penalty: 1.4, text_split_method: "cut2", text_lang: "ja"),
                    last_ref_id: "r-last",
                    aux_ref_ids: ["r-aux"]
                ),
                engines: [:]
            ),
            CharacterInfo(slug: "bob", source: "builtin", default_ref: "k-def", prefs: nil, engines: [:])
        ]

        store.selectCharacter("bob")
        XCTAssertEqual(store.params.character, "bob")
        XCTAssertEqual(store.params.speed, 1.0)
        XCTAssertEqual(store.params.fragment_interval, 0.3)
        XCTAssertEqual(store.referenceMode, "默认")
        XCTAssertNil(store.params.ref_id)
        XCTAssertEqual(store.currentSynthParamsState, .default)

        store.selectCharacter("alice")
        XCTAssertEqual(store.params.speed, 1.2)
        XCTAssertEqual(store.params.fragment_interval, 0.5)
        XCTAssertEqual(store.params.top_k, 25)
        XCTAssertEqual(store.referenceMode, "参考库")
        XCTAssertEqual(store.params.ref_id, "r-last")
        XCTAssertEqual(store.params.aux_ref_ids, ["r-aux"])
        XCTAssertEqual(store.currentSynthParamsState, .saved)

        store.params.temperature = 0.7
        XCTAssertEqual(store.currentSynthParamsState, .modified)

        try await store.saveCharacterSynthParams()
        XCTAssertEqual(store.currentSynthParamsState, .saved)
        XCTAssertEqual(store.models.first(where: { $0.slug == "alice" })?.prefs?.synth_params.temperature, 0.7)

        try await store.resetCharacterSynthParams()
        XCTAssertEqual(store.currentSynthParamsState, .default)
        XCTAssertEqual(store.params.speed, 1.0)
        XCTAssertEqual(store.params.fragment_interval, 0.3)
        XCTAssertEqual(store.params.temperature, 1.0)
    }

    @MainActor
    func testPrimaryAndAuxRefSelectionAndPersistence() async throws {
        let fixture = Stage4Fixture { request, body in
            if request.httpMethod == "PATCH", request.url?.path == "/characters/alice/prefs",
               let body, let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                let prefs = CharacterPrefs(
                    slug: "alice",
                    default_ref: obj["default_ref"] as? String ?? "r1",
                    synth_params: CharacterSynthParams(),
                    last_ref_id: obj["last_ref_id"] as? String,
                    aux_ref_ids: obj["aux_ref_ids"] as? [String] ?? []
                )
                return (200, try! JSONEncoder().encode(prefs))
            }
            if request.url?.path == "/refs" {
                return (200, Data(#"{"count":0,"refs":[]}"#.utf8))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:])]
        store.params.character = "alice"

        store.selectPrimaryRef("r2")
        XCTAssertEqual(store.referenceMode, "参考库")
        XCTAssertEqual(store.params.ref_id, "r2")

        // Attempting to add primary ref as aux ref is ignored.
        store.toggleAuxRef("r2")
        XCTAssertEqual(store.params.aux_ref_ids, [])

        store.toggleAuxRef("r3")
        store.toggleAuxRef("r4")
        XCTAssertEqual(store.params.aux_ref_ids, ["r3", "r4"])

        // Switching primary to r3 removes r3 from aux refs automatically.
        store.selectPrimaryRef("r3")
        XCTAssertEqual(store.params.ref_id, "r3")
        XCTAssertEqual(store.params.aux_ref_ids, ["r4"])

        try await store.persistRefPrefsNow()
        let patchReq = try XCTUnwrap(fixture.requests.last { $0.httpMethod == "PATCH" })
        let patchJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(patchReq.httpBody)) as? [String: Any])
        XCTAssertEqual(patchJSON["last_ref_id"] as? String, "r3")
        XCTAssertEqual(patchJSON["aux_ref_ids"] as? [String], ["r4"])

        try await store.setCharacterDefaultRef("r3", for: "alice")
        XCTAssertEqual(store.character?.default_ref, "r3")
    }

    @MainActor
    func testAuxRefCapabilityGatingInGenerateCompareAndPlan() async throws {
        let fixture = Stage4Fixture { request, _ in
            if request.httpMethod == "POST", request.url?.path == "/tts/candidates/jobs" {
                let job = Job(id: "j-cand", kind: "candidates", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: "", candidates_result: CandidatesResult(engine: "v4", ref_id: "r1", ref_text: "", elapsed_sec: 1, candidates: []))
                return (200, try! JSONEncoder().encode(job))
            }
            if request.httpMethod == "POST", request.url?.path == "/compare/jobs" {
                let job = Job(id: "j-cmp", kind: "compare", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: "", compare_result: CompareResult(text: "hi", ref_id: "r1", ref_text: "", base_seed: 1, elapsed_sec: 1, results: []))
                return (200, try! JSONEncoder().encode(job))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.engines = sampleEngines()
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:])]
        store.params.character = "alice"
        store.params.aux_ref_ids = ["r-aux-1", "r-aux-2"]
        store.params.fragment_interval = 0.65
        store.useManualComposer()

        // v2pro supports aux refs -> PlanInput includes aux_ref_ids and custom fragment_interval.
        store.params.engine = "v2pro"
        XCTAssertTrue(store.currentEngineSupportsAuxRefs)
        XCTAssertEqual(store.generationPlanInput.aux_ref_ids, ["r-aux-1", "r-aux-2"])
        XCTAssertEqual(store.generationPlanInput.fragment_interval, 0.65)

        // v4 does not support aux refs -> stripped in plan and generate request.
        store.params.engine = "v4"
        XCTAssertFalse(store.currentEngineSupportsAuxRefs)
        XCTAssertNil(store.generationPlanInput.aux_ref_ids)
        try await store.generate()
        let genReq = try XCTUnwrap(fixture.requests.last { $0.url?.path == "/tts/candidates/jobs" })
        let genBody = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(genReq.httpBody)) as? [String: Any])
        XCTAssertEqual(genBody["aux_ref_ids"] as? [String], [])
        XCTAssertEqual(genBody["fragment_interval"] as? Double, 0.65)

        // Compare combining v2pro + v4 strips aux_ref_ids because v4 does not support them.
        store.params.engine = "v2pro"
        try await store.compare(engines: ["v2pro", "v4"])
        let cmpReq = try XCTUnwrap(fixture.requests.last { $0.url?.path == "/compare/jobs" })
        let cmpBody = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(cmpReq.httpBody)) as? [String: Any])
        XCTAssertEqual(cmpBody["aux_ref_ids"] as? [String], [])
    }

    @MainActor
    func testHistoryReuseRestoresAllParametersAndSeed() {
        let store = StudioStore(defaults: MemoryDefaults())
        store.engines = sampleEngines()
        store.models = [
            CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:]),
            CharacterInfo(slug: "bob", source: "builtin", default_ref: "k1", engines: [:])
        ]
        let version = ModelVersion(
            id: "v2pro/g-bob.ckpt/s-bob.pth", label: "11 月训练",
            engine: "v2pro", gpt: "g-bob.ckpt", sovits: "s-bob.pth",
            score: 91, score_set: "hard", recommended: true, source_run: "run-1", created: "2026-11-20"
        )
        store.characterVersionsCache["bob"] = [version]

        let historyItem = AudioItem(
            id: "a-hist", created: "2026-11-20", audio_url: "/audio/a-hist",
            text: "こんにちは、世界。", text_lang: "ja", character: "bob",
            engine: "v2pro", gpt: "g-bob.ckpt", sovits: "s-bob.pth",
            ref_id: "k-ref-2", seed: 778899,
            top_k: 22, top_p: 0.88, temperature: 0.72, repetition_penalty: 1.45,
            speed: 1.1, fragment_interval: 0.55, text_split_method: "cut3", sample_steps: nil,
            aux_ref_ids: ["k-aux-1"],
            duration: 3.4, infer_sec: 1.2, synth_wall_sec: 1.5, semantic_tokens: 90,
            vram_peak_mb: 1200, ratio_vs_ref: [1.0]
        )

        store.page = .characters
        store.reuseHistoryItem(historyItem)
        XCTAssertEqual(store.page, .generate)
        XCTAssertEqual(store.params.character, "bob")
        XCTAssertEqual(store.params.text, "こんにちは、世界。")
        XCTAssertEqual(store.params.seed, 778899)
        XCTAssertEqual(store.params.top_k, 22)
        XCTAssertEqual(store.params.top_p, 0.88)
        XCTAssertEqual(store.params.temperature, 0.72)
        XCTAssertEqual(store.params.repetition_penalty, 1.45)
        XCTAssertEqual(store.params.speed, 1.1)
        XCTAssertEqual(store.params.fragment_interval, 0.55)
        XCTAssertEqual(store.params.text_split_method, "cut3")
        XCTAssertEqual(store.params.ref_id, "k-ref-2")
        XCTAssertEqual(store.referenceMode, "参考库")
        XCTAssertEqual(store.params.aux_ref_ids, ["k-aux-1"])
        XCTAssertEqual(store.params.version, version.id)
        XCTAssertFalse(store.composerManual)

        // Unmatched weights fall back to manual mode.
        var customItem = historyItem
        customItem.gpt = "unmatched-g.ckpt"
        store.reuseHistoryItem(customItem)
        XCTAssertTrue(store.composerManual)
        XCTAssertNil(store.params.version)
        XCTAssertEqual(store.params.gpt, "unmatched-g.ckpt")
    }

    func testTrainDefaultsAutoLinkageAndBlockingReasons() {
        let engines = sampleEngines()
        var request = TrainRequest(slug: "", dataset_id: "")

        // Switch to LoRA engine v4.
        applyTrainDefaults(for: engines[1], to: &request)
        XCTAssertEqual(request.engine, "v4")
        XCTAssertEqual(request.sovits_epochs, 2)
        XCTAssertEqual(request.sovits_save_every, 1)
        XCTAssertEqual(request.sovits_batch, 2)
        XCTAssertEqual(request.gpt_epochs, 15)
        XCTAssertEqual(request.lora_rank, 32)

        // Switch to v5turbo.
        applyTrainDefaults(for: engines[2], to: &request)
        XCTAssertEqual(request.sovits_epochs, 4)
        XCTAssertEqual(request.sovits_batch, 4)
        XCTAssertEqual(request.lora_rank, 64)

        // Switch back to full-finetune v2pro clears lora_rank.
        applyTrainDefaults(for: engines[0], to: &request)
        XCTAssertEqual(request.sovits_epochs, 8)
        XCTAssertEqual(request.sovits_batch, 8)
        XCTAssertNil(request.lora_rank)

        // Blocking reasons should explain every invalid state.
        let validPreview = DatasetPreview(id: "ds-1", lines_total: 100, lines_kept: 80, minutes_kept: 10, excluded_short: 5, excluded_long: 5, excluded_interjection: 10, interjection_examples: [])
        XCTAssertEqual(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: nil, engines: engines), "请先选择数据集")

        request.dataset_id = "ds-1"
        request.min_sec = 10; request.max_sec = 5
        XCTAssertEqual(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: validPreview, engines: engines), "最短时长不能大于最长时长")

        request.min_sec = 1; request.max_sec = 12
        XCTAssertEqual(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: validPreview, engines: engines), "请选择已有角色或输入新角色 slug")

        request.slug = "INVALID_SLUG!"
        XCTAssertNotNil(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: validPreview, engines: engines))

        request.slug = "my-voice-1"
        request.sovits_epochs = 2; request.sovits_save_every = 4
        XCTAssertEqual(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: validPreview, engines: engines), "训练轮数不能小于保存间隔")

        request.sovits_epochs = 8
        XCTAssertNil(trainingBlockReason(request: request, submitting: false, previewLoading: false, preview: validPreview, engines: engines))

        // Sweep blocking reasons.
        var sweep = SweepRequest()
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "请先选择至少一个角色")
        sweep.characters = ["alice"]
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "请先选择至少一个训练引擎")
        sweep.engines = ["v4"]
        sweep.keep = 1; sweep.samples = 3
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "生成样本的组合数不能大于保留组合数")
        sweep.samples = 1
        XCTAssertNil(sweepBlockReason(request: sweep, submitting: false, allCompleted: false))
    }

    @MainActor
    func testCrossWorkspaceNavigationAndBlindPKPrefill() {
        let store = StudioStore(defaults: MemoryDefaults())
        store.models = [
            CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:]),
            CharacterInfo(slug: "bob", source: "builtin", default_ref: "k1", engines: [:])
        ]

        store.openCharacter("bob", tab: .references)
        XCTAssertEqual(store.page, .characters)
        XCTAssertEqual(store.characterTab, .references)
        XCTAssertEqual(store.params.character, "bob")

        store.startBlindPK(character: "bob", versionA: "v2pro/g1/s1", versionB: "v4/g2/s2")
        XCTAssertEqual(store.page, .characters)
        XCTAssertEqual(store.characterTab, .blind)
        XCTAssertEqual(store.pendingBlindPrefill?.character, "bob")
        XCTAssertEqual(store.pendingBlindPrefill?.versionA, "v2pro/g1/s1")
        XCTAssertEqual(store.pendingBlindPrefill?.versionB, "v4/g2/s2")

        store.synthesizeWith(character: "alice", versionID: "v2pro/g/s", refID: "r-pick")
        XCTAssertEqual(store.page, .generate)
        XCTAssertEqual(store.params.character, "alice")
        XCTAssertEqual(store.pendingComposerVersionID, "v2pro/g/s")
        XCTAssertEqual(store.params.ref_id, "r-pick")
        XCTAssertEqual(store.referenceMode, "参考库")
    }

    func testPrimaryReferenceDurationEligibility() {
        let shortRef = Ref(id: "r-short", audio_url: "/refs/r-short/audio", character: "alice", text: "a", lang: "ja", duration: 2.99, tags: [])
        let minRef = Ref(id: "r-min", audio_url: "/refs/r-min/audio", character: "alice", text: "b", lang: "ja", duration: 3.0, tags: [])
        let maxRef = Ref(id: "r-max", audio_url: "/refs/r-max/audio", character: "alice", text: "c", lang: "ja", duration: 10.0, tags: [])
        let longRef = Ref(id: "r-long", audio_url: "/refs/r-long/audio", character: "alice", text: "d", lang: "ja", duration: 10.01, tags: [])

        XCTAssertFalse(isPrimaryReferenceEligible(shortRef))
        XCTAssertNotNil(primaryReferenceIneligibleReason(shortRef))
        XCTAssertTrue(isPrimaryReferenceEligible(minRef))
        XCTAssertNil(primaryReferenceIneligibleReason(minRef))
        XCTAssertTrue(isPrimaryReferenceEligible(maxRef))
        XCTAssertNil(primaryReferenceIneligibleReason(maxRef))
        XCTAssertFalse(isPrimaryReferenceEligible(longRef))
        XCTAssertNotNil(primaryReferenceIneligibleReason(longRef))
    }

    @MainActor
    func testWorkLineFragmentIntervalAuxRefsAndStaleDetection() {
        let defaults = MemoryDefaults()
        let store = StudioStore(defaults: defaults)
        store.engines = sampleEngines()
        let line = WorkLine(
            engine: "v2pro", ref_id: "r1", fragment_interval: 0.3, aux_ref_ids: [],
            id: "l-1", character: "alice", text: "おはよう"
        )
        store.work = Work(id: "w-1", title: "test", created: "", updated: "", lines: [line])
        store.staleLines = [:]

        var edited = line
        edited.fragment_interval = 0.6
        edited.aux_ref_ids = ["r-aux-1"]
        store.changeLine(edited)

        XCTAssertTrue(store.staleLines["w-1"]?.contains("l-1") == true)
        XCTAssertEqual(edited.input.fragment_interval, 0.6)
        XCTAssertEqual(edited.input.aux_ref_ids, ["r-aux-1"])
        XCTAssertEqual(store.workPickerInput.aux_ref_ids, ["r-aux-1"])
        XCTAssertEqual(store.workPickerInput.fragment_interval, 0.6)
    }

    @MainActor
    func testSettingsPreferencesDefaultCandidateCountAndExportFormat() {
        let defaults = MemoryDefaults()
        defaults.set(9, forKey: "defaultCandidateCount")
        defaults.set("flac", forKey: "defaultExportFormat")
        let store = StudioStore(defaults: defaults)
        XCTAssertEqual(store.params.n, 9)
        XCTAssertEqual(store.defaultCandidateCount, 9)
        XCTAssertEqual(store.defaultExportFormat, .flac)

        store.defaultCandidateCount = 99
        XCTAssertEqual(store.defaultCandidateCount, 16)
        store.defaultExportFormat = .mp3
        XCTAssertEqual(store.defaultExportFormat, .mp3)
    }

    // MARK: - Review Round 1 Regression Tests

    @MainActor
    func testCharacterSwitchAndTryCombinationClearStaleRefsAndAuxRefs() async throws {
        let modelsPayload = ModelsResponse(
            characters: [
                CharacterInfo(slug: "alice", source: "builtin", default_ref: "s1", engines: [:]),
                CharacterInfo(slug: "bob", source: "builtin", default_ref: "k1", engines: [:])
            ],
            engines: sampleEngines()
        )
        let fixture = Stage4Fixture { request, _ in
            if request.url?.path == "/models" {
                return (200, try! JSONEncoder().encode(modelsPayload))
            }
            return (200, Data("{}".utf8))
        }
        let store = makeStore(fixture: fixture)
        store.models = modelsPayload.characters
        store.engines = modelsPayload.engines
        store.params.character = "alice"
        store.refs = [Ref(id: "s1", audio_url: "/refs/s1/audio", character: "alice", text: "hi", lang: "ja", duration: 5.0, tags: ["开心"])]
        store.selectedReferenceID = "s1"
        store.params.aux_ref_ids = ["s2"]

        // Switching character clears stale refs immediately before new refs load.
        store.selectCharacter("bob")
        XCTAssertTrue(store.refs.isEmpty)
        XCTAssertNil(store.selectedReferenceID)
        XCTAssertTrue(store.params.aux_ref_ids.isEmpty)

        // tryCombination switches character cleanly and clears previous character refs & aux refs.
        store.refs = [Ref(id: "k1", audio_url: "/refs/k1/audio", character: "bob", text: "yo", lang: "ja", duration: 5.0, tags: [])]
        store.params.aux_ref_ids = ["k-aux"]
        let combo = ComboEval(gpt: "g-alice.ckpt", sovits: "s-alice.pth", n: 3, cer_median: 0.05, spk_sim_median: 0.9, score_median: 92)
        let eval = TrainEval(job_id: "t1", engine: "v2pro", character: "alice", ref_id: "s-eval", eval_set: "hard", sentences: [], candidates_per_sentence: 3, combos: [combo], best: combo)
        try await store.tryCombination(combo, evaluation: eval)
        XCTAssertEqual(store.params.character, "alice")
        XCTAssertTrue(store.refs.isEmpty)
        XCTAssertTrue(store.params.aux_ref_ids.isEmpty)
        XCTAssertEqual(store.params.ref_id, "s-eval")
        XCTAssertEqual(store.referenceMode, "参考库")
        XCTAssertEqual(store.page, .generate)
    }

    @MainActor
    func testPendingRefPrefsFlushedBeforeSwitchAndResetDoesNotOverwriteNewCharacter() async throws {
        let gate = DispatchSemaphore(value: 0)
        let fixture = Stage4Fixture { request, body in
            if request.httpMethod == "PATCH", request.url?.path == "/characters/alice/prefs",
               let body, let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                if obj["reset_synth_params"] as? Bool == true {
                    gate.wait()
                    let prefs = CharacterPrefs(slug: "alice", default_ref: "s1", synth_params: CharacterSynthParams(), last_ref_id: "s2", aux_ref_ids: ["s3"])
                    return (200, try! JSONEncoder().encode(prefs))
                }
                let prefs = CharacterPrefs(
                    slug: "alice",
                    default_ref: "s1",
                    synth_params: CharacterSynthParams(),
                    last_ref_id: obj["last_ref_id"] as? String,
                    aux_ref_ids: obj["aux_ref_ids"] as? [String] ?? []
                )
                return (200, try! JSONEncoder().encode(prefs))
            }
            return (200, Data("{}".utf8))
        }
        let store = makeStore(fixture: fixture)
        store.models = [
            CharacterInfo(slug: "alice", source: "builtin", default_ref: "s1", engines: [:]),
            CharacterInfo(
                slug: "bob", source: "builtin", default_ref: "k1",
                prefs: CharacterPrefs(slug: "bob", default_ref: "k1", synth_params: CharacterSynthParams(speed: 1.4), last_ref_id: "k2", aux_ref_ids: []),
                engines: [:]
            )
        ]
        store.params.character = "alice"
        store.selectPrimaryRef("s2")
        store.toggleAuxRef("s3")
        // Pending debounced save should be flushed immediately on character switch instead of cancelled and dropped.
        store.selectCharacter("bob")
        for _ in 0..<40 {
            if fixture.requests.contains(where: { $0.httpMethod == "PATCH" && $0.url?.path == "/characters/alice/prefs" }) { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        let flushed = try XCTUnwrap(fixture.requests.last { $0.httpMethod == "PATCH" && $0.url?.path == "/characters/alice/prefs" })
        let flushedJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(flushed.httpBody)) as? [String: Any])
        XCTAssertEqual(flushedJSON["last_ref_id"] as? String, "s2")
        XCTAssertEqual(flushedJSON["aux_ref_ids"] as? [String], ["s3"])

        // Start reset on alice, switch to bob while request is in flight, then release response.
        store.selectCharacter("alice")
        let resetTask = Task { try await store.resetCharacterSynthParams() }
        try await Task.sleep(for: .milliseconds(50))
        store.selectCharacter("bob")
        XCTAssertEqual(store.params.speed, 1.4)
        gate.signal()
        try await resetTask.value

        // Active character bob's form parameters must NOT be overwritten by alice's reset response.
        XCTAssertEqual(store.params.character, "bob")
        XCTAssertEqual(store.params.speed, 1.4)
        XCTAssertEqual(store.params.ref_id, "k2")
    }

    @MainActor
    func testSetDefaultVersionPreservesOtherEngineDefaultsAndSelectsRecommended() async throws {
        let v2Gpt = WeightEntry(id: "v2pro:g2.ckpt", name: "g2.ckpt", engine: "v2pro", type: "gpt", character: "alice", source: "user", created_at: "2026-11-01", size_bytes: 10, path: "", default_for: ["alice"], is_default: true, evals: [], deletable: true)
        let v2Sov = WeightEntry(id: "v2pro:s2.pth", name: "s2.pth", engine: "v2pro", type: "sovits", character: "alice", source: "user", created_at: "2026-11-01", size_bytes: 10, path: "", default_for: ["alice"], is_default: true, evals: [], deletable: true)
        let v4Gpt = WeightEntry(id: "v4:g4.ckpt", name: "g4.ckpt", engine: "v4", type: "gpt", character: "alice", source: "user", created_at: "2026-11-20", size_bytes: 10, path: "", default_for: [], is_default: false, evals: [], deletable: true)
        let v4Sov = WeightEntry(id: "v4:s4.pth", name: "s4.pth", engine: "v4", type: "sovits", character: "alice", source: "user", created_at: "2026-11-20", size_bytes: 10, path: "", default_for: [], is_default: false, evals: [], deletable: true)
        let allWeights = [v2Gpt, v2Sov, v4Gpt, v4Sov]
        let versionsAfter = [
            ModelVersion(id: "v4/g4.ckpt/s4.pth", label: "我设的默认", engine: "v4", gpt: "g4.ckpt", sovits: "s4.pth", score: 93, score_set: "hard", recommended: true, source_run: nil, created: "2026-11-20"),
            ModelVersion(id: "v2pro/g2.ckpt/s2.pth", label: "11 月训练", engine: "v2pro", gpt: "g2.ckpt", sovits: "s2.pth", score: 90, score_set: "hard", recommended: false, source_run: nil, created: "2026-11-01")
        ]
        let modelsResponse = ModelsResponse(characters: [CharacterInfo(slug: "alice", source: "builtin", default_ref: "s1", engines: [:])], engines: sampleEngines())

        let fixture = Stage4Fixture { request, _ in
            let path = request.url?.path ?? ""
            if request.httpMethod == "GET", path == "/weights" {
                let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
                let engine = items.first(where: { $0.name == "engine" })?.value
                let source = items.first(where: { $0.name == "source" })?.value
                let filtered = allWeights.filter { w in
                    (engine == nil || w.engine == engine) && (source == nil || w.source == source)
                }
                return (200, try! JSONEncoder().encode(WeightList(total: filtered.count, weights: filtered)))
            }
            if request.httpMethod == "PATCH", path.hasPrefix("/weights/") {
                let id = String(path.dropFirst("/weights/".count))
                let entry = allWeights.first(where: { $0.id == id }) ?? v4Gpt
                return (200, try! JSONEncoder().encode(entry))
            }
            if request.httpMethod == "GET", path == "/models" {
                return (200, try! JSONEncoder().encode(modelsResponse))
            }
            if request.httpMethod == "GET", path == "/characters/alice/versions" {
                return (200, try! JSONEncoder().encode(versionsAfter))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.models = modelsResponse.characters
        store.engines = modelsResponse.engines
        store.params.character = "alice"
        store.composerVersions.rows = [versionsAfter[1], versionsAfter[0]]
        store.selectComposerVersion("v2pro/g2.ckpt/s2.pth")
        XCTAssertEqual(store.composerVersions.selectedID, "v2pro/g2.ckpt/s2.pth")

        try await store.setDefaultVersion(character: "alice", engine: "v4", gpt: "g4.ckpt", sovits: "s4.pth")

        let patches = fixture.requests.filter { $0.httpMethod == "PATCH" && ($0.url?.path.hasPrefix("/weights/") ?? false) }
        let clearedPaths = patches.filter { req in
            let json = (try? JSONSerialization.jsonObject(with: req.httpBody ?? Data())) as? [String: Any]
            return json?["is_default"] as? Bool == false
        }.compactMap { $0.url?.path }
        let setPaths = patches.filter { req in
            let json = (try? JSONSerialization.jsonObject(with: req.httpBody ?? Data())) as? [String: Any]
            return json?["is_default"] as? Bool == true
        }.compactMap { $0.url?.path }

        // Other engines' manual defaults are preserved; core dd214a6 prioritizes the newly updated engine.
        XCTAssertTrue(clearedPaths.isEmpty)
        XCTAssertEqual(setPaths, ["/weights/v4:g4.ckpt", "/weights/v4:s4.pth"])
        XCTAssertEqual(store.composerVersions.selectedID, "v4/g4.ckpt/s4.pth")
        XCTAssertEqual(store.params.engine, "v4")

        // WeightStore.setDefaultPair shares the same settingDefaultVersion mutex.
        store.weightManager.pairCharacter = "alice"
        store.weightManager.pairEngine = "v4"
        store.weightManager.pairRows = [v4Gpt, v4Sov]
        store.weightManager.gptID = v4Gpt.id
        store.weightManager.sovitsID = v4Sov.id
        let patchCountBefore = fixture.requests.filter { $0.httpMethod == "PATCH" }.count
        store.settingDefaultVersion = true
        try await store.weightManager.setDefaultPair(studio: store)
        store.settingDefaultVersion = false
        XCTAssertEqual(fixture.requests.filter { $0.httpMethod == "PATCH" }.count, patchCountBefore)
    }

    @MainActor
    func testInFlightRefPrefsPatchSurvivesCharacterSwitchAndSetBaseURLClearsRefs() async throws {
        let releasePatch = DispatchSemaphore(value: 0)
        let fixture = Stage4Fixture { request, _ in
            if request.httpMethod == "PATCH", request.url?.path == "/characters/alice/prefs" {
                releasePatch.wait()
                let prefs = CharacterPrefs(slug: "alice", default_ref: "s1", synth_params: CharacterSynthParams(), last_ref_id: "s-inflight", aux_ref_ids: [])
                return (200, try! JSONEncoder().encode(prefs))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.models = [
            CharacterInfo(slug: "alice", source: "builtin", default_ref: "s1", engines: [:]),
            CharacterInfo(slug: "bob", source: "builtin", default_ref: "k1", engines: [:])
        ]
        store.params.character = "alice"
        store.referenceMode = "参考库"
        store.params.ref_id = "s-inflight"
        store.scheduleRefPrefsSave(delayMs: 15)

        // Yield on MainActor until the debounced PATCH has entered URLProtocol and is in flight.
        for _ in 0..<80 {
            if fixture.requests.contains(where: { $0.httpMethod == "PATCH" && $0.url?.path == "/characters/alice/prefs" }) { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(fixture.requests.contains(where: { $0.httpMethod == "PATCH" && $0.url?.path == "/characters/alice/prefs" }))

        // Switching character while PATCH is in flight must not cancel the in-flight request.
        store.selectCharacter("bob")
        releasePatch.signal()

        for _ in 0..<40 {
            if store.models.first(where: { $0.slug == "alice" })?.prefs?.last_ref_id == "s-inflight" { break }
            try await Task.sleep(for: .milliseconds(25))
        }
        XCTAssertEqual(store.models.first(where: { $0.slug == "alice" })?.prefs?.last_ref_id, "s-inflight")

        // Switching controller via setBaseURL clears refs and selectedReferenceID immediately.
        store.refs = [Ref(id: "k1", audio_url: "/refs/k1/audio", character: "bob", text: "hi", lang: "ja", duration: 4.5, tags: [])]
        store.selectedReferenceID = "k1"
        try store.setBaseURL("http://other-controller:9880")
        XCTAssertTrue(store.refs.isEmpty)
        XCTAssertNil(store.selectedReferenceID)
    }

    @MainActor
    func testWorkLineMatchesNormalizesDedupedAndPrimaryExcludedAuxRefs() {
        let store = StudioStore(defaults: MemoryDefaults())
        store.engines = sampleEngines()

        let line = WorkLine(
            engine: "v2pro", ref_id: nil, emotion: "开心",
            fragment_interval: 0.3, aux_ref_ids: ["r1", "r2", "r2"],
            id: "l-1", character: "alice", text: "おはよう", lang: "ja"
        )
        // Core resolves primary ref r1 and strips r1 + duplicates from aux_ref_ids -> ["r2"].
        let candidate = AudioItem(
            id: "a-1", created: "", audio_url: "/audio/a-1",
            text: "おはよう", text_lang: "ja", character: "alice", engine: "v2pro",
            gpt: "g", sovits: "s", ref_id: "r1", seed: 1,
            fragment_interval: 0.3, aux_ref_ids: ["r2"],
            duration: 2.0, infer_sec: 1.0, synth_wall_sec: 1.0, semantic_tokens: 40, vram_peak_mb: 0, ratio_vs_ref: []
        )
        XCTAssertTrue(store.canPick(candidate, for: line))

        // On an engine without aux_ref support (v4), core strips aux_ref_ids to []; candidate must still be pickable.
        var v4Line = line
        v4Line.engine = "v4"
        var v4Candidate = candidate
        v4Candidate.engine = "v4"
        v4Candidate.aux_ref_ids = []
        XCTAssertTrue(store.canPick(v4Candidate, for: v4Line))
    }

    @MainActor
    func testSaveWorkPreservesConcurrentEditsMadeInFlight() async throws {
        let gate = DispatchSemaphore(value: 0)
        let savedResponse = Work(
            id: "w-1", title: "old-title", created: "", updated: "",
            lines: [WorkLine(fragment_interval: 0.3, id: "l-real", character: "alice", text: "old-text")]
        )
        let fixture = Stage4Fixture { request, _ in
            if request.httpMethod == "PATCH", request.url?.path == "/works/w-1" {
                gate.wait()
                return (200, try! JSONEncoder().encode(savedResponse))
            }
            if request.url?.path == "/works" {
                return (200, try! JSONEncoder().encode(WorkList(works: [])))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.work = Work(
            id: "w-1", title: "old-title", created: "", updated: "",
            lines: [WorkLine(fragment_interval: 0.3, id: "draft-1", character: "alice", text: "old-text")]
        )
        store.workDirty = true

        let saveTask = Task { try await store.saveWork() }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertTrue(store.workSaving)

        // Concurrent edit while save is in flight.
        var edited = try XCTUnwrap(store.work?.lines.first)
        edited.text = "new-text-edited-during-save"
        edited.fragment_interval = 0.85
        store.changeLine(edited)

        gate.signal()
        try await saveTask.value

        XCTAssertFalse(store.workSaving)
        // Draft ID is remapped, concurrent edits are preserved, and workDirty stays true.
        XCTAssertEqual(store.work?.lines.first?.id, "l-real")
        XCTAssertEqual(store.work?.lines.first?.text, "new-text-edited-during-save")
        XCTAssertEqual(store.work?.lines.first?.fragment_interval, 0.85)
        XCTAssertTrue(store.workDirty)
    }

    @MainActor
    func testPlanAndSubmitCapabilityParityInEmotionModeAndFineFragmentInterval() async throws {
        let fixture = Stage4Fixture { request, _ in
            if request.httpMethod == "POST", request.url?.path == "/tts/candidates/jobs" {
                let job = Job(id: "j-emo", kind: "candidates", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: "", candidates_result: CandidatesResult(engine: "v2pro", ref_id: "r-picked", ref_text: "", elapsed_sec: 1, candidates: []))
                return (200, try! JSONEncoder().encode(job))
            }
            return (200, Data("{}".utf8))
        }
        let store = makeStore(fixture: fixture)
        store.engines = sampleEngines()
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r-def", engines: [:])]
        store.params.character = "alice"
        store.params.engine = "v2pro"
        store.params.ref_id = "r-aux-1" // stale primary ref kept in memory while switched to emotion mode
        store.referenceMode = "情绪"
        store.emotion = "开心"
        store.params.aux_ref_ids = ["r-aux-1", "r-aux-2"]
        store.params.fragment_interval = 0.3005 // differs from 0.3 by 5e-4 (> 1e-9, < 0.001)
        store.useManualComposer()

        // In emotion mode, old params.ref_id must NOT filter out aux_ref_ids in either plan or submit.
        XCTAssertEqual(store.generationPlanInput.aux_ref_ids, ["r-aux-1", "r-aux-2"])
        XCTAssertEqual(store.generationPlanInput.fragment_interval, 0.3005)

        try await store.generate()
        let req = try XCTUnwrap(fixture.requests.last { $0.url?.path == "/tts/candidates/jobs" })
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(req.httpBody)) as? [String: Any])
        XCTAssertEqual(body["aux_ref_ids"] as? [String], ["r-aux-1", "r-aux-2"])
        XCTAssertEqual(body["fragment_interval"] as? Double, 0.3005)
    }

    @MainActor
    func testEligibleEmotionTagsExcludeIneligibleDurations() {
        let store = StudioStore(defaults: MemoryDefaults())
        store.params.character = "alice"
        store.refs = [
            Ref(id: "r-short", audio_url: "/refs/r-short/audio", character: "alice", text: "short", lang: "ja", duration: 1.8, tags: ["生气"]),
            Ref(id: "r-long", audio_url: "/refs/r-long/audio", character: "alice", text: "long", lang: "ja", duration: 11.2, tags: ["低落"]),
            Ref(id: "r-ok", audio_url: "/refs/r-ok/audio", character: "alice", text: "ok", lang: "ja", duration: 5.2, tags: ["开心"]),
            Ref(id: "r-other-char", audio_url: "/refs/r-other/audio", character: "bob", text: "other", lang: "ja", duration: 5.0, tags: ["害羞"])
        ]

        XCTAssertEqual(eligibleEmotionTags(in: store.refs, for: "alice"), ["开心"])
        store.referenceMode = "情绪"
        store.emotion = "生气"
        XCTAssertFalse(store.hasValidEmotionReference)
        store.emotion = "开心"
        XCTAssertTrue(store.hasValidEmotionReference)
    }

    @MainActor
    func testHistoryReusePreservesHiddenManualWeightsAcrossLoadComposerWeights() async throws {
        let fixture = Stage4Fixture { request, _ in
            if request.url?.path == "/weights" {
                return (200, Data(#"{"total":0,"weights":[]}"#.utf8))
            }
            return (200, Data("{}".utf8))
        }
        let store = makeStore(fixture: fixture)
        store.engines = sampleEngines()
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:])]

        let hiddenItem = AudioItem(
            id: "a-hidden", created: "2026-11-20", audio_url: "/audio/a-hidden",
            text: "hidden voice", text_lang: "ja", character: "alice",
            engine: "v2pro", gpt: "hidden-g.ckpt", sovits: "hidden-s.pth",
            ref_id: "r1", seed: 123,
            duration: 3.0, infer_sec: 1.0, synth_wall_sec: 1.0, semantic_tokens: 50, vram_peak_mb: 0, ratio_vs_ref: []
        )
        store.reuseHistoryItem(hiddenItem)
        XCTAssertTrue(store.composerManual)
        XCTAssertEqual(store.params.gpt, "hidden-g.ckpt")
        XCTAssertEqual(store.params.sovits, "hidden-s.pth")

        // Loading visible weights must NOT wipe out reused hidden weight filenames.
        try await store.loadComposerWeights()
        XCTAssertEqual(store.params.gpt, "hidden-g.ckpt")
        XCTAssertEqual(store.params.sovits, "hidden-s.pth")
    }

    func testTrainingNewModeRejectsExistingSlugAndSweepValidatesInvalidBounds() {
        let engines = sampleEngines()
        let preview = DatasetPreview(id: "ds-1", lines_total: 100, lines_kept: 80, minutes_kept: 10, excluded_short: 0, excluded_long: 0, excluded_interjection: 0, interjection_examples: [])
        let request = TrainRequest(slug: "alice", dataset_id: "ds-1", engine: "v2pro")

        let reason = trainingBlockReason(
            request: request, submitting: false, previewLoading: false, preview: preview,
            engines: engines, targetMode: "new", existingSlugs: ["alice", "bob"]
        )
        XCTAssertEqual(reason, "该角色已存在，请换一个 slug 或切到「已有角色」追加训练")
        XCTAssertNil(trainingBlockReason(
            request: request, submitting: false, previewLoading: false, preview: preview,
            engines: engines, targetMode: "existing", existingSlugs: ["alice", "bob"]
        ))

        var sweep = SweepRequest(characters: ["alice"], engines: ["v4"], trains: [SweepTrain(sovits_epochs: 4, gpt_epochs: 10, sovits_save_every: 2, gpt_save_every: 5, sovits_batch: 0, gpt_batch: 8)])
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "每批大小（Batch）必须大于 0")
        sweep.trains[0].sovits_batch = 2
        sweep.max_lines = 5
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "每组最多句数不能少于 10 句")
        sweep.max_lines = 100
        sweep.samples = -1
        XCTAssertEqual(sweepBlockReason(request: sweep, submitting: false, allCompleted: false), "生成样本的组合数不能小于 0")
        sweep.samples = 1
        XCTAssertNil(sweepBlockReason(request: sweep, submitting: false, allCompleted: false))
    }

    // MARK: - V4 Mac Polish Tests

    @MainActor
    func testPreferredEngineFollowsCommonModelsAndFallbacks() {
        let all = ["v1", "v2", "v2pro", "v2proplus", "v3", "v4", "v5turbo", "v5dev"]

        // Default commonModels includes v2proplus -> prefers v2proplus.
        let defaultStore = StudioStore(defaults: MemoryDefaults())
        defaultStore.compute.engines = all
        XCTAssertEqual(defaultStore.preferredEngine, "v2proplus")
        XCTAssertEqual(defaultStore.params.engine, "v2proplus")

        // Single custom selection -> follows user's choice (v4 / v5turbo).
        defaultStore.compute.commonModels = ["v4"]
        XCTAssertEqual(defaultStore.preferredEngine, "v4")
        defaultStore.compute.commonModels = ["v5turbo"]
        XCTAssertEqual(defaultStore.preferredEngine, "v5turbo")

        // Multiple selections containing v2proplus -> prioritizes v2proplus.
        defaultStore.compute.commonModels = ["v2pro", "v2proplus", "v4"]
        XCTAssertEqual(defaultStore.preferredEngine, "v2proplus")

        // Multiple selections without v2proplus -> follows canonical order.
        defaultStore.compute.commonModels = ["v5dev", "v5turbo", "v4"]
        XCTAssertEqual(defaultStore.preferredEngine, "v4")
        defaultStore.compute.commonModels = ["v5dev", "v5turbo"]
        XCTAssertEqual(defaultStore.preferredEngine, "v5turbo")

        // Empty commonModels -> falls back to v2proplus, then v2pro when v2proplus unavailable.
        defaultStore.compute.commonModels = []
        XCTAssertEqual(defaultStore.preferredEngine, "v2proplus")
        defaultStore.compute.engines = ["v1", "v2pro", "v4"]
        XCTAssertEqual(defaultStore.preferredEngine, "v2pro")

        // addLine follows preferredEngine when no recommended version exists.
        defaultStore.compute.engines = all
        defaultStore.compute.commonModels = ["v4"]
        defaultStore.models = [CharacterInfo(slug: "alice", source: "builtin", engines: [:])]
        defaultStore.work = Work(id: "w-pref", title: "pref", created: "", updated: "", lines: [])
        defaultStore.addLine()
        XCTAssertEqual(defaultStore.work?.lines.first?.engine, "v4")

        // Default request models use v2proplus while legacy WorkLine JSON decoding remains backward compatible.
        XCTAssertEqual(TrainRequest(slug: "a", dataset_id: "d").engine, "v2proplus")
        XCTAssertEqual(CandidatesParams(text: "hi").engine, "v2proplus")
        XCTAssertEqual(BlindSide().engine, "v2proplus")
        XCTAssertEqual(WorkLine(id: "l-new", text: "hi").engine, "v2proplus")
        XCTAssertEqual(WorkLineIn(text: "hi").engine, "v2proplus")
        let legacyLine = try? JSONDecoder().decode(WorkLine.self, from: Data(#"{"id":"l-old","text":"hi"}"#.utf8))
        XCTAssertEqual(legacyLine?.engine, "v2pro")
    }

    func testDisplayEngineAndSortedEnginesOrder() {
        XCTAssertEqual(displayEngine("v2pro"), "v2Pro")
        XCTAssertEqual(displayEngine("v2proplus"), "v2ProPlus")
        XCTAssertEqual(displayEngine("v4"), "v4")
        XCTAssertEqual(displayEngine("v5turbo"), "v5 Turbo")
        XCTAssertEqual(displayEngine("v5dev"), "v5 Dev")
        XCTAssertEqual(displayEngine("v3"), "v3")
        XCTAssertEqual(displayEngine("v2"), "v2")
        XCTAssertEqual(displayEngine("v1"), "v1")
        XCTAssertEqual(displayEngine("custom"), "custom")

        let shuffled = ["v1", "v3", "v5dev", "v2pro", "v4", "v2proplus", "v2", "v5turbo", "v2pro"]
        XCTAssertEqual(
            sortedEngines(shuffled),
            ["v2proplus", "v2pro", "v4", "v5turbo", "v5dev", "v3", "v2", "v1"]
        )

        let infos = ["v1", "v4", "v2proplus", "v5turbo", "v2pro"].map { id in
            EngineInfo(id: id, version: id, code: "fork", code_path: "", zero_shot: true, trainable: true, train_method: "full", pretrained_gpt: "", pretrained_sovits: "", has_july_weights: false)
        }
        let sortedInstalledFirst = sortedEngineInfos(infos, installed: ["v2proplus", "v4", "v5turbo"])
        XCTAssertEqual(sortedInstalledFirst.map(\.id), ["v2proplus", "v4", "v5turbo", "v2pro", "v1"])
    }

    func testModelSpeedTableFormattingForGenerationTrainingAndScorer() {
        // Model generation speed: < 10 keeps 1 decimal, >= 10 rounds to integer, nil/untested -> 未测.
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: 4.7, source: "reference")), "约 4.7 秒/句")
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: 9.94, source: "measured")), "约 9.9 秒/句")
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: 9.96, source: "measured")), "约 10 秒/句")
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: 31.4, source: "measured")), "约 31 秒/句")
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: nil, source: "untested")), "未测")
        XCTAssertEqual(displayModelSpeed(EngineSpeed(sec_per_item: 3.0, source: "untested")), "未测")
        XCTAssertEqual(displayModelSpeed(nil), "未测")

        // Training speed: seconds per line + 1000-line estimate in minutes or hours.
        XCTAssertEqual(displayTrainSpeed(EngineSpeed(sec_per_item: 1.1, source: "measured")), "约 1.1 秒/行 · 千句约 18 分钟")
        XCTAssertEqual(displayTrainSpeed(EngineSpeed(sec_per_item: 10.4, source: "reference")), "约 10 秒/行 · 千句约 3 小时")
        XCTAssertEqual(displayTrainSpeed(EngineSpeed(sec_per_item: nil, source: "untested")), "未测")
        XCTAssertEqual(displayTrainSpeed(nil), "未测")

        // Scorer speed: > 0 shows seconds/item; 0 with non-untested shows included label; untested/nil shows 未测.
        XCTAssertEqual(displayScorerSpeed(EngineSpeed(sec_per_item: 4.3, source: "measured")), "约 4.3 秒/条")
        XCTAssertEqual(displayScorerSpeed(EngineSpeed(sec_per_item: 0, source: "reference")), "已含在评测耗时内")
        XCTAssertEqual(displayScorerSpeed(EngineSpeed(sec_per_item: nil, source: "untested")), "未测")
        XCTAssertEqual(displayScorerSpeed(nil), "未测")

        // Speed table engine keys exclude train/scorer and keep canonical order including v5.
        let speeds: [String: EngineSpeed] = [
            "train": EngineSpeed(sec_per_item: 1.1, source: "reference"),
            "scorer": EngineSpeed(sec_per_item: 0, source: "reference"),
            "v5dev": EngineSpeed(sec_per_item: 45, source: "reference"),
            "v5turbo": EngineSpeed(sec_per_item: 12, source: "reference"),
            "v2pro": EngineSpeed(sec_per_item: 4.7, source: "measured"),
            "v2proplus": EngineSpeed(sec_per_item: 4.7, source: "measured"),
        ]
        XCTAssertEqual(
            engineKeysForSpeedTable(engines: ["v4", "v2pro"], speeds: speeds),
            ["v2proplus", "v2pro", "v4", "v5turbo", "v5dev"]
        )
    }

    @MainActor
    func testPlanRequestEncodesCharacterVersionWeightsAndRefs() throws {
        let req = PlanRequest(
            kind: "gen",
            engine: "v2proplus",
            items: 6,
            character: "alice",
            version: "v2proplus/g.ckpt/s.pth",
            gpt: "g.ckpt",
            sovits: "s.pth",
            ref_id: "ref-1",
            aux_ref_ids: ["ref-2", "ref-3"],
            fragment_interval: 0.45
        )
        let data = try TaskPlanner.body(req, compute: "colab-L4")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["kind"] as? String, "gen")
        XCTAssertEqual(json["engine"] as? String, "v2proplus")
        XCTAssertEqual(json["items"] as? Int, 6)
        XCTAssertEqual(json["character"] as? String, "alice")
        XCTAssertEqual(json["version"] as? String, "v2proplus/g.ckpt/s.pth")
        XCTAssertEqual(json["gpt"] as? String, "g.ckpt")
        XCTAssertEqual(json["sovits"] as? String, "s.pth")
        XCTAssertEqual(json["ref_id"] as? String, "ref-1")
        XCTAssertEqual(json["aux_ref_ids"] as? [String], ["ref-2", "ref-3"])
        XCTAssertEqual(json["fragment_interval"] as? Double, 0.45)
        XCTAssertEqual(json["compute"] as? String, "colab-L4")

        // Omitted optional fields stay absent from encoded payload for older backends.
        let minimal = PlanRequest(kind: "gen", engine: "v2proplus", items: 3)
        let minimalObj = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(minimal)) as? [String: Any])
        XCTAssertNil(minimalObj["character"])
        XCTAssertNil(minimalObj["version"])
        XCTAssertNil(minimalObj["gpt"])
        XCTAssertNil(minimalObj["sovits"])
        XCTAssertNil(minimalObj["ref_id"])
        XCTAssertNil(minimalObj["aux_ref_ids"])

        // StudioStore.generationPlanInput populates character, version, weights, and primary ref.
        let store = StudioStore(defaults: MemoryDefaults())
        store.engines = sampleEngines()
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r-def", engines: [:])]
        store.params.character = "alice"
        store.params.engine = "v2pro"
        store.params.version = "v2pro/g2.ckpt/s2.pth"
        store.params.gpt = "g2.ckpt"
        store.params.sovits = "s2.pth"
        store.referenceMode = "默认"
        let planInput = store.generationPlanInput
        XCTAssertEqual(planInput.character, "alice")
        XCTAssertEqual(planInput.version, "v2pro/g2.ckpt/s2.pth")
        XCTAssertEqual(planInput.gpt, "g2.ckpt")
        XCTAssertEqual(planInput.sovits, "s2.pth")
        XCTAssertEqual(planInput.ref_id, "r-def")
    }

    @MainActor
    func testManualCloudGPUSelectionForGenerationConfirmsAndSubmitsApprovedFields() async throws {
        let quoteJSON = """
        {"code":"confirm_required","detail":"需要确认云端花费","plan":{
          "recommended":"local","chosen":"colab-L4","reason_code":"pinned","reason":"已固定这台",
          "candidates":[
            {"device":"colab-L4","name":"云端 L4","type":"cloud","gpu":"L4","seconds":240,"units":0.45,"speed_source":"reference","available":true,"idle":true,"premium":false,"prep_sec":180},
            {"device":"local","name":"studio-host","type":"local","seconds":40,"available":true,"idle":true,"premium":false}
          ],
          "confirm":["over_task"],"month_used":10,"ask_over":0.2,"ask_month":500
        }}
        """
        let fixture = Stage4Fixture { request, body in
            if request.httpMethod == "POST", request.url?.path == "/tts/candidates/jobs" {
                let obj = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
                if obj["approved"] as? Bool != true {
                    return (409, Data(quoteJSON.utf8))
                }
                let job = Job(
                    id: "j-cloud-gen", kind: "candidates", target: "alice",
                    status: "succeeded", progress: 1, stage: "取回完成", created: "",
                    candidates_result: CandidatesResult(engine: "v2proplus", ref_id: "r1", ref_text: "", elapsed_sec: 12, candidates: []),
                    compute: JobCompute(device: "colab-L4", name: "云端 L4", type: "cloud")
                )
                return (200, try! JSONEncoder().encode(job))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        store.engines = sampleEngines()
        store.models = [CharacterInfo(slug: "alice", source: "builtin", default_ref: "r1", engines: [:])]
        store.params.character = "alice"
        store.params.engine = "v2proplus"
        store.useManualComposer()
        store.generationPlanner.selected = "colab-L4"

        let genTask = Task { try await store.generate() }
        for _ in 0..<100 {
            if store.generationPlanner.confirmation != nil { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        let confirmation = try XCTUnwrap(store.generationPlanner.confirmation)
        XCTAssertEqual(confirmation.chosen, "colab-L4")
        XCTAssertEqual(confirmation.selected?.prep_sec, 180)
        XCTAssertEqual(cloudPreparation(try XCTUnwrap(confirmation.selected)), "要先准备环境约 3 分钟")

        store.generationPlanner.resolve(true)
        try await genTask.value

        let posts = fixture.requests.filter { $0.httpMethod == "POST" && $0.url?.path == "/tts/candidates/jobs" }
        XCTAssertEqual(posts.count, 2)
        let firstBody = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(posts[0].httpBody)) as? [String: Any])
        XCTAssertEqual(firstBody["compute"] as? String, "colab-L4")
        XCTAssertNil(firstBody["approved"])

        let secondBody = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(posts[1].httpBody)) as? [String: Any])
        XCTAssertEqual(secondBody["compute"] as? String, "colab-L4")
        XCTAssertEqual(secondBody["approved"] as? Bool, true)
        XCTAssertEqual(secondBody["approved_device"] as? String, "colab-L4")
        XCTAssertEqual(secondBody["approved_units"] as? Double, 0.45)
    }

    // MARK: - V4 Mac Polish Review Round 1 Fixes

    @MainActor
    func testTrainableComputeEnginesExcludeLANOnlyAndPreserveEditedTrainingForm() {
        let store = StudioStore(defaults: MemoryDefaults())
        let v2pp = EngineInfo(
            id: "v2proplus", version: "v2ProPlus", code: "fork", code_path: "",
            zero_shot: true, trainable: true, train_method: "full",
            pretrained_gpt: "g2pp.ckpt", pretrained_sovits: "s2pp.pth", has_july_weights: true,
            train_defaults: TrainDefaults(sovits_epochs: 8, sovits_save_every: 4, sovits_batch: 8, gpt_epochs: 15, gpt_save_every: 5, gpt_batch: 8, lora: false)
        )
        store.engines = [v2pp] + sampleEngines()
        store.compute.engines = ["v2proplus", "v2pro", "v4", "v5turbo"]
        store.compute.devices = [
            ComputeDevice(id: "local", name: "studio-host", type: "local", status: "idle", engines: ["v2proplus", "v2pro"], speeds: [:]),
            ComputeDevice(id: "lan-1", name: "gpu-worker", type: "lan", status: "idle", engines: ["v2proplus", "v5turbo"], speeds: [:]),
            ComputeDevice(id: "colab-L4", name: "云端 L4", type: "cloud", status: "idle", engines: ["v2proplus", "v4"], speeds: [:])
        ]

        // User checked v5turbo (LAN-only) in commonModels -> trainable default must skip LAN-only v5turbo and pick v2proplus.
        store.compute.commonModels = ["v5turbo"]
        XCTAssertEqual(store.trainableComputeEngines, ["v2proplus", "v2pro", "v4"])
        XCTAssertEqual(store.preferredTrainableEngine()?.id, "v2proplus")
        XCTAssertEqual(store.sortedTrainableEngines.map(\.id), ["v2proplus", "v2pro", "v4", "v5turbo"])

        // When user has NOT edited the form, catalog refresh updates engine and applies defaults.
        var req = TrainRequest(slug: "alice", dataset_id: "ds-1", engine: "v2pro")
        var initialized = false
        TrainingView.syncPreferredTrainingEngine(
            request: &req,
            trainableEngines: store.sortedTrainableEngines,
            preferredEngine: store.preferredTrainableEngine(),
            fallbackEngineID: store.preferredEngine,
            initializedDefaults: &initialized,
            userEditedForm: false
        )
        XCTAssertEqual(req.engine, "v2proplus")
        XCTAssertEqual(req.sovits_epochs, 8)
        XCTAssertTrue(initialized)

        // Once user edits training parameters or picks v4, subsequent catalog refreshes preserve user values.
        req.engine = "v4"
        req.sovits_epochs = 6
        req.gpt_epochs = 25
        req.lora_rank = 64
        TrainingView.syncPreferredTrainingEngine(
            request: &req,
            trainableEngines: store.sortedTrainableEngines,
            preferredEngine: v2pp,
            fallbackEngineID: "v2proplus",
            initializedDefaults: &initialized,
            userEditedForm: true
        )
        XCTAssertEqual(req.engine, "v4")
        XCTAssertEqual(req.sovits_epochs, 6)
        XCTAssertEqual(req.gpt_epochs, 25)
        XCTAssertEqual(req.lora_rank, 64)
    }

    func testSweepInvalidSelectedEnginesRemainRemovableAndBlockSubmission() {
        let validIDs: Set<String> = ["v2proplus", "v2pro"]
        var sweep = SweepRequest(characters: ["alice"], engines: ["v2proplus", "v5turbo"])

        let invalid = SweepFormView.invalidSelectedEngines(selected: sweep.engines, validEngineIDs: validIDs, hasKnownCatalog: true)
        XCTAssertEqual(invalid, ["v5turbo"])

        let blocked = sweepBlockReason(request: sweep, submitting: false, allCompleted: false, allowedEngines: validIDs)
        XCTAssertNotNil(blocked)
        XCTAssertTrue(blocked?.contains("v5 Turbo") == true)

        // Removing the unsupported engine clears the block.
        sweep.engines.removeAll { $0 == "v5turbo" }
        XCTAssertTrue(SweepFormView.invalidSelectedEngines(selected: sweep.engines, validEngineIDs: validIDs, hasKnownCatalog: true).isEmpty)
        XCTAssertNil(sweepBlockReason(request: sweep, submitting: false, allCompleted: false, allowedEngines: validIDs))
    }

    @MainActor
    func testBlindAsyncVersionLoadPreservesEditedSidesAndIgnoresStaleCharacter() {
        let v1 = ModelVersion(id: "v2proplus/g1/s1", label: "版本一", engine: "v2proplus", gpt: "g1", sovits: "s1", score: 90, score_set: "hard", recommended: true, source_run: nil, created: "2026-11-20")
        let v2 = ModelVersion(id: "v4/g2/s2", label: "版本二", engine: "v4", gpt: "g2", sovits: "s2", score: 88, score_set: "hard", recommended: false, source_run: nil, created: "2026-11-21")

        // User edited side A manually before async versions returned; side B was untouched.
        var a = BlindSide(engine: "v5turbo", gpt: "custom-g.ckpt", sovits: "custom-s.pth", version: nil)
        var b = BlindSide(engine: "v2proplus")
        var appliedPrefill = false
        BlindCreateView.applyLoadedVersions(
            targetCharacter: "alice",
            currentCharacter: "alice",
            versions: [v1, v2],
            selectedVersion: v1,
            changedCharacter: true,
            prefill: nil,
            appliedPrefill: &appliedPrefill,
            editedA: true,
            editedB: false,
            a: &a,
            b: &b
        )
        XCTAssertEqual(a, BlindSide(engine: "v5turbo", gpt: "custom-g.ckpt", sovits: "custom-s.pth", version: nil))
        XCTAssertEqual(b.version, v2.id)
        XCTAssertEqual(b.engine, "v4")

        // Stale async response after character switch must not touch either side.
        var kanaA = BlindSide(engine: "v2pro")
        var kanaB = BlindSide(engine: "v2pro")
        BlindCreateView.applyLoadedVersions(
            targetCharacter: "alice",
            currentCharacter: "bob",
            versions: [v1, v2],
            selectedVersion: v1,
            changedCharacter: true,
            prefill: nil,
            appliedPrefill: &appliedPrefill,
            editedA: false,
            editedB: false,
            a: &kanaA,
            b: &kanaB
        )
        XCTAssertEqual(kanaA, BlindSide(engine: "v2pro"))
        XCTAssertEqual(kanaB, BlindSide(engine: "v2pro"))
    }

    @MainActor
    func testAddLinePreservesManualComposerEngineWhenNoRecommendedVersion() {
        let store = StudioStore(defaults: MemoryDefaults())
        store.engines = sampleEngines()
        store.compute.engines = ["v2proplus", "v2pro", "v4", "v5turbo"]
        store.compute.commonModels = ["v5turbo"]
        store.models = [CharacterInfo(slug: "alice", source: "builtin", engines: [:])]
        store.params.character = "alice"
        store.work = Work(id: "w-manual", title: "manual", created: "", updated: "", lines: [])

        // Explicit manual selection in composer is preserved by addLine when character has no recommended version.
        store.useManualComposer()
        store.params.engine = "v4"
        store.params.gpt = "manual-g4.ckpt"
        store.params.sovits = "manual-s4.pth"
        store.addLine()
        let firstLine = store.work?.lines.last
        XCTAssertEqual(firstLine?.engine, "v4")
        XCTAssertEqual(firstLine?.gpt, "manual-g4.ckpt")
        XCTAssertEqual(firstLine?.sovits, "manual-s4.pth")
        XCTAssertNil(firstLine?.version)

        // Without manual selection (and no recommended version), addLine falls back to preferredEngine.
        store.composerManual = false
        store.addLine()
        let secondLine = store.work?.lines.last
        XCTAssertEqual(secondLine?.engine, "v5turbo")
        XCTAssertNil(secondLine?.gpt)
        XCTAssertNil(secondLine?.sovits)
    }

    func testCommonSpeedIconsFilterByDeviceOwnEngines() {
        let remoteSpeeds: [String: EngineSpeed] = [
            "v5turbo": EngineSpeed(sec_per_item: 8.5, source: "measured"),
            "v2proplus": EngineSpeed(sec_per_item: 3.2, source: "measured"),
            "train": EngineSpeed(sec_per_item: 1.1, source: "reference"),
            "scorer": EngineSpeed(sec_per_item: 0, source: "reference")
        ]
        // Even if controller compute.engines only lists v2proplus/v2pro, remote device's v5turbo is displayed.
        let shown = CommonSpeedIcons.displayedEngines(
            commonModels: ["v5turbo", "v4", "v2proplus"],
            deviceEngines: ["v2proplus", "v5turbo"],
            speeds: remoteSpeeds,
            fallbackEngines: ["v2proplus", "v2pro"]
        )
        XCTAssertEqual(shown, ["v2proplus", "v5turbo"])
    }

    func testZeroAndNegativeSpeedBoundariesAndZeroCloudPrepSec() {
        let zeroMeasured = EngineSpeed(sec_per_item: 0, source: "measured")
        let negativeRef = EngineSpeed(sec_per_item: -2, source: "reference")

        XCTAssertNil(zeroMeasured.grade)
        XCTAssertNil(negativeRef.grade)
        XCTAssertFalse(zeroMeasured.hasValidPositiveSpeed)
        XCTAssertEqual(displayModelSpeed(zeroMeasured), "未测")
        XCTAssertEqual(displayModelSpeed(negativeRef), "未测")
        XCTAssertEqual(displayTrainSpeed(zeroMeasured), "未测")
        XCTAssertEqual(displayTrainSpeed(negativeRef), "未测")
        XCTAssertEqual(zeroMeasured.effectiveSource(allowZero: false), "untested")
        XCTAssertEqual(zeroMeasured.effectiveSourceLabel(allowZero: false), "未测")
        XCTAssertEqual(negativeRef.effectiveSource(allowZero: true), "untested")
        XCTAssertEqual(displayScorerSpeed(negativeRef), "未测")

        // Scorer == 0 with non-untested source remains valid ("included").
        let cloudScorer = EngineSpeed(sec_per_item: 0, source: "reference")
        XCTAssertEqual(displayScorerSpeed(cloudScorer), "已含在评测耗时内")
        XCTAssertEqual(cloudScorer.effectiveSource(allowZero: true), "reference")
        XCTAssertEqual(cloudScorer.effectiveSourceLabel(allowZero: true), "参考值")

        // Cloud candidate with prep_sec <= 0 omits preparation phrasing.
        let warmCloud = PlanCandidate(
            device: "colab-L4", name: "云端 L4", type: "cloud", gpu: "L4",
            seconds: 45, units: 0.1, speed_source: "measured",
            available: true, reason_code: nil, reason: nil, idle: true, premium: false,
            wake: nil, prep_sec: 0
        )
        XCTAssertNil(cloudPreparation(warmCloud))
        XCTAssertEqual(computeQueueLabel(warmCloud, device: nil), "云端 GPU · 已就绪")

        var coldCloud = warmCloud
        coldCloud.prep_sec = 180
        XCTAssertEqual(cloudPreparation(coldCloud), "要先准备环境约 3 分钟")

        var unknownPrepCloud = warmCloud
        unknownPrepCloud.prep_sec = nil
        XCTAssertEqual(cloudPreparation(unknownPrepCloud), "要先准备环境")
    }

    @MainActor
    func testPlanInputCloudCapableEncodingForWorksBlindAndGenerate() throws {
        // Default nil omits cloud_capable from JSON payload.
        let defaultPlan = PlanInput(kind: "gen", engine: "v2proplus", items: 3)
        let defaultJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(defaultPlan)) as? [String: Any])
        XCTAssertNil(defaultJSON["cloud_capable"])

        // Explicit false encodes cloud_capable: false.
        let disabledPlan = PlanInput(kind: "gen", engine: "v2proplus", items: 3, cloud_capable: false)
        let disabledJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(disabledPlan)) as? [String: Any])
        XCTAssertEqual(disabledJSON["cloud_capable"] as? Bool, false)

        // Works and Blind previews pass cloud_capable: false; Generate and Compare keep nil (cloud allowed).
        let store = StudioStore(defaults: MemoryDefaults())
        store.engines = sampleEngines()
        store.work = Work(
            id: "w-cloud", title: "work", created: "", updated: "",
            lines: [WorkLine(engine: "v2proplus", id: "l-1", character: "alice", text: "おはよう")]
        )
        XCTAssertEqual(store.workPickerInput.cloud_capable, false)
        let workJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: TaskPlanner.body(store.workPickerInput, compute: nil)) as? [String: Any])
        XCTAssertEqual(workJSON["cloud_capable"] as? Bool, false)

        let blindInput = BlindCreateView.blindPlanInput(engines: ["v2proplus", "v4"], items: 12)
        XCTAssertEqual(blindInput.cloud_capable, false)
        let blindJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: TaskPlanner.body(blindInput, compute: nil)) as? [String: Any])
        XCTAssertEqual(blindJSON["cloud_capable"] as? Bool, false)

        XCTAssertNil(store.generationPlanInput.cloud_capable)
        XCTAssertNil(store.comparisonPlanInput(engines: ["v2proplus", "v4"]).cloud_capable)
    }

    // MARK: - V4 Mac Status Sync & Storage Overview Tests

    @MainActor
    func testComputeSummaryAndCloudRowStateForTrainingGeneratingWarmAndIdle() {
        let store = StudioStore(defaults: MemoryDefaults())
        let local = ComputeDevice(
            id: "local", name: "studio-host", type: "local", status: "idle",
            engines: ["v2proplus"], speeds: [:],
            disk_free_bytes: 48_900_000_000, disk_total_bytes: 494_300_000_000, cache_bytes: 1_200_000_000
        )
        XCTAssertEqual(local.diskSummary, "本机磁盘可用 48.9 GB / 494.3 GB · 缓存 1.2 GB")
        XCTAssertFalse(local.isDiskLow)

        // 1. Cloud training
        let cloudTraining = ComputeDevice(
            id: "colab-L4", name: "云端 L4", type: "cloud", status: "training",
            activity: "正在训练", engines: ["v2proplus"], speeds: [:], warm: 1
        )
        store.compute.devices = [local, cloudTraining]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 L4 正在训练")
        XCTAssertEqual(ComputeSettingsView.cloudRowState(for: [cloudTraining]), .active("使用中 · 云端 L4 正在训练"))

        // 2. Cloud generating
        let cloudGenerating = ComputeDevice(
            id: "colab-L4", name: "云端 L4", type: "cloud", status: "busy",
            activity: "正在生成", engines: ["v2proplus"], speeds: [:]
        )
        store.compute.devices = [local, cloudGenerating]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 L4 正在生成")
        XCTAssertEqual(ComputeSettingsView.cloudRowState(for: [cloudGenerating]), .active("使用中 · 云端 L4 正在生成"))

        // 3. Cloud warm (kept alive after job, not active)
        let cloudWarm = ComputeDevice(
            id: "colab-L4", name: "云端 L4", type: "cloud", status: "unavailable",
            engines: ["v2proplus"], speeds: [:], warm: 1
        )
        store.compute.devices = [local, cloudWarm]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 L4 保留中")
        XCTAssertFalse(store.compute.summary.contains("云端 GPU 未开"))
        XCTAssertEqual(ComputeSettingsView.cloudRowState(for: [cloudWarm]), .warm("保留中 · 云端 L4"))

        // 4. All cloud devices off (warm nil or 0)
        let cloudOff = ComputeDevice(
            id: "colab-L4", name: "云端 L4", type: "cloud", status: "unavailable",
            engines: ["v2proplus"], speeds: [:], warm: 0
        )
        store.compute.devices = [local, cloudOff]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 GPU 未开")
        XCTAssertEqual(ComputeSettingsView.cloudRowState(for: [cloudOff]), .idle("未开"))
    }

    func testStorageOverviewDecodingDisplayBytesAndDriveSummaries() throws {
        // displayBytes formatting
        XCTAssertEqual(displayBytes(0), "0 B")
        XCTAssertEqual(displayBytes(-10), "0 B")
        XCTAssertEqual(displayBytes(512), "512 B")
        XCTAssertEqual(displayBytes(1_500), "1.5 KB")
        XCTAssertEqual(displayBytes(917_000_000), "917 MB")
        XCTAssertEqual(displayBytes(999_600_000), "1 GB")
        XCTAssertEqual(displayBytes(8_140_000_000), "8.1 GB")
        XCTAssertEqual(displayBytes(48_900_000_000), "48.9 GB")
        XCTAssertEqual(displayBytes(494_300_000_000), "494.3 GB")
        XCTAssertEqual(displayBytes(1_250_000_000_000), "1.3 TB")

        let payload = """
        {
          "measured_at": "2026-11-22T19:15:00+09:00",
          "disk": { "path": "/Users/test", "total_bytes": 494300000000, "used_bytes": 445400000000, "free_bytes": 48900000000 },
          "library": {
            "selected": true, "kind": "drive", "path": "/Users/test/GoogleDrive/tts-library",
            "total_bytes": 8140000000, "local_bytes": 8140000000,
            "categories": [
              { "key": "weights", "label": "模型权重", "bytes": 7212400000, "local_bytes": 7212700000, "files": 94 },
              { "key": "datasets", "label": "训练数据集", "bytes": 927600000, "local_bytes": 120000000, "files": 12 }
            ]
          },
          "local": {
            "machine": "studio-host", "total_bytes": 6712000000,
            "categories": [
              { "key": "engine", "label": "语音引擎与基础模型", "bytes": 5800000000, "local_bytes": 5800000000, "files": 1200 },
              { "key": "cache", "label": "数据集解包缓存", "bytes": 912000000 }
            ]
          }
        }
        """
        let decoded = try JSONDecoder().decode(StorageOverview.self, from: Data(payload.utf8))
        XCTAssertEqual(decoded.measured_at, "2026-11-22T19:15:00+09:00")
        XCTAssertEqual(decoded.disk.free_bytes, 48_900_000_000)
        XCTAssertFalse(decoded.disk.isLowSpace)
        XCTAssertEqual(decoded.local.sectionTitle, "studio-host 本机存储")
        XCTAssertEqual(decoded.local.categories.count, 2)
        XCTAssertNil(decoded.local.categories[1].local_bytes)
        XCTAssertNil(decoded.local.categories[1].files)

        // Drive library reports factual logical size and local usage only (never inferred "其余仅在云端")
        XCTAssertEqual(decoded.library.summaryLabel, "Google Drive · 文件总大小 8.1 GB · 本机占用 8.1 GB")
        XCTAssertFalse(decoded.library.summaryLabel.contains("其余仅在云端"))

        // Per-category: tiny filesystem block delta does not trigger parenthetical; large difference does
        XCTAssertFalse(decoded.library.categories[0].hasNotableLocalDifference)
        XCTAssertEqual(decoded.library.categories[0].sizeLabel(isDrive: true), "7.2 GB")
        XCTAssertTrue(decoded.library.categories[1].hasNotableLocalDifference)
        XCTAssertEqual(decoded.library.categories[1].sizeLabel(isDrive: true), "928 MB（本机 120 MB）")
        XCTAssertEqual(decoded.library.categories[1].sizeLabel(isDrive: false), "928 MB")

        // Drive library with smaller local usage still reports factual copy without "其余仅在云端"
        var smallerLocalLibrary = decoded.library
        smallerLocalLibrary.local_bytes = 2_400_000_000
        XCTAssertEqual(smallerLocalLibrary.summaryLabel, "Google Drive · 文件总大小 8.1 GB · 本机占用 2.4 GB")
        XCTAssertFalse(smallerLocalLibrary.summaryLabel.contains("其余仅在云端"))

        // Drive library without local_bytes falls back cleanly
        var noLocalBytesLibrary = decoded.library
        noLocalBytesLibrary.local_bytes = nil
        XCTAssertEqual(noLocalBytesLibrary.summaryLabel, "Google Drive · 文件总大小 8.1 GB")

        // Local (non-Drive) library summary
        var localLibrary = decoded.library
        localLibrary.kind = "local"
        XCTAssertEqual(localLibrary.summaryLabel, "资料库共 8.1 GB")
    }

    @MainActor
    func testLowDiskSpaceStatusBarThresholdAndStorageRefreshGraceful404() async throws {
        let store = StudioStore(defaults: MemoryDefaults())
        store.compute.devices = [
            ComputeDevice(id: "local", name: "studio-host", type: "local", status: "idle", engines: ["v2proplus"], speeds: [:])
        ]

        // Free space at exactly 20 GB threshold -> no warning
        store.compute.storage = StorageOverview(
            disk: DiskUsage(total_bytes: 500_000_000_000, used_bytes: 480_000_000_000, free_bytes: 20_000_000_000),
            library: LibraryStorage(selected: false),
            local: LocalStorage(machine: "studio-host")
        )
        XCTAssertFalse(store.compute.storage!.disk.isLowSpace)
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 GPU 未开")

        // Free space below 20 GB -> appends low-space reminder
        store.compute.storage?.disk.free_bytes = 12_400_000_000
        XCTAssertTrue(store.compute.storage!.disk.isLowSpace)
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 GPU 未开 · 磁盘剩余 12.4 GB")

        // Boundary 19,999,999,999 bytes -> still below 20 GB
        store.compute.storage?.disk.free_bytes = 19_999_999_999
        XCTAssertTrue(store.compute.storage!.disk.isLowSpace)
        XCTAssertTrue(store.compute.summary.contains("磁盘剩余"))

        // Local device disk_free_bytes takes precedence over storage?.disk.free_bytes:
        // 1) Fresh local device reports >= 20 GB -> clears stale storage low-space warning
        store.compute.devices = [
            ComputeDevice(id: "local", name: "studio-host", type: "local", status: "idle", engines: ["v2proplus"], speeds: [:], disk_free_bytes: 25_000_000_000, disk_total_bytes: 500_000_000_000)
        ]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 GPU 未开")

        // 2) Fresh local device reports < 20 GB -> shows low-space warning even without storage overview
        store.compute.storage = nil
        store.compute.devices = [
            ComputeDevice(id: "local", name: "studio-host", type: "local", status: "idle", engines: ["v2proplus"], speeds: [:], disk_free_bytes: 15_300_000_000, disk_total_bytes: 500_000_000_000)
        ]
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 GPU 未开 · 磁盘剩余 15.3 GB")

        // Older controller returning 404 on /system/storage does not pollute compute.error
        let fixture404 = Stage4Fixture { request, _ in
            if request.url?.path == "/system/storage" {
                return (404, Data(#"{"detail":"Not Found"}"#.utf8))
            }
            return (200, Data("{}".utf8))
        }
        let store404 = makeStore(fixture: fixture404)
        await store404.compute.refreshStorage(refresh: true)
        XCTAssertNil(store404.compute.storage)
        XCTAssertNil(store404.compute.error)
        XCTAssertFalse(store404.compute.storageLoading)
        XCTAssertEqual(fixture404.requests.last?.url?.query, "refresh=true")

        // Reset clears storage
        store.compute.reset()
        XCTAssertNil(store.compute.storage)
    }

    @MainActor
    func testJobStartAndFinishTriggerImmediateComputeRefresh() async throws {
        final class Box: @unchecked Sendable {
            private let lock = NSLock()
            private var value: String
            init(_ value: String) { self.value = value }
            func get() -> String { lock.lock(); defer { lock.unlock() }; return value }
            func set(_ newValue: String) { lock.lock(); value = newValue; lock.unlock() }
        }
        let cloudStatusBox = Box("training")

        let fixture = Stage4Fixture { request, _ in
            switch request.url?.path {
            case "/compute":
                let status = cloudStatusBox.get()
                let warm = status == "unavailable" ? 1 : 0
                let activityJSON = status == "training" ? "\"正在训练\"" : "null"
                let json = """
                {
                  "devices": [
                    {"id":"local","name":"studio-host","type":"local","status":"idle","engines":["v2proplus"],"speeds":{}},
                    {"id":"colab-L4","name":"云端 L4","type":"cloud","status":"\(status)","activity":\(activityJSON),"engines":["v2proplus"],"speeds":{},"warm":\(warm)}
                  ],
                  "auto": {"mode":"auto","device":null,"ask_over":2,"ask_month":500},
                  "engines": ["v2proplus"],
                  "month_used": 0
                }
                """
                return (200, Data(json.utf8))
            case "/library":
                return (200, Data(Stage2Fixtures.library.utf8))
            case "/cloud":
                return (200, Data(Stage2Fixtures.cloud.utf8))
            case "/system/info":
                return (200, Data(Stage2Fixtures.systemInfo.utf8))
            case "/system/storage":
                return (404, Data(#"{"detail":"Not Found"}"#.utf8))
            default:
                return (200, Data("{}".utf8))
            }
        }

        let store = makeStore(fixture: fixture)
        store.connected = true

        // Job starts -> triggers compute.refresh(refreshAccounts: false) immediately
        let activeJob = Job(id: "j-train-live", kind: "train", target: "alice", status: "running", progress: 0.1, stage: "训练中", created: "")
        store.updateJob(activeJob)
        for _ in 0..<80 {
            if store.compute.summary.contains("云端 L4 正在训练") { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        XCTAssertTrue(store.compute.summary.contains("云端 L4 正在训练"))
        // Must not force account refresh (?refresh=true)
        XCTAssertNil(fixture.requests.last(where: { $0.url?.path == "/cloud" })?.url?.query)

        // Job completes -> triggers another compute.refresh -> switches to warm state
        cloudStatusBox.set("unavailable")
        let doneJob = Job(id: "j-train-live", kind: "train", target: "alice", status: "succeeded", progress: 1.0, stage: "完成", created: "")
        store.updateJob(doneJob)
        for _ in 0..<80 {
            if store.compute.summary.contains("云端 L4 保留中") { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        XCTAssertTrue(store.compute.summary.contains("云端 L4 保留中"))
        XCTAssertFalse(store.compute.summary.contains("云端 GPU 未开"))
    }

    @MainActor
    func testStaleComputeResponseDroppedBySequenceNumber() async throws {
        final class RaceState: @unchecked Sendable {
            private let lock = NSLock()
            private var count = 0
            private var _firstStarted = false
            func next() -> Int { lock.lock(); defer { lock.unlock() }; count += 1; return count }
            func markFirstStarted() { lock.lock(); _firstStarted = true; lock.unlock() }
            var firstStarted: Bool { lock.lock(); defer { lock.unlock() }; return _firstStarted }
        }
        let state = RaceState()
        let releaseFirst = DispatchSemaphore(value: 0)
        defer { releaseFirst.signal() }

        let fixture = Stage4Fixture { request, _ in
            guard request.url?.path == "/compute" else { return (200, Data("{}".utf8)) }
            let call = state.next()
            if call == 1 {
                state.markFirstStarted()
                releaseFirst.wait()
                let staleTraining = """
                {
                  "devices": [
                    {"id":"local","name":"studio-host","type":"local","status":"idle","engines":["v2proplus"],"speeds":{}},
                    {"id":"colab-L4","name":"云端 L4","type":"cloud","status":"training","activity":"正在训练","engines":["v2proplus"],"speeds":{},"warm":0}
                  ],
                  "auto": {"mode":"auto","device":null,"ask_over":2,"ask_month":500},
                  "engines": ["v2proplus"],
                  "month_used": 0
                }
                """
                return (200, Data(staleTraining.utf8))
            }
            let newerWarm = """
            {
              "devices": [
                {"id":"local","name":"studio-host","type":"local","status":"idle","engines":["v2proplus"],"speeds":{}},
                {"id":"colab-L4","name":"云端 L4","type":"cloud","status":"unavailable","activity":null,"engines":["v2proplus"],"speeds":{},"warm":1}
              ],
              "auto": {"mode":"auto","device":null,"ask_over":2,"ask_month":500},
              "engines": ["v2proplus"],
              "month_used": 0
            }
            """
            return (200, Data(newerWarm.utf8))
        }

        let store = makeStore(fixture: fixture)

        // Start slow first /compute request (seq 1: training)
        let slowTask = Task { try await store.compute.refreshDevices() }
        for _ in 0..<80 {
            if state.firstStarted { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(state.firstStarted)

        // Issue newer /compute request (seq 2: warm) and finish it first
        try await store.compute.refreshDevices()
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 L4 保留中")

        // Release slow first response; sequence check must discard it instead of reverting to training
        releaseFirst.signal()
        try await slowTask.value
        XCTAssertEqual(store.compute.summary, "studio-host 空闲 · 云端 L4 保留中")
    }

    @MainActor
    func testSelectLibraryClearsOldStorageAndForcesRefresh() async throws {
        final class Flag: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            func set() { lock.lock(); value = true; lock.unlock() }
            func get() -> Bool { lock.lock(); defer { lock.unlock() }; return value }
        }
        let storageStarted = Flag()
        let releaseStorage = DispatchSemaphore(value: 0)
        defer { releaseStorage.signal() }

        let fixture = Stage4Fixture { request, _ in
            if request.httpMethod == "POST", request.url?.path == "/library" {
                let updatedLibrary = """
                {
                  "selected": true, "error": null, "path": "/tmp/new-library", "kind": "local",
                  "owner": {"machine_id": "m1", "name": "studio-host"}, "owned": true,
                  "this_machine": {"machine_id": "m1", "name": "studio-host"},
                  "counts": null, "conflicts": 0, "conflict_files": [], "broken_files": [], "scanned_at": null
                }
                """
                return (200, Data(updatedLibrary.utf8))
            }
            if request.url?.path == "/system/storage" {
                storageStarted.set()
                releaseStorage.wait()
                let refreshedStorage = """
                {
                  "measured_at": "2026-11-22T19:35:00+09:00",
                  "disk": {"path": "/Users/test", "total_bytes": 500000000000, "used_bytes": 450000000000, "free_bytes": 50000000000},
                  "library": {"selected": true, "kind": "local", "path": "/tmp/new-library", "total_bytes": 1200000000, "categories": []},
                  "local": {"machine": "studio-host", "total_bytes": 3000000000, "categories": []}
                }
                """
                return (200, Data(refreshedStorage.utf8))
            }
            return (200, Data("{}".utf8))
        }

        let store = makeStore(fixture: fixture)
        // Seed old library storage before switching
        store.compute.storage = StorageOverview(
            measured_at: "2026-11-22T18:00:00+09:00",
            disk: DiskUsage(total_bytes: 500_000_000_000, used_bytes: 450_000_000_000, free_bytes: 50_000_000_000),
            library: LibraryStorage(selected: true, kind: "drive", path: "/old/drive-library", total_bytes: 9_000_000_000, local_bytes: 9_000_000_000),
            local: LocalStorage(machine: "studio-host", total_bytes: 6_000_000_000)
        )

        let selectTask = Task { try await store.compute.selectLibrary("/tmp/new-library") }
        for _ in 0..<80 {
            if storageStarted.get() { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(storageStarted.get())

        // Immediately after POST /library succeeds (before /system/storage returns), stale storage must already be cleared
        XCTAssertNil(store.compute.storage)
        XCTAssertTrue(store.compute.storageLoading)

        releaseStorage.signal()
        try await selectTask.value

        XCTAssertFalse(store.compute.storageLoading)
        XCTAssertEqual(store.compute.storage?.library.path, "/tmp/new-library")
        XCTAssertEqual(store.compute.storage?.library.total_bytes, 1_200_000_000)
        XCTAssertEqual(fixture.requests.last(where: { $0.url?.path == "/system/storage" })?.url?.query, "refresh=true")
    }
}
