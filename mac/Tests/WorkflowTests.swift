import XCTest
@testable import TTSStudio

private final class WorkflowFixture: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [URLRequest] = []
    let responses: [String: Data]
    let echoReviews: Bool
    init(responses: [String: Data], echoReviews: Bool = false) { self.responses = responses; self.echoReviews = echoReviews }
    func response(_ request: URLRequest) -> Data {
        lock.lock(); defer { lock.unlock() }
        var copy = request
        if copy.httpBody == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var body = Data(), buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(contentsOf: buffer.prefix(count))
            }
            copy.httpBody = body
        }
        recorded.append(copy)
        if echoReviews && request.httpMethod == "POST" && request.url!.path.hasSuffix("/review"), let body = copy.httpBody,
           var json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
            json["audio_id"] = request.url!.pathComponents.dropLast().last!
            json["updated"] = String(recorded.count)
            return try! JSONSerialization.data(withJSONObject: json)
        }
        return responses["\(request.httpMethod ?? "GET") \(request.url!.path)"] ?? Data((request.url!.path.hasSuffix("/versions") ? "[]" : request.url!.path == "/weights" ? #"{"weights":[],"total":0}"# : "{}").utf8)
    }
    var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return recorded }
}
private final class WorkflowProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var fixture: WorkflowFixture!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let data = Self.fixture.response(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: request.httpMethod == "DELETE" && request.url!.path.hasSuffix("/review") ? 204 : 200, httpVersion: nil, headerFields: ["Content-Type":"application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}
final class WorkflowTests: XCTestCase {
    private func encoded<T: Encodable>(_ value: T) throws -> Data { try JSONEncoder().encode(value) }
    @MainActor private func store(_ fixture: WorkflowFixture, defaults: UserDefaults) -> StudioStore {
        WorkflowProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [WorkflowProtocol.self]
        let store = StudioStore(defaults: defaults)
        store.api = APIClient(baseURL: "http://fixture", session: URLSession(configuration: config))
        return store
    }
    private func audio(_ id: String, score: Double, engine: String = "v2proplus") -> AudioItem {
        AudioItem(id: id, created: "", audio_url: "/audio/\(id)", text: "台词", text_lang: "ja", character: "alice", engine: engine, gpt: "g", sovits: "s", ref_id: "r", seed: 1, duration: 1, infer_sec: 1, synth_wall_sec: 1, semantic_tokens: 25, vram_peak_mb: 0, ratio_vs_ref: [], score: score)
    }
    @MainActor func testImportSavesEditsAndRemapsSelectedDraftIDs() async throws {
        let defaults = MemoryDefaults()
        let saved = Work(id: "w-original", title: "edited", created: "", updated: "", lines: [WorkLine(id: "l-saved", text: "已修改")])
        let destination = Work(id: "w-destination", title: "destination", created: "", updated: "", lines: [WorkLine(id: "l-imported", text: "台词", candidates: [audio("a", score: 99)], picked: "a")])
        let fixture = WorkflowFixture(responses: ["PATCH /works/w-original":try encoded(saved), "GET /works":try encoded(WorkList(works: [])), "POST /works/w-destination/lines":try encoded(destination)])
        let store = store(fixture, defaults: defaults)
        store.work = Work(id: "w-original", title: "edited", created: "", updated: "", lines: [WorkLine(id: "draft-a", text: "已修改")])
        store.workDirty = true
        store.selectedWorkLineIDs = ["draft-a"]
        try await store.saveWork()
        XCTAssertEqual(store.selectedWorkLineIDs, ["l-saved"])
        store.workDirty = true
        try await store.addCandidateToWork(audio("a", score: 99), workID: "w-destination")
        let requests = fixture.requests
        XCTAssertEqual(requests.first?.httpMethod, "PATCH")
        let lastPost = try XCTUnwrap(requests.last { $0.httpMethod == "POST" })
        XCTAssertEqual(lastPost.url?.path, "/works/w-destination/lines")
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(lastPost.httpBody)) as! [String: Any]
        XCTAssertEqual(body["audio_id"] as? String, "a")
        XCTAssertNil(body["line"])
        XCTAssertEqual(store.work?.lines.first?.picked, "a")
    }
    @MainActor func testCoalescedCompletedProgressLoadsEveryCandidateOnce() async throws {
        let defaults = MemoryDefaults()
        let a = audio("a", score: 80), b = audio("b", score: 99)
        let items = [JobItemDone(audio_id: "a", audio_url: "/audio/a", score: 80, seed: 1, done: 1, total: 2), JobItemDone(audio_id: "b", audio_url: "/audio/b", score: 99, seed: 2, done: 2, total: 2)]
        let line = WorkLine(id: "l", text: "台词")
        let fixture = WorkflowFixture(responses: ["GET /audio/a/meta":try encoded(a),"GET /audio/b/meta":try encoded(b)])
        let store = store(fixture, defaults: defaults)
        store.work = Work(id: "w", title: "", created: "", updated: "", lines: [line])
        var completed = items
        for i in completed.indices { completed[i].line_id = "l" }
        let job = Job(id: "j", kind: "work_generate", target: "w", status: "running", progress: 1, stage: "", created: "", latest: completed.last, completed: completed)
        try await store.consumeProgress(job)
        try await store.consumeProgress(job)
        XCTAssertEqual(store.work?.lines[0].candidates.map(\.id), ["b", "a"])
        XCTAssertEqual(fixture.requests.count, 2)
    }
    @MainActor func testRapidTagQueueAdvancesBeforeNetworkAndPreservesEarlierTags() async throws {
        let defaults = MemoryDefaults()
        let first = Ref(id: "r1", audio_url: "/refs/r1/audio", character: "alice", text: "", lang: "ja", duration: 1, tags: ["已有"])
        let updated = Ref(id: "r1", audio_url: first.audio_url, character: first.character, text: "", lang: "ja", duration: 1, tags: ["已有", "开心"])
        let second = Ref(id: "r2", audio_url: "/refs/r2/audio", character: "alice", text: "", lang: "ja", duration: 1, tags: [])
        let fixture = WorkflowFixture(responses: ["PUT /refs/r1":try encoded(updated), "PUT /refs/r2":try encoded(second)])
        let store = store(fixture, defaults: defaults)
        store.refs = [first, second]
        store.selectedReferenceID = "r1"
        let one = store.tagAndAdvance("开心", visibleIDs: ["r1","r2"])
        XCTAssertEqual(store.selectedReferenceID, "r2")
        XCTAssertEqual(store.referenceAudition?.referenceID, "r2")
        let two = store.tagAndAdvance("平静", visibleIDs: ["r1","r2"])
        XCTAssertNil(store.selectedReferenceID)
        await one?.value; await two?.value
        XCTAssertEqual(fixture.requests.map { $0.url!.path }, ["/refs/r1","/refs/r2"])
        let body = try JSONSerialization.jsonObject(with: XCTUnwrap(fixture.requests.first?.httpBody)) as! [String: Any]
        XCTAssertEqual(Set(body["tags"] as! [String]), ["已有","开心"])
    }
}

extension WorkflowTests {
    @MainActor func testPauseEditInvalidatesOldRenderAcrossSaveAndReopen() async throws {
        let defaults = MemoryDefaults()
        let render = RenderResult(work_id: "w", rendered_at: "", duration: 1, wav_url: "/old.wav", mp3_url: "/old.mp3", srt_url: "/old.srt", lrc_url: "/old.lrc", lines: [])
        let old = Work(id: "w", title: "", created: "", updated: "", lines: [WorkLine(id: "l", text: "台词")], render: render)
        let fixture = WorkflowFixture(responses: ["PATCH /works/w":try encoded(old), "GET /works/w":try encoded(old), "GET /works":try encoded(WorkList(works: []))])
        let store = store(fixture, defaults: defaults)
        store.work = old
        var edited = old.lines[0]
        edited.pause_after = 2
        store.changeLine(edited)
        XCTAssertNil(store.work?.render)
        try await store.saveWork()
        XCTAssertNil(store.work?.render)
        try await store.openWork("w")
        XCTAssertNil(store.work?.render)
        XCTAssertFalse(store.staleLines["w"]?.contains("l") ?? false, "只改停顿无需重新合成，但必须重新渲染")
    }
}

extension WorkflowTests {
    @MainActor func testOldTextCandidateCannotBePickedAfterRegenerationClearsStaleFlag() async throws {
        let defaults = MemoryDefaults()
        let old = audio("old", score: 99)
        let line = WorkLine(id: "l", text: "新的台词", candidates: [old], picked: "new")
        let fixture = WorkflowFixture(responses: [:])
        let store = store(fixture, defaults: defaults)
        store.work = Work(id: "w", title: "", created: "", updated: "", lines: [line])
        XCTAssertTrue(store.staleLines.isEmpty, "模拟重新生成完成后的状态")
        XCTAssertFalse(store.canPick(old, for: line))
        do { try await store.pick(old, line: line); XCTFail("旧台词不能重新选用") }
        catch { XCTAssertTrue(error.localizedDescription.contains("旧台词")) }
        XCTAssertTrue(fixture.requests.isEmpty, "不向服务端提交无效的 pick")
    }
    @MainActor func testOlderEmotionCandidatesStayIneligibleAfterRegenerationAndRestart() {
        let defaults = MemoryDefaults()
        let old = audio("old", score: 99), new = audio("new", score: 90)
        let line = WorkLine(emotion: "开心", id: "l", character: "alice", text: "台词", candidates: [old], picked: old.id)
        let store = StudioStore(defaults: defaults)
        store.work = Work(id: "w", title: "", created: "", updated: "", lines: [line])
        var edited = line
        edited.emotion = "生气"
        store.changeLine(edited)
        store.staleLines["w"] = [] // regeneration clears the row flag, not obsolete candidates
        edited.candidates.append(new)
        edited.picked = new.id
        store.work?.lines[0] = edited
        XCTAssertFalse(store.canPick(old, for: edited))
        XCTAssertTrue(store.canPick(new, for: edited))
        let reopened = StudioStore(defaults: defaults)
        reopened.work = store.work
        XCTAssertFalse(reopened.canPick(old, for: edited))
    }
    func testCandidateIdentityMustMatchCurrentLine() {
        let line = WorkLine(ref_id: "r", id: "l", character: "alice", text: "台词")
        let original = audio("a", score: 99)
        XCTAssertTrue(line.matches(original))
        var variants = Array(repeating: original, count: 5)
        variants[0].text = "旧台词"; variants[1].character = "bob"
        variants[2].engine = "v5dev"; variants[3].text_lang = "en"; variants[4].ref_id = "other"
        for item in variants { XCTAssertFalse(line.matches(item)) }
    }
    @MainActor func testSubmittedTrainingAppearsWithoutRefreshAndKeepsSSEUpdates() async throws {
        let defaults = MemoryDefaults()
        let queued = Job(id: "new-train", kind: "train", target: "zz-test-voice", status: "queued", progress: 0, stage: "排队", created: "")
        var completed = queued; completed.status = "succeeded"; completed.progress = 1; completed.stage = "完成"
        let event = Data("data: \(String(decoding: try encoded(completed), as: UTF8.self))\n\n".utf8)
        let fixture = WorkflowFixture(responses: ["POST /train":try encoded(queued), "GET /jobs/new-train/events":event, "GET /models":try encoded(ModelsResponse(characters: []))])
        let store = store(fixture, defaults: defaults)
        try await store.submitTraining(TrainRequest(slug: "zz-test-voice", dataset_id: "fixture"))
        XCTAssertEqual(store.trainingJobs.map(\.id), [queued.id], "提交返回即显示任务，不依赖 GET /jobs")
        for _ in 0..<100 {
            if store.trainingJobs.first?.status == "succeeded" { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(store.trainingJobs.first?.progress, 1)
        XCTAssertEqual(store.trainingJobs.count, 1, "SSE 替换同一条任务")
        XCTAssertFalse(fixture.requests.contains { $0.url?.path == "/jobs" })
        store.updateJob(Job(id: "infer", kind: "candidates", target: "alice", status: "running", progress: 0, stage: "生成", created: ""))
        XCTAssertEqual(store.trainingJobs.map(\.id), [queued.id], "其他任务不进入训练列表")
    }
    @MainActor func testTrainingRefreshAndSuccessfulMonitorDiscoverNewCharacterWithoutResettingComposer() async throws {
        let defaults = MemoryDefaults()
        let newCharacter = CharacterInfo(slug: "new-voice", source: "user", default_ref: "r", engines: [:])
        let job = Job(id: "j", kind: "train", target: "new-voice", status: "succeeded", progress: 1, stage: "完成", created: "")
        let fixture = WorkflowFixture(responses: ["GET /models":try encoded(ModelsResponse(characters: [newCharacter])), "GET /jobs":try encoded(JobList(jobs: [job]))])
        let store = store(fixture, defaults: defaults)
        store.params.gpt = "selected-weight"
        store.params.ref_id = "selected-reference"
        var active = job; active.status = "running"
        XCTAssertFalse(store.isDeletedTrainingCharacter(active))
        store.monitor(job)
        for _ in 0..<100 {
            if store.trainingCatalogLoaded { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(store.trainingCatalogLoaded)
        XCTAssertEqual(store.models.first?.slug, "new-voice")
        XCTAssertFalse(store.isDeletedTrainingCharacter(job))
        XCTAssertEqual(store.params.gpt, "selected-weight")
        XCTAssertEqual(store.params.ref_id, "selected-reference")
        try await store.loadTrainingJobs()
        XCTAssertEqual(Set(fixture.requests.suffix(2).map { $0.url!.path }), ["/models", "/jobs"])
        var deleted = job; deleted.target = "removed"
        XCTAssertTrue(store.isDeletedTrainingCharacter(deleted))
        deleted.status = "queued"
        XCTAssertFalse(store.isDeletedTrainingCharacter(deleted))
        deleted.status = "failed"
        XCTAssertFalse(store.isDeletedTrainingCharacter(deleted))
    }
    @MainActor func testEvaluationRefreshesCatalogBeforeDeclaringCharacterDeleted() async throws {
        let defaults = MemoryDefaults()
        let job = Job(id: "j", kind: "train", target: "new-voice", status: "succeeded", progress: 1, stage: "", created: "")
        let combo = ComboEval(gpt: "g", sovits: "s", n: 1, cer_median: 0, spk_sim_median: 1, score_median: 100)
        let evaluation = TrainEval(job_id: "j", engine: "v2pro", character: "new-voice", ref_id: "r", sentences: [], candidates_per_sentence: 1, combos: [combo], best: combo)
        let character = CharacterInfo(slug: "new-voice", source: "user", default_ref: "r", engines: [:])
        let fixture = WorkflowFixture(responses: ["GET /models":try encoded(ModelsResponse(characters: [character])), "GET /train/j/eval":try encoded(evaluation)])
        let store = store(fixture, defaults: defaults)
        XCTAssertTrue(store.models.isEmpty)
        try await store.loadEvaluation(job)
        XCTAssertEqual(store.evaluation?.character, "new-voice")
        XCTAssertEqual(fixture.requests.map { $0.url!.path }, ["/models", "/train/j/eval"])
    }
}

extension WorkflowTests {
    private func entry(_ id: String, type: String, source: String = "pretrained", character: String? = nil) -> WeightEntry {
        WeightEntry(id: "v4:\(id)", name: id, engine: "v4", type: type, character: character, source: source, created_at: "2026-10-23T00:00:00", size_bytes: 100, path: "D:/weights/\(id)", default_for: [], is_default: false, evals: [], deletable: source == "user")
    }
    @MainActor func testVisibleWeightsCombineRoleAndBaseAndDoNotPinOldDefaults() async throws {
        let defaults = MemoryDefaults()
        let weights = [entry("g", type: "gpt"), entry("s", type: "sovits")]
        let fixture = WorkflowFixture(responses: ["GET /weights":try encoded(WeightList(total: 2, weights: weights))])
        let store = store(fixture, defaults: defaults)
        store.params.character = "alice"; store.params.engine = "v4"
        store.applyDefaults()
        XCTAssertNil(store.params.gpt)
        XCTAssertNil(store.params.sovits)
        try await store.loadComposerWeights()
        XCTAssertEqual(store.composerWeights.count, 2)
        XCTAssertTrue(store.composerIsZeroShot)
        let queries = fixture.requests.map { URLComponents(url: $0.url!, resolvingAgainstBaseURL: false)!.queryItems! }
        XCTAssertTrue(queries.contains { $0.contains(URLQueryItem(name: "character", value: "alice")) })
        XCTAssertTrue(queries.contains { $0.contains(URLQueryItem(name: "source", value: "pretrained")) })
        XCTAssertTrue(queries.allSatisfy { $0.contains(URLQueryItem(name: "hidden", value: "false")) })
    }
    @MainActor func testAllListSortPreferencesReachTheServerAndTrainingIsFiltered() async throws {
        let defaults = MemoryDefaults()
        defaults.set("title", forKey: "worksSort"); defaults.set("asc", forKey: "worksOrder")
        defaults.set("duration", forKey: "refsSort"); defaults.set("desc", forKey: "refsOrder")
        defaults.set("progress", forKey: "trainingSort"); defaults.set("asc", forKey: "trainingOrder")
        let fixture = WorkflowFixture(responses: ["GET /works":try encoded(WorkList(works: [])), "GET /refs":try encoded(RefList(count: 0, refs: [])), "GET /jobs":try encoded(JobList(jobs: [])), "GET /models":try encoded(ModelsResponse(characters: []))])
        let store = store(fixture, defaults: defaults)
        try await store.loadWorks(); try await store.loadRefs(character: "alice"); try await store.loadTrainingJobs()
        func query(_ path: String) throws -> [String: String] {
            let req = try XCTUnwrap(fixture.requests.first { $0.url!.path == path })
            return Dictionary(uniqueKeysWithValues: URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!.queryItems!.map { ($0.name, $0.value!) })
        }
        XCTAssertEqual(try query("/works")["sort"], "title")
        XCTAssertEqual(try query("/refs")["order"], "desc")
        XCTAssertEqual(try query("/jobs")["sort"], "progress")
        XCTAssertEqual(try query("/jobs")["kind"], "train")
    }
    @MainActor func testWeightPairMetadataAndSortRequests() async throws {
        let defaults = MemoryDefaults()
        let gpt = entry("g", type: "gpt"), sovits = entry("s", type: "sovits")
        let model = CharacterInfo(slug: "alice", source: "builtin", default_ref: "r", engines: [:])
        let fixture = WorkflowFixture(responses: ["GET /weights":try encoded(WeightList(total: 2, weights: [gpt,sovits])), "PATCH /weights/v4:g":try encoded(gpt), "PATCH /weights/v4:s":try encoded(sovits), "GET /models":try encoded(ModelsResponse(characters: [model]))])
        let studio = store(fixture, defaults: defaults)
        let manager = WeightStore(api: studio.api, defaults: defaults, character: "alice", engine: "v4")
        manager.sort = "score"; manager.order = "asc"
        try await manager.load(); try await manager.loadPair(studio: studio)
        XCTAssertTrue(manager.pairReady)
        try await manager.patch(gpt, patch: WeightPatch(display_name: "zz-test-name", note: "note", hidden: false))
        try await manager.setDefaultPair(studio: studio)
        let patches = fixture.requests.filter { $0.httpMethod == "PATCH" }
        XCTAssertEqual(patches.count, 3)
        let bodies = try patches.map { try JSONSerialization.jsonObject(with: XCTUnwrap($0.httpBody)) as! [String: Any] }
        XCTAssertEqual(bodies[0]["display_name"] as? String, "zz-test-name")
        XCTAssertEqual(bodies[1]["character"] as? String, "alice")
        XCTAssertEqual(bodies[2]["is_default"] as? Bool, true)
        XCTAssertEqual(defaults.string(forKey: "weightsSort"), "score")
        manager.pairEngine = "future"
        XCTAssertFalse(manager.pairReady, "引擎改变后旧组合不能提交")
    }
    @MainActor func testClearReviewIsSerializedAndDoesNotResurrectOldTags() async throws {
        let defaults = MemoryDefaults()
        let fixture = WorkflowFixture(responses: [:], echoReviews: true)
        let store = store(fixture, defaults: defaults)
        var item = audio("a", score: 99)
        item.review = Review(rating: 3, verdict: "good", tags: ["旧标签"], note: "旧备注", audio_id: "a", updated: "0")
        store.candidates = [item]
        store.history = AudioPage(total: 1, items: [item])
        let a = Task { try await store.setRating(item, rating: 4) }
        await Task.yield()
        let b = Task { try await store.clearReview(item) }
        await Task.yield()
        let c = Task { try await store.setRating(item, rating: 5) }
        try await a.value; try await b.value; try await c.value
        XCTAssertEqual(fixture.requests.map(\.httpMethod), ["POST", "DELETE", "POST"])
        XCTAssertEqual(store.candidates.first?.review?.rating, 5)
        XCTAssertEqual(store.history.items.first?.review?.tags, [])
        XCTAssertEqual(store.candidates.first?.review?.note, "")
    }
    @MainActor func testEngineCatalogAndWeightDeletePermissionsAreDataDriven() async throws {
        let defaults = MemoryDefaults()
        let engine = EngineInfo(id: "future", version: "future", code: "fork", code_path: "", zero_shot: true, trainable: false, train_method: "", pretrained_gpt: "g", pretrained_sovits: "s", has_july_weights: false)
        let fixture = WorkflowFixture(responses: ["GET /models":try encoded(ModelsResponse(characters: [], engines: [engine]))])
        let studio = store(fixture, defaults: defaults)
        try await studio.reloadModels(resetSelection: false)
        XCTAssertEqual(studio.engines.map(\.id), ["future"])
        XCTAssertTrue(studio.engines.filter(\.trainable).isEmpty)
        XCTAssertTrue(engine.label.contains("零样本"))
        let manager = WeightStore(api: studio.api, defaults: defaults, character: "alice", engine: "future")
        do { try await manager.delete(entry("g", type: "gpt"), studio: studio); XCTFail("底模不可删") }
        catch { XCTAssertTrue(error.localizedDescription.contains("自己添加")) }
        XCTAssertFalse(fixture.requests.contains { $0.httpMethod == "DELETE" })
    }
}

extension WorkflowTests {
    @MainActor func testReferenceSelectionIsSilentUntilExplicitAudition() async throws {
        let defaults = MemoryDefaults()
        let ref = Ref(id: "r1", audio_url: "/refs/r1/audio", character: "alice", text: "", lang: "ja", duration: 1, tags: [])
        let fixture = WorkflowFixture(responses: ["GET /refs":try encoded(RefList(count: 1, refs: [ref]))])
        let studio = store(fixture, defaults: defaults)
        for character in ["alice", "dave", "dave"] {
            studio.resetReferenceAudition()
            try await studio.loadRefs(character: character)
            studio.selectedReferenceID = studio.refs.first?.id
            XCTAssertNil(studio.referenceAudition)
            XCTAssertFalse(studio.audio.playing)
        }
        studio.auditionReference("r1")
        XCTAssertEqual(studio.referenceAudition?.referenceID, "r1")
        let first = studio.referenceAudition
        studio.auditionReference("r1")
        XCTAssertNotEqual(studio.referenceAudition, first, "主动再次点击同一行仍然试听")
        studio.moveReference(offset: 1, visibleIDs: ["r1", "r2"])
        XCTAssertEqual(studio.referenceAudition?.referenceID, "r2")
        studio.resetReferenceAudition()
        XCTAssertNil(studio.referenceAudition)
        XCTAssertTrue(fixture.requests.allSatisfy { $0.url?.path == "/refs" }, "后台选择不下载音频")
    }
}
