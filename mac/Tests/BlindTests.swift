import XCTest
@testable import TTSStudio

final class BlindTests: XCTestCase {
    func testHiddenCombosDecodeForBothServerContracts() throws {
        let combo: [String: Any] = ["engine":"v2pro", "gpt":"secret-gpt", "sovits":"secret-sovits"]
        for side in [NSNull() as Any, combo as Any] {
            let value: [String: Any] = ["id":"b-null", "title":"alice v2pro:secret-gpt+secret-sovits vs v2pro:secret-gpt+secret-sovits", "created":"2026-10-24T15:00:00", "character":"alice", "eval_set":"mini", "repeats":1, "ref_id":"r", "a":side, "b":side, "status":"ready", "job_id":"j", "n_questions":1, "n_ready":1, "n_voted":0, "revealed":false, "questions":[["id":"q01", "index":0, "text":"hello", "tests":[], "ready":true, "note":""]]]
            let detail = try JSONDecoder().decode(BlindTest.self, from: JSONSerialization.data(withJSONObject: value))
            let list = try JSONDecoder().decode(BlindList.self, from: JSONSerialization.data(withJSONObject: ["total":1,"tests":[value]]))
            XCTAssertEqual(detail.questions.first?.id, "q01")
            XCTAssertFalse(detail.revealed)
            XCTAssertEqual(detail.a == nil, side is NSNull)
            XCTAssertEqual(detail.b == nil, side is NSNull)
            XCTAssertEqual(list.tests.first?.a == nil, side is NSNull)
            XCTAssertEqual(list.tests.first?.b == nil, side is NSNull)
            XCTAssertEqual(blindTitle(detail.title, character: detail.character, evalSet: detail.eval_set, created: detail.created), "alice · 快速评测 · 2026-10-24")
        }
    }
    func testBlindTitlesPreserveNewAutomaticAndCustomFormats() {
        let created = "2026-10-24T15:00:00"
        for title in ["Alice · mini · 2026-10-24 15:00", "我选 A vs B", "自定义盲听"] {
            XCTAssertEqual(blindTitle(title, character: "alice", evalSet: "mini", created: created), title)
        }
        XCTAssertEqual(blindTitle("", character: "alice", evalSet: "mini", created: created), "alice · 快速评测 · 2026-10-24")
        XCTAssertEqual(blindTitle("alice · mini · 2026-10-24 15:00", character: "alice", evalSet: "mini", created: created), "alice · 快速评测 · 2026-10-24")
        XCTAssertEqual(blindTitle("alice · mini · 我的标题", character: "alice", evalSet: "mini", created: created), "alice · mini · 我的标题")
    }
    @MainActor func testPollingContinuesAfterTransientFailureAndStopsOnCancellation() async {
        let store = BlindStore(api: APIClient(baseURL: "http://fixture"))
        var attempts = 0
        var errors = 0
        await store.poll(every: .milliseconds(1), refresh: {
            attempts += 1
            if attempts == 1 { throw URLError(.networkConnectionLost) }
            throw CancellationError()
        }, report: { _ in errors += 1 })
        XCTAssertEqual(attempts, 2)
        XCTAssertEqual(errors, 1)
    }

    @MainActor func testNewJobKindsAndConflictDetail() throws {
        for kind in ["blind", "eval"] {
            let data = try JSONEncoder().encode(Job(id: "j-test", kind: kind, target: "b-test", status: "queued", progress: 0, stage: "", created: ""))
            let job = try JSONDecoder().decode(Job.self, from: data)
            XCTAssertEqual(job.kindLabel, kind == "blind" ? "盲听生成" : "组合评测")
        }
        XCTAssertEqual(APIClient.error(Data(#"{"detail":"盲听生成期间不能删除权重"}"#.utf8), status: 409).message, "盲听生成期间不能删除权重")
    }
    @MainActor func testNextUnvotedWrapsAndKeepsUnreadyQuestions() {
        let questions = [BlindQuestion(id: "q0", index: 0, text: "a", tests: [], ready: true), BlindQuestion(id: "q1", index: 1, text: "b", tests: [], ready: true, vote: "left"), BlindQuestion(id: "q2", index: 2, text: "c", tests: [], ready: false)]
        XCTAssertEqual(BlindStore.nextUnvoted(in: questions, after: 1)?.id, "q2")
        XCTAssertEqual(BlindStore.nextUnvoted(in: questions, after: 2)?.id, "q0")
        var voted = questions
        for i in voted.indices { voted[i].vote = "tie" }
        XCTAssertNil(BlindStore.nextUnvoted(in: voted, after: 0))
    }
    @MainActor func testComparisonsNeverMixEvaluationSetsAndKeepLatest() {
        let manager = WeightStore(api: APIClient(baseURL: "http://fixture"), character: "alice", engine: "v2pro")
        let mini = ComboScore(gpt: "g", sovits: "s", n: 3, cer_median: 0, spk_sim_median: 1, score_median: 99, eval_set: "mini", character: "alice", ref_id: "r", job_id: "mini", evaluated_at: "2026-10-23")
        var hard = mini
        hard.eval_set = "hard"; hard.score_median = 80; hard.evaluated_at = "2026-10-21"
        var latest = hard
        latest.score_median = 85; latest.evaluated_at = "2026-10-22"
        let entry = WeightEntry(id: "w", name: "g", engine: "v2pro", type: "gpt", source: "user", created_at: "", size_bytes: 1, path: "", hidden: false, default_for: [], is_default: false, evals: [mini, hard, latest], deletable: true)
        manager.pairRows = [entry]
        XCTAssertEqual(manager.comparisons.map(\.score_median), [85])
        manager.evalSet = "mini"
        XCTAssertTrue(manager.comparisons.isEmpty)
        manager.pairRows = [entry]
        XCTAssertEqual(manager.comparisons.map(\.score_median), [99])
    }
}

extension BlindTests {
    /// Opt-in real gateway/Store/audio workflow. Does not claim physical UI interaction.
    @MainActor func testLiveBlindWorkflow() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "blind", "Explicit real blind workflow only")
        let defaults = MemoryDefaults()
        let studio = StudioStore(defaults: defaults)
        let manager = studio.blindManager
        studio.audio.volume = 0.15
        defer { manager.stopAudio(studio: studio) }
        try await studio.reloadModels(resetSelection: false)
        try await manager.prepare()
        let alice = try XCTUnwrap(studio.models.first { $0.slug == "alice" })
        let a = try XCTUnwrap(alice.engines["v2pro"])
        let b = try XCTUnwrap(alice.engines["v2proplus"])
        let title = "zz-test-mac-blind-\(Int(Date().timeIntervalSince1970))"
        try await manager.create(BlindCreate(character: "alice", a: BlindSide(engine: "v2pro", gpt: a.default_gpt, sovits: a.default_sovits), b: BlindSide(engine: "v2proplus", gpt: b.default_gpt, sovits: b.default_sovits), eval_set: "mini", repeats: 1, title: title))
        let id = try XCTUnwrap(manager.selectedID)
        print("BLIND_CREATED:\(id):\(title)")
        do {
            XCTAssertEqual(manager.test?.n_questions, 3)
            XCTAssertFalse(studio.audio.playing)
            var observedPartial = false
            for _ in 0..<180 {
                try await manager.refresh()
                let value = try XCTUnwrap(manager.test)
                print("BLIND_PROGRESS:\(value.n_ready)/\(value.n_questions):\(value.status)")
                if value.n_ready > 0 && value.n_ready < value.n_questions { observedPartial = true }
                if value.status == "failed" { XCTFail("盲听生成失败"); break }
                if value.n_ready == value.n_questions { break }
                try await Task.sleep(for: .seconds(2))
            }
            let test = try XCTUnwrap(manager.test)
            XCTAssertEqual(test.n_ready, 3)
            XCTAssertFalse(studio.audio.playing, "生成完成不能自动播放")
            XCTAssertNil(manager.result)
            for side in ["left", "right"] {
                let first = try XCTUnwrap(manager.question)
                let path = try XCTUnwrap(side == "left" ? first.left_url : first.right_url)
                manager.play(path, identity: "blind-live-\(side)", studio: studio)
                for _ in 0..<100 {
                    if studio.audio.playing { break }
                    try await Task.sleep(for: .milliseconds(50))
                }
                XCTAssertTrue(studio.audio.playing, "AVFoundation 必须实际进入播放状态")
                print("BLIND_PLAYED:\(side)")
                try await Task.sleep(for: .milliseconds(200))
                manager.stopAudio(studio: studio)
            }
            let first = test.questions[0].id
            try await manager.vote(question: first, choice: "left", note: "zz-test-left")
            XCTAssertEqual(manager.questionID, test.questions[1].id)
            XCTAssertFalse(studio.audio.playing)
            manager.move(-1)
            XCTAssertEqual(manager.questionID, first)
            XCTAssertEqual(manager.question?.vote, "left")
            try await manager.vote(question: first, choice: "right", note: "zz-test-change")
            XCTAssertEqual(manager.test?.questions[0].vote, "right")
            XCTAssertEqual(manager.test?.n_voted, 1)
            XCTAssertNil(manager.result)
            for question in test.questions.dropFirst() {
                try await manager.vote(question: question.id, choice: "tie", note: "zz-test-tie")
            }
            XCTAssertNotNil(manager.result, "全部投完必须自动进入结果")
            XCTAssertEqual(manager.result?.n_voted, 3)
            try await manager.reveal()
            XCTAssertEqual(manager.result?.a.ties, 2)
            try await manager.vote(question: first, choice: "left", note: "zz-test-result-change")
            XCTAssertEqual(manager.result?.questions[0].vote, "left")
            XCTAssertEqual(manager.result?.questions[0].note, "zz-test-result-change")
            try await manager.loadList()
            XCTAssertTrue(manager.rows.contains { $0.id == id && $0.revealed })
            XCTAssertNotNil(manager.revealedResults[id])
            let weights = studio.weightManager
            for set in ["hard", "mini"] {
                weights.evalSet = set
                try await weights.load()
                try await weights.loadPair(studio: studio)
                XCTAssertTrue(weights.rows.allSatisfy { $0.best_eval == nil || $0.best_eval?.eval_set == set })
                XCTAssertTrue(weights.comparisons.allSatisfy { $0.eval_set == set })
                print("WEIGHT_EVAL_SET:\(set):\(weights.total)")
            }
            try await manager.delete(id)
            XCTAssertFalse(manager.rows.contains { $0.id == id })
            print("BLIND_DELETED:\(id):PARTIAL_OBSERVED=\(observedPartial)")
        } catch {
            try await manager.delete(id)
            throw error
        }
        let remaining: BlindList = try await studio.api.get("/blind", query: ["limit":"500"])
        XCTAssertFalse(remaining.tests.contains { $0.title.hasPrefix("zz-test-") }, "GET /blind 不得残留 zz-test 盲听")
        print("BLIND_ZZ_TEST_REMAINDER:\(remaining.tests.filter { $0.title.hasPrefix("zz-test-") }.count)")
    }
}
