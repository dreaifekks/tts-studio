import XCTest
import SwiftUI
@testable import TTSStudio

/// Explicit modes only. Metadata mode does not submit inference or training.
final class Round3AcceptanceTests: XCTestCase {
    private func emit(_ text: String) { FileHandle.standardOutput.write(Data((text + "\n").utf8)) }
    @MainActor private func window(studio: StudioStore) async throws -> NSWindow {
        await studio.loadInitial()
        for _ in 0..<100 {
            if !studio.models.isEmpty, !studio.engines.isEmpty { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        emit("APP_WINDOWS:" + NSApp.windows.map { "\($0.windowNumber):\($0.title):\($0.frame.height)" }.joined(separator: "|"))
        return try XCTUnwrap(NSApp.windows.first { $0.isVisible && $0.frame.height > 650 && $0.level == .normal }, "需要真正的 WindowGroup 进行顶部间距验收")
    }
    @MainActor private func capture(_ name: String, page: StudioPage, studio: StudioStore, window: NSWindow) async throws {
        studio.page = page
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
        try await Task.sleep(for: .seconds(2))
        emit("SCREENSHOT:\(name):\(window.windowNumber)")
        try await Task.sleep(for: .seconds(1))
    }
    @MainActor func testTraining503AndWorksLayout() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "r3-503", "Explicit training-503 acceptance only")
        let studio = StudioStore.shared
        let window = try await window(studio: studio)
        let health: Health = try await studio.api.getWithoutRetry("/health")
        guard let protectedID = health.training_job else { throw XCTSkip("需要正在运行的开发训练任务") }
        let api = APIClient(baseURL: studio.api.baseURL)
        var messages: [String] = []
        api.onStatus = { messages.append($0); studio.status = $0 }
        let request = try api.request("/workers/warmup", method: "POST")
        let retry = Task { try await api.data(request) }
        defer { retry.cancel() }
        for _ in 0..<100 {
            if messages.contains(where: { $0.contains("秒后重试") }) { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        let message = try XCTUnwrap(messages.last { $0.contains(protectedID) && $0.contains("120 秒后重试") })
        emit("TRAINING_503:" + message)
        studio.page = .generate
        studio.status = message
        try await Task.sleep(for: .milliseconds(100))
        emit("SCREENSHOT:training503:\(window.windowNumber)")
        try await Task.sleep(for: .seconds(1))
        retry.cancel()
        do { _ = try await retry.value; XCTFail("停止等待应取消客户端请求") } catch { }
        try await capture("works", page: .works, studio: studio, window: window)
        studio.characterTab = .versions
        try await capture("models", page: .characters, studio: studio, window: window)
        let protected: Job = try await studio.api.get("/jobs/\(protectedID)")
        XCTAssertNotEqual(protected.status, "cancelled")
        emit("PROTECTED_TRAINING_UNTOUCHED:\(protected.status)")
    }
    @MainActor func testMetadataAndLayout() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "r3-metadata", "Explicit metadata acceptance only")
        let studio = StudioStore.shared
        let window = try await window(studio: studio)
        XCTAssertEqual(studio.engines.count, 8)
        let health: Health = try await studio.api.getWithoutRetry("/health")
        guard let protectedID = health.training_job else { throw XCTSkip("需要正在运行的开发训练任务") }
        let before: Job = try await studio.api.get("/jobs/\(protectedID)")
        let manager = studio.weightManager
        let preferences = ["historySort","historyOrder","reviewsSort","reviewsOrder","refsSort","refsOrder","worksSort","worksOrder","trainingSort","trainingOrder","weightsSort","weightsOrder"]
        let oldPreferences = preferences.map { ($0, UserDefaults.standard.object(forKey: $0)) }
        var originalWeight: WeightEntry?
        var defaultIDs: (String, String)?
        var originalReviewItem: AudioItem?
        defer {
            for (key, value) in oldPreferences {
                if let value { UserDefaults.standard.set(value, forKey: key) } else { UserDefaults.standard.removeObject(forKey: key) }
            }
        }
        do {
            manager.character = "dave"; manager.engine = "v2pro"
            manager.source = "july"; manager.type = "gpt"; manager.showHidden = true
            manager.sort = "name"; manager.order = "asc"
            try await manager.load()
            XCTAssertFalse(manager.rows.isEmpty)
            XCTAssertTrue(manager.rows.allSatisfy { $0.character == "dave" && $0.engine == "v2pro" && $0.source == "july" && $0.type == "gpt" })
            let entry = try XCTUnwrap(manager.rows.first)
            originalWeight = entry
            manager.selectedID = entry.id
            try await manager.patch(entry, patch: WeightPatch(display_name: "zz-test-mac-weight", note: "zz-test-mac-note"))
            let renamed: WeightEntry = try await studio.api.get("/weights/\(entry.id)")
            XCTAssertEqual(renamed.display_name, "zz-test-mac-weight")
            try await manager.patch(renamed, patch: WeightPatch(hidden: true))
            let hidden: WeightEntry = try await studio.api.get("/weights/\(entry.id)")
            XCTAssertTrue(hidden.hidden)
            try await manager.patch(hidden, patch: WeightPatch(display_name: entry.display_name ?? "", note: entry.note, hidden: entry.hidden))
            originalWeight = nil
            manager.pairCharacter = "dave"; manager.pairEngine = "v2pro"
            try await studio.reloadModels(resetSelection: false)
            try await manager.loadPair(studio: studio)
            let baseline = try XCTUnwrap(studio.models.first { $0.slug == "dave" }?.engines["v2pro"])
            let oldGPT = try XCTUnwrap(manager.pairRows.first { $0.type == "gpt" && $0.name == baseline.default_gpt })
            let oldSovits = try XCTUnwrap(manager.pairRows.first { $0.type == "sovits" && $0.name == baseline.default_sovits })
            defaultIDs = (oldGPT.id, oldSovits.id)
            manager.gptID = try XCTUnwrap(manager.pairRows.first { $0.type == "gpt" && $0.id != oldGPT.id }).id
            manager.sovitsID = try XCTUnwrap(manager.pairRows.first { $0.type == "sovits" && $0.id != oldSovits.id }).id
            try await manager.setDefaultPair(studio: studio)
            manager.gptID = oldGPT.id; manager.sovitsID = oldSovits.id
            try await manager.setDefaultPair(studio: studio)
            let restored = try XCTUnwrap(studio.models.first { $0.slug == "dave" }?.engines["v2pro"])
            XCTAssertEqual(restored.default_gpt, baseline.default_gpt)
            XCTAssertEqual(restored.default_sovits, baseline.default_sovits)
            defaultIDs = nil
            emit("METADATA_AND_DEFAULTS_RESTORED")

            let page: AudioPage = try await studio.api.get("/history", query: ["character":"alice","reviewed":"false","limit":"1","sort":"created","order":"asc"])
            var audio = try XCTUnwrap(page.items.first)
            XCTAssertNil(audio.review)
            originalReviewItem = audio
            let review: Review = try await studio.api.send("/audio/\(audio.id)/review", body: ReviewIn(rating: 4, tags: ["zz-test-mac-r3"], note: "zz-test-mac-r3"))
            audio.review = review
            studio.candidates = [audio]
            try await studio.clearReview(audio)
            let meta: AudioItem = try await studio.api.get("/audio/\(audio.id)/meta")
            XCTAssertNil(meta.review)
            originalReviewItem = nil
            emit("REVIEW_CLEARED")

            UserDefaults.standard.set("duration", forKey: "refsSort"); UserDefaults.standard.set("desc", forKey: "refsOrder")
            try await studio.loadRefs(character: "alice")
            XCTAssertEqual(studio.refs.map(\.duration), studio.refs.map(\.duration).sorted(by: >))
            UserDefaults.standard.set("title", forKey: "worksSort"); UserDefaults.standard.set("asc", forKey: "worksOrder")
            try await studio.loadWorks()
            UserDefaults.standard.set("created", forKey: "trainingSort"); UserDefaults.standard.set("asc", forKey: "trainingOrder")
            try await studio.loadTrainingJobs()
            XCTAssertTrue(studio.trainingJobs.allSatisfy { $0.kind == "train" })
            UserDefaults.standard.set("duration", forKey: "historySort"); UserDefaults.standard.set("asc", forKey: "historyOrder")
            let reviewed: AudioPage = try await studio.api.get("/reviews", query: ["sort":"rating","order":"desc","limit":"100"])
            XCTAssertEqual(reviewed.items.compactMap { $0.review?.rating }, reviewed.items.compactMap { $0.review?.rating }.sorted(by: >))
            emit("LIST_SORTS_VERIFIED")
            for page in StudioPage.allCases {
                if page == .characters { studio.characterTab = .versions; manager.source = ""; manager.type = ""; manager.showHidden = false; try await manager.load() }
                try await capture(page.captureName, page: page, studio: studio, window: window)
            }
            let after: Job = try await studio.api.get("/jobs/\(protectedID)")
            XCTAssertFalse(after.status == "cancelled" && before.status != "cancelled")
            emit("PROTECTED_TRAINING_UNTOUCHED:\(after.status)")
        } catch {
            if let item = originalReviewItem { _ = try? await studio.api.data(studio.api.request("/audio/\(item.id)/review", method: "DELETE")) }
            if let originalWeight {
                let _: WeightEntry = try await studio.api.send("/weights/\(originalWeight.id)", method: "PATCH", body: WeightPatch(display_name: originalWeight.display_name ?? "", note: originalWeight.note, hidden: originalWeight.hidden))
            }
            if let defaultIDs { manager.gptID = defaultIDs.0; manager.sovitsID = defaultIDs.1; try await manager.setDefaultPair(studio: studio) }
            throw error
        }
    }
}
extension StudioPage {
    var captureName: String {
        switch self {
        case .generate: "generate"
        case .works: "works"
        case .characters: "characters"
        case .training: "training"
        }
    }
}

extension Round3AcceptanceTests {
    @MainActor func testGPUAfterUserTraining() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "r3-gpu", "Explicit post-training GPU acceptance only")
        let studio = StudioStore.shared
        let window = try await window(studio: studio)
        let health: Health = try await studio.api.getWithoutRetry("/health")
        try XCTSkipUnless(health.training_job == nil, "用户仍在训练，GPU 验收暂不运行")
        studio.audio.volume = 0.2
        defer { studio.audio.stop() }
        let manager = studio.weightManager
        var temporaryRole: String?
        do {
            for engineID in ["v2proplus", "v4"] {
                let engine = try XCTUnwrap(studio.engines.first { $0.id == engineID && $0.zero_shot })
                studio.params.character = "alice"; studio.params.engine = engine.id
                studio.applyDefaults()
                try await studio.loadComposerWeights()
                studio.params.gpt = engine.pretrained_gpt
                studio.params.sovits = engine.pretrained_sovits
                studio.params.text = "おはよう。今日はいい天気だね。"
                studio.params.n = 2; studio.params.debug = true
                try await studio.generate()
                XCTAssertEqual(studio.candidates.count, 2)
                XCTAssertTrue(studio.candidates.allSatisfy { $0.engine == engine.id && $0.gpt == engine.pretrained_gpt && $0.sovits == engine.pretrained_sovits })
                let first = try XCTUnwrap(studio.candidates.first)
                try await studio.play(first)
                try await Task.sleep(for: .seconds(1))
                studio.audio.stop()
                emit("ZERO_SHOT_VERIFIED:\(engine.id)")
                try await capture("generate-\(engine.id)", page: .generate, studio: studio, window: window)
                try await capture("generate", page: .generate, studio: studio, window: window)
            }
            manager.character = ""; manager.engine = "v4"; manager.source = "pretrained"; manager.type = ""
            manager.pairCharacter = "alice"; manager.pairEngine = "v4"
            try await manager.load(); try await manager.loadPair(studio: studio)
            let engine = try XCTUnwrap(studio.engines.first { $0.id == "v4" })
            manager.gptID = try XCTUnwrap(manager.pairRows.first { $0.name == engine.pretrained_gpt && $0.type == "gpt" }).id
            manager.sovitsID = try XCTUnwrap(manager.pairRows.first { $0.name == engine.pretrained_sovits && $0.type == "sovits" }).id
            studio.characterTab = .versions
            studio.page = .characters
            try await manager.evaluate(studio: studio)
            XCTAssertTrue(manager.comparisons.contains { $0.gpt == engine.pretrained_gpt && $0.sovits == engine.pretrained_sovits && $0.character == "alice" })
            emit("WEIGHT_EVAL_VERIFIED:\(manager.evaluationJobID ?? "")")
            studio.characterTab = .versions
        try await capture("models", page: .characters, studio: studio, window: window)

            // Wait for the development controller to release the training slot.
            var availability: Health = try await studio.api.getWithoutRetry("/health")
            while availability.training_job != nil {
                try await Task.sleep(for: .seconds(30))
                availability = try await studio.api.getWithoutRetry("/health")
            }
            let slug = "zz-test-mac-" + UUID().uuidString.prefix(8).lowercased()
            let dataset = try XCTUnwrap(studio.models.first { $0.slug == "dave" }?.dataset_id)
            temporaryRole = slug
            let request = TrainRequest(slug: slug, dataset_id: dataset, max_lines: 10, engine: "v2pro", sovits_epochs: 1, sovits_batch: 1, sovits_save_every: 1, gpt_epochs: 1, gpt_batch: 1, gpt_save_every: 1)
            let job: Job = try await studio.api.send("/train", body: request)
            emit("TEMP_TRAIN_STARTED:\(job.id):\(slug)")
            try await Task.sleep(for: .seconds(1))
            let temporaryHealth: Health = try await studio.api.getWithoutRetry("/health")
            if temporaryHealth.training_job != nil {
                let warmupRequest = try studio.api.request("/workers/warmup", method: "POST")
                let wait = Task { try await studio.api.data(warmupRequest) }
                for _ in 0..<100 {
                    if studio.waitingStatus?.contains("秒后重试") == true { break }
                    try await Task.sleep(for: .milliseconds(100))
                }
                XCTAssertTrue(studio.waitingStatus?.contains("正在训练") == true)
                emit("TRAINING_503:" + (studio.waitingStatus ?? ""))
                await studio.refreshConnection()
                try await studio.loadTrainingJobs()
                try await capture("training503", page: .training, studio: studio, window: window)
                wait.cancel()
                _ = try? await wait.value
            }
            _ = try await studio.follow(job)
            try await studio.reloadModels(resetSelection: false)
            manager.character = slug; manager.engine = "v2pro"; manager.source = "user"; manager.showHidden = true
            manager.pairCharacter = slug; manager.pairEngine = "v2pro"
            try await manager.load(); try await manager.loadPair(studio: studio)
            let temporaryWeights = manager.rows
            XCTAssertFalse(temporaryWeights.isEmpty)
            XCTAssertTrue(temporaryWeights.allSatisfy { $0.source == "user" && $0.character == slug && $0.deletable })
            for entry in temporaryWeights {
                try await manager.delete(entry, studio: studio)
                emit("TEMP_WEIGHT_DELETED:\(entry.id):\(manager.notice ?? "")")
            }
            XCTAssertTrue(manager.rows.isEmpty)
            _ = try await studio.api.data(studio.api.request("/characters/\(slug)", method: "DELETE"))
            temporaryRole = nil
            try await studio.reloadModels(resetSelection: false)
            XCTAssertFalse(studio.models.contains { $0.slug == slug })
            emit("TEMP_ROLE_DELETED:\(slug)")
            manager.character = ""; manager.engine = "v4"; manager.source = "pretrained"; manager.showHidden = false
            manager.pairCharacter = "alice"; manager.pairEngine = "v4"
            try await manager.load(); try await manager.loadPair(studio: studio)
            studio.characterTab = .versions
        try await capture("models", page: .characters, studio: studio, window: window)
        } catch {
            if let slug = temporaryRole {
                do { _ = try await studio.api.data(studio.api.request("/characters/\(slug)", method: "DELETE")); emit("TEMP_ROLE_CLEANED_AFTER_FAILURE:\(slug)") }
                catch { XCTFail("临时角色清理失败：\(slug)：\(error)") }
            }
            throw error
        }
    }
}

extension Round3AcceptanceTests {
    @MainActor func testWeightMetadataClearAndRestore() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "weight-clear", "Explicit metadata-clear acceptance only")
        let defaults = MemoryDefaults()
        let api = APIClient(baseURL: "http://fixture")
        let manager = WeightStore(api: api, defaults: defaults, character: "dave", engine: "v2pro")
        manager.source = "july"; manager.type = "gpt"
        try await manager.load()
        let original = try XCTUnwrap(manager.rows.first)
        do {
            try await manager.patch(original, patch: WeightPatch(display_name: "zz-test-mac-clear", note: "zz-test-mac-clear"))
            let renamed: WeightEntry = try await api.get("/weights/\(original.id)")
            XCTAssertEqual(renamed.display_name, "zz-test-mac-clear")
            XCTAssertEqual(renamed.note, "zz-test-mac-clear")
            try await manager.patch(renamed, patch: WeightPatch(display_name: "", note: ""))
            let cleared: WeightEntry = try await api.get("/weights/\(original.id)")
            XCTAssertEqual(cleared.display_name ?? "", "")
            XCTAssertEqual(cleared.note, "")
            XCTAssertEqual(manager.rows.first { $0.id == original.id }?.label, original.name)
            print("WEIGHT_METADATA_CLEARED_TO_FILENAME:\(original.id):\(cleared.label)")
        } catch {
            try await manager.patch(original, patch: WeightPatch(display_name: original.display_name ?? "", note: original.note))
            throw error
        }
        try await manager.patch(original, patch: WeightPatch(display_name: original.display_name ?? "", note: original.note))
        let restored: WeightEntry = try await api.get("/weights/\(original.id)")
        XCTAssertEqual(restored.display_name ?? "", original.display_name ?? "")
        XCTAssertEqual(restored.note, original.note)
        print("WEIGHT_METADATA_RESTORED:\(original.id)")
    }
}

extension Round3AcceptanceTests {
    @MainActor func testRound4FreshReleaseLayoutAndAudition() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "round4", "Explicit round-four fresh Release acceptance only")
        let studio = StudioStore.shared
        let window = try await window(studio: studio)
        emit("FRESH_RELEASE_PID:\(ProcessInfo.processInfo.processIdentifier):\(Bundle.main.bundlePath)")
        XCTAssertEqual(studio.page, .generate)
        XCTAssertNil(studio.work)
        studio.audio.volume = 0.15
        try await capture("cold-launch", page: .generate, studio: studio, window: window)
        try await studio.loadWorks()
        XCTAssertTrue(studio.works.isEmpty, "只核验真实空列表，不删除任何用户作品")
        try await capture("works-empty", page: .works, studio: studio, window: window)
        XCTAssertNil(studio.work)
        var temporaryWorkID: String?
        var tagged: Ref?
        do {
            let work: Work = try await studio.api.send("/works", body: WorkCreate(title: "zz-test-round4-layout", lines: []))
            temporaryWorkID = work.id
            try await studio.loadWorks()
            XCTAssertNil(studio.work)
            try await capture("works-unselected", page: .works, studio: studio, window: window)
            try await studio.openWork(work.id)
            try await capture("works-selected", page: .works, studio: studio, window: window)
            _ = try await studio.api.data(studio.api.request("/works/\(work.id)", method: "DELETE"))
            temporaryWorkID = nil
            studio.work = nil
            try await studio.loadWorks()
            for page in StudioPage.allCases {
                let names: [StudioPage:String] = [.generate:"generate", .works:"works", .characters:"characters", .training:"training"]
                if page == .characters { studio.characterTab = .references }
                try await capture(names[page]!, page: page, studio: studio, window: window)
                if page == .characters {
                    XCTAssertFalse(studio.audio.playing)
                    XCTAssertNil(studio.referenceAudition)
                    emit("REFERENCE_ENTRY_SILENT")
                    let refs = Array(studio.refs.filter { $0.duration > 2 }.prefix(3))
                    XCTAssertEqual(refs.count, 3)
                    studio.auditionReference(refs[0].id)
                    try await awaitReference(refs[0].id, studio: studio)
                    try await capture("reference-active", page: .characters, studio: studio, window: window)
                    studio.moveReference(offset: 1, visibleIDs: refs.map(\.id))
                    try await awaitReference(refs[1].id, studio: studio)
                    tagged = refs[1]
                    await studio.tagAndAdvance("zz-test", visibleIDs: refs.map(\.id))?.value
                    try await awaitReference(refs[2].id, studio: studio)
                    emit("REFERENCE_ACTIVE_AND_TAG_ADVANCE_PLAYED")
                    try await studio.tag(refs[1], tags: refs[1].tags)
                    tagged = nil
                    studio.resetReferenceAudition()
                    try await studio.loadRefs(character: "dave")
                    studio.selectedReferenceID = studio.refs.first?.id
                    try await Task.sleep(for: .seconds(1))
                    XCTAssertFalse(studio.audio.playing)
                    XCTAssertNil(studio.referenceAudition)
                    studio.resetReferenceAudition()
                    try await studio.loadRefs(character: "alice")
                    studio.selectedReferenceID = studio.refs.first?.id
                    try await Task.sleep(for: .seconds(1))
                    XCTAssertFalse(studio.audio.playing)
                    emit("REFERENCE_ROLE_AND_REFRESH_SILENT")
                    try await capture("reference-refreshed-silent", page: .characters, studio: studio, window: window)
                }
            }
            emit("ROUND4_COMPLETE_TEST_DATA_REMOVED")
        } catch {
            if let tagged { try await studio.tag(tagged, tags: tagged.tags) }
            if let temporaryWorkID { _ = try await studio.api.data(studio.api.request("/works/\(temporaryWorkID)", method: "DELETE")) }
            throw error
        }
    }
    @MainActor private func awaitReference(_ id: String, studio: StudioStore) async throws {
        for _ in 0..<100 {
            if studio.audio.currentID == "ref-" + id && studio.audio.playing { return }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTFail("主动操作应播放 \(id)")
    }
}
