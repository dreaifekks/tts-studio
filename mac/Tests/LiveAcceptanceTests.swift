import XCTest
import SwiftUI
import AVFoundation
@testable import TTSStudio

/// Opt-in sandboxed app acceptance. Uses zz-test resources and always restores them.
/// Does not write reviews or submit training.
final class LiveAcceptanceTests: XCTestCase {
    @MainActor func testLiveWorkflow() async throws {
        let mode = ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] ?? ""
        try XCTSkipUnless(["1", "stats", "pick", "training"].contains(mode), "Run explicitly with TTS_LIVE_ACCEPTANCE=1")
        let store = StudioStore.shared
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1320, height: 860), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "TTS Studio · 真实网关验收"
        window.contentViewController = NSHostingController(rootView: ContentView().environment(store))
        window.makeKeyAndOrderFront(nil)
        defer { window.orderOut(nil); store.audio.stop() }
        store.audio.volume = 0.2
        await store.loadInitial()
        for _ in 0..<30 {
            if !store.models.isEmpty { break }
            try await Task.sleep(for: .seconds(1))
        }
        if mode == "stats" {
            store.stats = try await store.api.get("/reviews/stats")
            store.characterTab = .stats
            await phase("stats", store: store, page: .characters, window: window)
            return
        }
        if mode == "training" {
            try await store.loadDatasets()
            try await store.loadTrainingJobs()
            await phase("training", store: store, page: .training, window: window)
            return
        }
        if mode == "pick" {
            try await pickRegression(store: store, window: window)
            return
        }
        let health: Health = try await store.api.getWithoutRetry("/health")
        try XCTSkipUnless(health.training_job == nil, "GPU 正在训练，本轮不运行生成验收")
        XCTAssertFalse(store.models.isEmpty)
        var originalRefs: [Ref] = []
        var createdWorkID: String?
        do {
            store.params.character = "alice"
            store.params.engine = "v2pro"
            store.applyDefaults()
            store.params.text = "おはよう。今日はいい天気だね。"
            store.params.debug = true
            store.params.n = 6
            let generation = Task { try await store.generate() }
            var observedCounts: [Int] = []
            var capturedProgress = false
            for _ in 0..<1800 {
                let count = store.candidates.count
                if count > 0 && observedCounts.last != count {
                    observedCounts.append(count)
                    emit("PROGRESS:\(count)/6")
                    XCTAssertEqual(store.candidates.map { $0.score ?? -.infinity }, store.candidates.map { $0.score ?? -.infinity }.sorted(by: >))
                    if count < 6 && !capturedProgress {
                        emit("SCREENSHOT:generateprogress:\(window.windowNumber)")
                        capturedProgress = true
                    }
                }
                if count == 6 && !store.generationBusy { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            _ = try await generation.value
            XCTAssertTrue(capturedProgress, "必须在任务完成前看到增量候选，不能靠终态 GET 冒充 SSE")
            XCTAssertEqual(store.candidates.count, 6)
            emit("INCREMENTAL_COUNTS:\(observedCounts)")
            let first = try XCTUnwrap(store.candidates.first)
            let second = store.candidates[1]
            try await store.play(first)
            XCTAssertTrue(store.audio.playing)
            try await Task.sleep(for: .seconds(1))
            store.audio.stop()
            await phase("generate", store: store, page: .generate, window: window)

            let debugWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 800), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            debugWindow.title = "TTS Studio · Debug 验收"
            debugWindow.contentViewController = NSHostingController(rootView: DebugView(item: first).environment(store))
            debugWindow.makeKeyAndOrderFront(nil)
            try await Task.sleep(for: .seconds(2))
            emit("SCREENSHOT:debug:\(debugWindow.windowNumber)")
            try await Task.sleep(for: .seconds(2))
            debugWindow.orderOut(nil)

            store.characterTab = .references
            await phase("references", store: store, page: .characters, window: window)
            originalRefs = Array(store.refs.prefix(3))
            XCTAssertEqual(originalRefs.count, 3)
            for ref in originalRefs {
                store.selectedReferenceID = ref.id
                try await store.playReference(ref)
                XCTAssertTrue(store.audio.playing)
                let task = store.tagAndAdvance("zz-test", visibleIDs: store.refs.map(\.id))
                await task?.value
                XCTAssertTrue(store.refs.first(where: { $0.id == ref.id })?.tags.contains("zz-test") == true)
                if let next = store.refs.first(where: { $0.id == store.selectedReferenceID }) {
                    for _ in 0..<50 {
                        if store.audio.currentID == "ref-" + next.id { break }
                        try await Task.sleep(for: .milliseconds(100))
                    }
                    XCTAssertEqual(store.audio.currentID, "ref-" + next.id, "打标后下一条应自动试听")
                }
            }
            await phase("references", store: store, page: .characters, window: window)
            for ref in originalRefs { try await store.tag(ref, tags: ref.tags) }
            originalRefs = []
            emit("TAGS_RESTORED")
            store.audio.stop()

            store.history = try await store.api.get("/history", query: ["character":"alice", "limit":"30"])
            XCTAssertTrue(store.candidates.allSatisfy { c in store.history.items.contains { $0.id == c.id } })
            store.generateRightTab = .history
            await phase("history", store: store, page: .generate, window: window)
            store.stats = try await store.api.get("/reviews/stats")
            store.characterTab = .stats
            await phase("stats", store: store, page: .characters, window: window)

            try await store.createWork(title: "zz-test-tts-studio-\(UUID().uuidString.prefix(8))")
            createdWorkID = store.work?.id
            store.work?.title += "-saved-edit"
            store.workDirty = true
            try await store.addCandidateToWork(first, workID: createdWorkID)
            XCTAssertTrue(store.work?.title.hasSuffix("-saved-edit") == true)
            XCTAssertEqual(store.work?.lines.first?.picked, first.id)
            store.addLine()
            store.work?.lines[1].text = second.text
            try await store.saveWork()
            let lineID = try XCTUnwrap(store.work?.lines[1].id)
            try await store.addCandidateToWork(second, workID: createdWorkID, lineID: lineID)
            XCTAssertEqual(store.work?.lines[1].picked, second.id)
            XCTAssertEqual(store.work?.lines.count, 2)
            emit("IMPORTED_NEW_AND_EXISTING")
            try await store.renderWork()
            let render = try XCTUnwrap(store.work?.render)
            try await store.play(path: render.wav_url, id: render.wav_url)
            XCTAssertTrue(store.audio.playing)
            try await Task.sleep(for: .seconds(1))
            store.audio.stop()
            for (ext, path) in [("wav",render.wav_url),("mp3",render.mp3_url),("srt",render.srt_url),("lrc",render.lrc_url)] {
                let data = try await store.api.data(store.api.request(path))
                XCTAssertGreaterThan(data.count, 0)
                let file = FileManager.default.temporaryDirectory.appendingPathComponent("tts-studio-acceptance.\(ext)")
                try data.write(to: file)
                emit("EXPORT:\(file.path)")
            }
            emit("WORK:\(try XCTUnwrap(createdWorkID))")
            await phase("works", store: store, page: .works, window: window)
            try await store.loadDatasets()
            let dataset = try XCTUnwrap(store.datasets.first)
            let preview: DatasetPreview = try await store.api.post("/datasets/\(dataset.id)/preview", query: ["min_sec":"1","max_sec":"12","exclude_interjections":"true"])
            XCTAssertGreaterThan(preview.lines_kept, 0)
            try await store.loadTrainingJobs()
            await phase("training", store: store, page: .training, window: window)
        } catch {
            await cleanup(store: store, refs: originalRefs, workID: createdWorkID)
            throw error
        }
        await cleanup(store: store, refs: originalRefs, workID: createdWorkID)
        emit("LIVE_ACCEPTANCE_COMPLETE")
    }
    @MainActor private func pickRegression(store: StudioStore, window: NSWindow) async throws {
        let health: Health = try await store.api.getWithoutRetry("/health")
        try XCTSkipUnless(health.training_job == nil, "GPU 正在训练，跳过生成回归")
        let jobs: JobList = try await store.api.get("/jobs", query: ["kind":"candidates", "status":"succeeded", "limit":"1"])
        let old = try XCTUnwrap(jobs.jobs.first?.candidates_result?.candidates.first)
        var workID: String?
        do {
            try await store.createWork(title: "zz-test-old-pick-\(UUID().uuidString.prefix(8))")
            workID = store.work?.id
            try await store.addCandidateToWork(old, workID: workID)
            var edited = try XCTUnwrap(store.work?.lines.first)
            edited.text = "今日は少し寒いね。一緒にお茶を飲もう。"
            store.changeLine(edited)
            try await store.regenerate(edited)
            let line = try XCTUnwrap(store.work?.lines.first)
            XCTAssertFalse(store.needsRegeneration(line), "新一轮生成已解除行级标记")
            XCTAssertTrue(line.candidates.contains { $0.id == old.id }, "服务仍保留旧候选，必须在客户端拒选")
            XCTAssertFalse(store.canPick(old, for: line))
            do { try await store.pick(old, line: line); XCTFail("不能换回旧台词") }
            catch { XCTAssertTrue(error.localizedDescription.contains("旧台词")) }
            emit("OLD_PICK_REJECTED")
            let current = try XCTUnwrap(line.candidates.first { store.canPick($0, for: line) })
            XCTAssertEqual(current.text, edited.text)
            try await store.pick(current, line: line)
            try await store.renderWork()
            let render = try XCTUnwrap(store.work?.render)
            let data = try await store.api.data(store.api.request(render.wav_url))
            XCTAssertGreaterThan(data.count, 0)
            emit("NEW_PICK_RENDERED")
            await phase("works", store: store, page: .works, window: window)
            try await store.loadDatasets()
            try await store.loadTrainingJobs()
            await phase("training", store: store, page: .training, window: window)
        } catch {
            await cleanup(store: store, refs: [], workID: workID)
            throw error
        }
        await cleanup(store: store, refs: [], workID: workID)
    }
    @MainActor private func cleanup(store: StudioStore, refs: [Ref], workID: String?) async {
        for ref in refs {
            do { try await store.tag(ref, tags: ref.tags) }
            catch { XCTFail("恢复 zz-test 标签失败：\(ref.id)：\(error)") }
        }
        if let workID {
            do {
                _ = try await store.api.data(store.api.request("/works/\(workID)", method: "DELETE"))
                store.forgetWorkState(workID)
                store.work = nil
                try await store.loadWorks()
                XCTAssertFalse(store.works.contains { $0.id == workID })
                emit("TEST_WORK_DELETED:\(workID)")
            } catch { XCTFail("删除本轮 zz-test 作品失败：\(workID)：\(error)") }
        }
    }
    @MainActor private func phase(_ name: String, store: StudioStore, page: StudioPage, window: NSWindow) async {
        store.page = page
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
        try? await Task.sleep(for: .seconds(2))
        emit("SCREENSHOT:\(name):\(window.windowNumber)")
        try? await Task.sleep(for: .seconds(2))
    }
    private func emit(_ message: String) { FileHandle.standardOutput.write(Data((message + "\n").utf8)) }
}
