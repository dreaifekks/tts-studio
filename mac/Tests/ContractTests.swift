import XCTest
@testable import TTSStudio

final class MemoryDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    init() { super.init(suiteName: "tts-studio-memory-only")! }
    override func object(forKey key: String) -> Any? { values[key] }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func stringArray(forKey key: String) -> [String]? { values[key] as? [String] }
    override func dictionary(forKey key: String) -> [String: Any]? { values[key] as? [String: Any] }
}

final class ContractTests: XCTestCase {
    func testSSEMultilineAndKeepalive() throws {
        var parser = SSEParser()
        XCTAssertNil(parser.consume(": keepalive"))
        XCTAssertNil(parser.consume(""))
        XCTAssertNil(parser.consume("event: job"))
        XCTAssertNil(parser.consume("data: {\"status\":"))
        XCTAssertNil(parser.consume("data: \"running\"}"))
        let data = try XCTUnwrap(parser.consume(""))
        let json = try JSONSerialization.jsonObject(with: data) as? [String: String]
        XCTAssertEqual(json?["status"], "running")
        XCTAssertNil(parser.consume(""))
    }
    @MainActor func testRetryAfterAndValidationErrors() throws {
        XCTAssertEqual(APIClient.retryDelay("120"), 120)
        XCTAssertEqual(APIClient.retryDelay("Thu, 01 Jan 1970 00:02:00 GMT", now: Date(timeIntervalSince1970: 0)), 120)
        let data = Data(#"{"detail":[{"loc":["body","n"],"msg":"必须在 1–16 之间"}]}"#.utf8)
        XCTAssertEqual(APIClient.error(data, status: 422).message, "body.n：必须在 1–16 之间")
    }
    @MainActor func testRelativeURLsAndTailnetAddress() throws {
        let api = APIClient(baseURL: "http://fixture")
        XCTAssertEqual(try api.url("/works/w-ab/files/a.wav").absoluteString, "http://fixture" + "/works/w-ab/files/a.wav")
        let url = try api.url("/refs", query: ["emotion":"开心", "character":"alice"])
        XCTAssertTrue(url.absoluteString.contains("character=alice"))
        XCTAssertEqual(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "emotion" }?.value, "开心")
    }
    func testDraftWorkIDIsOmittedButSavedIDsPersist() throws {
        let newLine = WorkLine(id: "draft-ab", text: "おはよう")
        XCTAssertNil(newLine.input.id)
        let existingLine = WorkLine(id: "l-ab", text: "おはよう")
        XCTAssertEqual(existingLine.input.id, "l-ab")
        let encoded = try JSONEncoder().encode(newLine.input)
        let json = try JSONSerialization.jsonObject(with: encoded) as! [String: Any]
        XCTAssertNil(json["id"])
        XCTAssertNil(json["candidates"])
    }
    @MainActor func testFailedInitialJobIsNotResubmitted() async {
        let api = APIClient(baseURL: "http://invalid.invalid")
        let failed = Job(id: "candidates-test", kind: "candidates", target: "alice", status: "failed", progress: 0, stage: "失败", created: "", error: "GPU 故障")
        do { _ = try await api.follow(failed) { _ in }; XCTFail("Expected failed job") }
        catch { XCTAssertEqual(error.localizedDescription, "GPU 故障") }
    }
}

extension ContractTests {
    func testRawSSEByteFramingPreservesBlankLinesAndUTF8() throws {
        let event = ": keepalive\r\n\r\nevent: job\r\ndata: {\"stage\":\"合成中\",\r\ndata: \"progress\":0.5}\r\n\r\ndata: {\"stage\":\"完成\"}\n\n"
        var parser = SSEParser()
        let messages = event.utf8.compactMap { parser.consume(byte: $0) }
        XCTAssertEqual(messages.count, 2)
        let first = try JSONSerialization.jsonObject(with: messages[0]) as! [String: Any]
        XCTAssertEqual(first["stage"] as? String, "合成中")
        XCTAssertEqual(first["progress"] as? Double, 0.5)
    }
    @MainActor func testEmotionAndClearedReferenceInvalidateAudioButPauseDoesNot() {
        let defaults = MemoryDefaults()
        let store = StudioStore(defaults: defaults)
        let line = WorkLine(ref_id: "ref-a", emotion: "开心", id: "l-test", text: "你好")
        store.work = Work(id: "w-test", title: "test", created: "", updated: "", lines: [line])
        store.staleLines = [:]
        var edited = line
        edited.pause_after = 1
        store.changeLine(edited)
        XCTAssertFalse(store.staleLines["w-test"]?.contains("l-test") ?? false)
        edited.emotion = "生气"
        store.changeLine(edited)
        XCTAssertTrue(store.staleLines["w-test"]?.contains("l-test") == true)
        store.staleLines = [:]
        edited.ref_id = nil
        store.changeLine(edited)
        XCTAssertTrue(store.staleLines["w-test"]?.contains("l-test") == true)
        store.staleLines.removeValue(forKey: "w-test")
        defaults.set(store.staleLines, forKey: "staleWorkLines")
    }
}

extension ContractTests {
    func testWeightMetadataClearEncodesExplicitEmptyStrings() throws {
        let body = try JSONEncoder().encode(WeightPatch(display_name: "", note: ""))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: String])
        XCTAssertEqual(json, ["display_name":"", "note":""])
    }
}

extension ContractTests {
    @MainActor func testLocalJobsPersistAndOtherDevicesDoNotReplaceStatus() {
        let defaults = MemoryDefaults()
        let store = StudioStore(defaults: defaults)
        store.recordLocalJob("local")
        store.status = "本机操作"
        store.updateJob(Job(id: "other", kind: "blind", target: "test", status: "running", progress: 0.5, stage: "其他设备生成中", created: ""))
        XCTAssertEqual(store.status, "本机操作")
        XCTAssertTrue(store.localActiveJobs.isEmpty)
        XCTAssertEqual(store.otherActiveJobs.map(\.id), ["other"])
        store.updateJob(Job(id: "local", kind: "candidates", target: "test", status: "running", progress: 0.5, stage: "本机生成中", created: ""))
        XCTAssertEqual(store.status, "本机生成中")
        let restored = StudioStore(defaults: defaults)
        restored.jobs = store.jobs
        XCTAssertEqual(restored.localActiveJobs.map(\.id), ["local"])
        XCTAssertEqual(restored.otherActiveJobs.map(\.id), ["other"])
    }
}
