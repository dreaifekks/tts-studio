import XCTest
import SwiftUI
import AppKit
@testable import TTSStudio

private final class ComputeFixture: @unchecked Sendable {
    let lock = NSLock()
    var requests: [URLRequest] = []
    var handler: @Sendable (URLRequest, Int) -> (Int, Data) = { _, _ in (200, Data()) }
    func respond(_ request: URLRequest) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }
        var copy = request
        if copy.httpBody == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data(); var bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(contentsOf: bytes.prefix(n)) }
            copy.httpBody = data
        }
        requests.append(copy)
        return handler(copy, requests.count)
    }
    var count: Int { lock.lock(); defer { lock.unlock() }; return requests.count }
}
private final class ComputeProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var fixture: ComputeFixture!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, data) = Self.fixture.respond(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}
private struct Submitted: Decodable { var id: String }
final class ComputeTests: XCTestCase {
    private static func quote(units: Double = 2.345, device: String = "colab-L4") -> String {
        """
        {"recommended":"colab-L4","chosen":"\(device)","reason_code":"train_needs_gpu","reason":"训练", "candidates":[
          {"device":"colab-L4","name":"云端 L4","type":"cloud","gpu":"L4","seconds":4200,"units":\(units),"speed_source":"reference","available":true,"idle":true,"premium":false},
          {"device":"local","name":"studio-host","type":"local","seconds":32400,"available":true,"idle":true,"premium":false}],
          "confirm":["over_task"],"month_used":151,"ask_over":2,"ask_month":500}
        """
    }
    @MainActor private func api(_ handler: @escaping @Sendable (URLRequest, Int) -> (Int, Data)) -> APIClient {
        let fixture = ComputeFixture(); fixture.handler = handler; ComputeProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [ComputeProtocol.self]
        return APIClient(baseURL: "http://fixture", session: URLSession(configuration: config))
    }
    private func body(_ request: URLRequest) throws -> [String: Any] {
        if let data = request.httpBody { return try JSONSerialization.jsonObject(with: data) as! [String: Any] }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open(); defer { stream.close() }
        var data = Data(); var bytes = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(contentsOf: bytes.prefix(n)) }
        return try JSONSerialization.jsonObject(with: data) as! [String: Any]
    }
    @MainActor private func awaitConfirmation(_ planner: TaskPlanner, units: Double) async throws {
        for _ in 0..<200 {
            if planner.confirmation?.selected?.units == units { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTFail("Expected quote \(units)")
    }
    @MainActor func testTypedConflictKeepsCodeAndFullQuote() throws {
        let data = Data("{\"code\":\"confirm_required\",\"detail\":\"先问\",\"plan\":\(Self.quote())}".utf8)
        let failure = APIClient.error(data, status: 409)
        XCTAssertEqual(failure.code, "confirm_required")
        XCTAssertEqual(failure.plan?.selected?.units, 2.345)
    }
    @MainActor func testRequoteRequiresNewApprovalAndExactBudget() async throws {
        let api = api { _, attempt in
            if attempt < 3 {
                let quote = Self.quote(units: attempt == 1 ? 2.345 : 3.456)
                return (409, Data("{\"code\":\"confirm_required\",\"detail\":\"确认\",\"plan\":\(quote)}".utf8))
            }
            return (200, Data(#"{"id":"created"}"#.utf8))
        }
        let planner = TaskPlanner()
        let operation = Task { () -> Submitted in try await planner.submit(api: api, path: "/train", input: ["slug":"test"]) }
        try await awaitConfirmation(planner, units: 2.345)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        planner.resolve(true)
        try await awaitConfirmation(planner, units: 3.456)
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
        planner.resolve(true)
        let value = try await operation.value
        XCTAssertEqual(value.id, "created")
        let requests = ComputeProtocol.fixture.requests
        XCTAssertNil(try body(requests[0])["approved"])
        XCTAssertEqual(try body(requests[1])["approved_device"] as? String, "colab-L4")
        XCTAssertEqual(try body(requests[1])["approved_units"] as? Double, 2.345)
        XCTAssertEqual(try body(requests[2])["approved_units"] as? Double, 3.456)
        XCTAssertNil(planner.selected)
        XCTAssertNil(planner.confirmation)
    }
    @MainActor func testCancelQuoteDoesNotResubmit() async throws {
        let api = api { _, _ in (409, Data("{\"code\":\"confirm_required\",\"detail\":\"确认\",\"plan\":\(Self.quote())}".utf8)) }
        let planner = TaskPlanner()
        let operation = Task { () -> Submitted in try await planner.submit(api: api, path: "/train", input: ["slug":"test"]) }
        try await awaitConfirmation(planner, units: 2.345)
        operation.cancel()
        do { _ = try await operation.value; XCTFail("Expected cancellation") } catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        XCTAssertFalse(planner.submitting)
        XCTAssertNil(planner.confirmation)
    }
    @MainActor func testChoosingFreeAlternativeUsesThatDeviceAndOverrideResets() async throws {
        let api = api { _, count in
            count == 1 ? (409, Data("{\"code\":\"confirm_required\",\"detail\":\"确认\",\"plan\":\(Self.quote())}".utf8)) : (200, Data(#"{"id":"created"}"#.utf8))
        }
        let planner = TaskPlanner(); planner.selected = "colab-L4"
        let operation = Task { () -> Submitted in try await planner.submit(api: api, path: "/train", input: ["slug":"test"]) }
        try await awaitConfirmation(planner, units: 2.345)
        planner.confirmationDevice = "local"; planner.resolve(true)
        _ = try await operation.value
        let _: Submitted = try await planner.submit(api: api, path: "/train", input: ["slug":"again"])
        let requests = ComputeProtocol.fixture.requests
        XCTAssertEqual(try body(requests[1])["compute"] as? String, "local")
        XCTAssertEqual(try body(requests[1])["approved_device"] as? String, "local")
        XCTAssertNil(try body(requests[2])["compute"])
        XCTAssertNil(try body(requests[2])["approved"])
    }
    @MainActor func testAllCompletedSweepHasNoPlan() async throws {
        let api = api { _, _ in (200, Data(#"{"plan":null,"groups":[{"slug":"dave-123","character":"dave","engine":"v2proplus","status":"skipped"}]}"#.utf8)) }
        let planner = TaskPlanner()
        await planner.refresh(api: api, input: SweepRequest(characters: ["dave"], engines: ["v2proplus"]), sweep: true)
        XCTAssertTrue(planner.allCompleted)
        XCTAssertNil(planner.plan)
        XCTAssertEqual(planner.groups.first?.status, "skipped")
        XCTAssertEqual(ComputeProtocol.fixture.requests.first?.url?.path, "/train/sweep/plan")
    }
    @MainActor func testUnavailableShowsUserReasonWithoutSubmittingAgain() async throws {
        let quote = Self.quote().replacingOccurrences(of: "\"available\":true", with: "\"available\":false,\"reason_code\":\"insufficient_balance\"")
        let api = api { _, _ in (409, Data("{\"code\":\"compute_unavailable\",\"detail\":\"internal\",\"plan\":\(quote)}".utf8)) }
        let planner = TaskPlanner()
        do {
            let _: Submitted = try await planner.submit(api: api, path: "/train", input: ["slug":"test"])
            XCTFail("Expected conflict")
        } catch { XCTAssertEqual(error.localizedDescription, "剩余单元不够") }
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
    }
    @MainActor func testVersionsUseSelectedController() async {
        let api = api { _, _ in (200, Data(#"{"studio":{"current":"0.1.0","latest":"0.2.0","notes_url":null},"core":{"current":"0.1.0"}}"#.utf8)) }
        let versions = VersionStore()
        await versions.checkLatest(api: api)
        XCTAssertEqual(versions.latest?.studio.latest, "0.2.0")
        XCTAssertEqual(ComputeProtocol.fixture.requests.first?.url?.path, "/system/versions")
        XCTAssertEqual(ComputeProtocol.fixture.requests.first?.url?.host, "fixture")
    }
    @MainActor func testIdentityDefaultsAndClientRole() {
        let name = "compute-tests-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { false })
        XCTAssertFalse(connection.configured)
        XCTAssertEqual(connection.baseURL, "http://127.0.0.1:9880")
        XCTAssertTrue(connection.localCoreMissing)
        connection.role = "client"; connection.address = "http://studio.local:9980"; connection.alias = "mini"; connection.save()
        let restored = StudioConnection(defaults: defaults, localCoreInstalled: { false })
        XCTAssertTrue(restored.configured)
        XCTAssertTrue(restored.isClient)
        XCTAssertFalse(restored.localCoreMissing)
        XCTAssertEqual(restored.controllerName, "mini")
        XCTAssertEqual(restored.baseURL, "http://studio.local:9980")
        restored.alias = "   "
        XCTAssertEqual(restored.controllerName, "主控")
    }
    @MainActor func testCloudLoginHandshakeWithMockAuthorizationCode() async throws {
        let api = api { request, _ in
            switch (request.httpMethod!, request.url!.path) {
            case ("POST", "/cloud/accounts/login"):
                return (200, Data(#"{"login_id":"login-test","auth_url":"https://example.com/authorize","needs_code":true}"#.utf8))
            case ("POST", "/cloud/accounts/login/login-test"):
                return (200, Data(#"{"state":"waiting","account":null,"error":null}"#.utf8))
            case ("GET", "/cloud/accounts/login/login-test"):
                let cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
                let account = String(decoding: try! JSONEncoder().encode(cloud.accounts[0]), as: UTF8.self)
                return (200, Data("{\"state\":\"done\",\"account\":\(account),\"error\":null}".utf8))
            default: return (404, Data())
            }
        }
        let start: CloudLogin = try await api.post("/cloud/accounts/login")
        XCTAssertTrue(start.needs_code)
        XCTAssertEqual(start.auth_url.host, "example.com")
        let accepted: CloudLoginStatus = try await api.send("/cloud/accounts/login/\(start.login_id)", body: ["code":"mock-authorization-code"])
        XCTAssertEqual(accepted.state, "waiting")
        let result: CloudLoginStatus = try await api.getWithoutRetry("/cloud/accounts/login/\(start.login_id)")
        XCTAssertEqual(result.state, "done")
        XCTAssertEqual(result.account?.id, "a")
        XCTAssertEqual(try body(ComputeProtocol.fixture.requests[1])["code"] as? String, "mock-authorization-code")
    }
    @MainActor func testCloudAndDeviceMutationContractsWithMocks() async throws {
        let api = api { request, _ in
            switch (request.httpMethod!, request.url!.path) {
            case ("GET", "/compute"): return (200, Data(Stage2Fixtures.compute.utf8))
            case ("POST", "/compute/devices"), ("PATCH", "/compute/devices/lan-dev"):
                let list = try! JSONDecoder().decode(ComputeList.self, from: Data(Stage2Fixtures.compute.utf8))
                return (200, try! JSONEncoder().encode(list.devices[1]))
            case ("GET", "/cloud"): return (200, Data(Stage2Fixtures.cloud.utf8))
            case ("PUT", "/cloud/settings"): return (200, Data(#"{"gpu_pref":"auto","max_minutes":null,"keep_minutes":0}"#.utf8))
            case ("POST", "/cloud/accounts/a/disable"): return (204, Data())
            default: return (404, Data())
            }
        }
        let store = ComputeStore(api: api)
        let added = try await store.addDevice(name: "gpu-worker", address: "http://worker.local", pairCode: "ABC-DEF")
        XCTAssertEqual(added.id, "lan-dev")
        try await store.patch(added.id, fields: ["wake":DeviceWake(mac: "00:11:22:33:44:55", broadcast: "192.168.1.255").jsonValue])
        try await store.patch(added.id, fields: ["wake":.null])
        try await store.saveCloud(CloudSettings())
        try await store.disableAccount("a")
        let requests = ComputeProtocol.fixture.requests
        XCTAssertEqual(requests.filter { $0.url?.path == "/compute/devices" }.count, 1)
        let patches = requests.filter { $0.httpMethod == "PATCH" }
        XCTAssertEqual((try body(patches[0])["wake"] as? [String: String])?["mac"], "00:11:22:33:44:55")
        XCTAssertEqual((try body(patches[0])["wake"] as? [String: String])?["broadcast"], "192.168.1.255")
        XCTAssertTrue(try body(patches[1])["wake"] is NSNull)
        let settings = try XCTUnwrap(requests.first { $0.url?.path == "/cloud/settings" })
        XCTAssertEqual(settings.httpMethod, "PUT")
        XCTAssertTrue(try body(settings)["max_minutes"] is NSNull)
        XCTAssertTrue(requests.contains { $0.httpMethod == "POST" && $0.url?.path == "/cloud/accounts/a/disable" })
    }
    func testCloudAutomaticDurationEncodesExplicitNull() throws {
        let data = try JSONEncoder().encode(CloudSettings())
        let object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertTrue(object["max_minutes"] is NSNull)
        let decoded = try JSONDecoder().decode(CloudSettings.self, from: Data(#"{"gpu_pref":"auto","max_minutes":null,"keep_minutes":0}"#.utf8))
        XCTAssertNil(decoded.max_minutes)
    }
    @MainActor func testGoogleDrivePickerUsesFirstDirectoryAndFallsBack() throws {
        let files = FileManager.default
        let home = files.temporaryDirectory.resolvingSymlinksInPath().appending(path: "tts-library-picker-\(UUID().uuidString)")
        let cloudStorage = home.appending(path: "Library/CloudStorage")
        try files.createDirectory(at: home, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: home) }

        XCTAssertEqual(LibrarySettingsView.googleDrivePickerDirectory(home: home).path, home.path)
        try files.createDirectory(at: cloudStorage, withIntermediateDirectories: true)
        let decoy = cloudStorage.appending(path: "GoogleDrive-00-file")
        try Data().write(to: decoy)
        XCTAssertEqual(LibrarySettingsView.googleDrivePickerDirectory(home: home).path, cloudStorage.path)

        let firstDrive = cloudStorage.appending(path: "GoogleDrive-a")
        let laterMyDrive = cloudStorage.appending(path: "GoogleDrive-z/My Drive")
        try files.createDirectory(at: laterMyDrive, withIntermediateDirectories: true)
        try files.createDirectory(at: firstDrive, withIntermediateDirectories: false)
        XCTAssertEqual(LibrarySettingsView.googleDrivePickerDirectory(home: home).lastPathComponent, firstDrive.lastPathComponent)

        let myDrive = firstDrive.appending(path: "My Drive")
        try files.createDirectory(at: myDrive, withIntermediateDirectories: false)
        let picked = LibrarySettingsView.googleDrivePickerDirectory(home: home)
        XCTAssertEqual(picked.lastPathComponent, myDrive.lastPathComponent)
        XCTAssertEqual(picked.deletingLastPathComponent().lastPathComponent, firstDrive.lastPathComponent)
    }
    func testWakePacketAndSpeedBoundaries() throws {
        let packet = try WakePacket.bytes(mac: "aa:bb:cc:dd:ee:ff")
        XCTAssertEqual(packet.count, 102)
        XCTAssertEqual(Array(packet.prefix(6)), Array(repeating: 255, count: 6))
        XCTAssertEqual(Array(packet.suffix(6)), [0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff])
        XCTAssertEqual(EngineSpeed(sec_per_item: 4, source: "measured").grade, "快")
        XCTAssertEqual(EngineSpeed(sec_per_item: 12, source: "reference").grade, "中")
        XCTAssertEqual(EngineSpeed(sec_per_item: 12.1, source: "measured").grade, "慢")
        XCTAssertNil(EngineSpeed(sec_per_item: 3, source: "untested").grade)
    }
}


private final class ConnectionFixtureState: @unchecked Sendable {
    let lock = NSLock()
    private var failures = 0
    private var counts: [String: Int] = [:]
    func failNext(_ value: Int) { lock.lock(); failures = value; lock.unlock() }
    func respond(_ request: URLRequest) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url!.path
        counts[path, default: 0] += 1
        if path == "/health" {
            if failures > 0 { failures -= 1; return (503, Data(#"{"detail":"暂不可用"}"#.utf8)) }
            return (200, Data(#"{"status":"ok","time":"","workers":{},"version":"0.1.3","commit":"fixture"}"#.utf8))
        }
        let value: String
        switch path {
        case "/models": value = Stage2Fixtures.models
        case "/system/info": value = Stage2Fixtures.systemInfo
        case "/jobs": value = #"{"jobs":[{"id":"restored","kind":"train","target":"test","status":"succeeded","progress":1,"stage":"完成","created":"","log_tail":[],"completed":[]}],"total":1}"#
        case "/compute": value = Stage2Fixtures.compute
        case "/library": value = Stage2Fixtures.library
        case "/cloud": value = Stage2Fixtures.cloud
        case "/system/versions": value = #"{"studio":{"current":"0.1.3","latest":"0.1.3","notes_url":null},"core":{"current":"0.1.3"}}"#
        default: value = "{}"
        }
        return (200, Data(value.utf8))
    }
    func count(_ path: String) -> Int { lock.lock(); defer { lock.unlock() }; return counts[path, default: 0] }
}
extension ComputeTests {
    @MainActor func testDuplicateSubmissionCannotOverwritePendingApproval() async throws {
        let api = api { _, n in n == 1 ? (409, Data("{\"code\":\"confirm_required\",\"detail\":\"确认\",\"plan\":\(Self.quote())}".utf8)) : (200, Data(#"{"id":"only-one"}"#.utf8)) }
        let planner = TaskPlanner()
        let first = Task { () -> Submitted in try await planner.submit(api: api, path: "/train", input: ["slug":"test"]) }
        try await awaitConfirmation(planner, units: 2.345)
        do { let _: Submitted = try await planner.submit(api: api, path: "/train", input: ["slug":"second"]); XCTFail("Duplicate accepted") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        planner.resolve(true)
        let created = try await first.value
        XCTAssertEqual(created.id, "only-one")
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
    }
    @MainActor func testWakeCanRunAgainAfterSuccessfulConnectionAndUsesBroadcast() throws {
        let defaults = UserDefaults(suiteName: "wake-\(UUID().uuidString)")!
        var packets: [(String, String)] = []
        let connection = StudioConnection(defaults: defaults) { packets.append(($0, $1)) }
        connection.role = "client"; connection.wakeMAC = "00:11:22:33:44:55"; connection.broadcast = "192.168.1.255"
        try connection.wakeIfNeeded(); try connection.wakeIfNeeded()
        XCTAssertEqual(packets.count, 1)
        connection.retryWake() // the health-success path clears the prior attempt
        try connection.wakeIfNeeded()
        XCTAssertEqual(packets.count, 2)
        XCTAssertEqual(packets.last?.1, "192.168.1.255")
    }
    @MainActor func testAutomaticReconnectLoadsInitialDataAndInitializationCannotRace() async throws {
        let state = ConnectionFixtureState()
        state.failNext(1)
        let api = api { request, _ in state.respond(request) }
        let studio = StudioStore(defaults: UserDefaults(suiteName: "reconnect-\(UUID().uuidString)")!)
        studio.api = api
        await studio.loadInitial()
        XCTAssertTrue(studio.jobs.isEmpty)
        await studio.refreshConnection()
        XCTAssertTrue(studio.connected)
        XCTAssertEqual(studio.jobs.first?.id, "restored")
        XCTAssertNotNil(studio.compute.cloud)
        XCTAssertNotNil(studio.versions.latest)
        async let first: Void = studio.loadInitial()
        async let second: Void = studio.loadInitial()
        _ = await (first, second)
        XCTAssertEqual(state.count("/jobs"), 1)
        XCTAssertEqual(state.count("/models"), 1)
        let cloud = ComputeProtocol.fixture.requests.first { $0.url?.path == "/cloud" }
        XCTAssertNil(cloud?.url?.query)
        await studio.compute.refresh(refreshAccounts: true)
        XCTAssertEqual(ComputeProtocol.fixture.requests.last { $0.url?.path == "/cloud" }?.url?.query, "refresh=true")
    }
    @MainActor func testConcurrentFirstInitializationFetchesJobsOnce() async {
        let state = ConnectionFixtureState()
        let api = api { request, _ in state.respond(request) }
        let studio = StudioStore(defaults: UserDefaults(suiteName: "initialize-\(UUID().uuidString)")!)
        studio.api = api
        async let first: Void = studio.loadInitial()
        async let second: Void = studio.loadInitial()
        _ = await (first, second)
        XCTAssertEqual(state.count("/jobs"), 1)
    }
    @MainActor func testTwoHealthFailuresAreNeededAndSameAddressKeepsCurrentFollow() async throws {
        let state = ConnectionFixtureState()
        let api = api { request, _ in state.respond(request) }
        let studio = StudioStore(defaults: UserDefaults(suiteName: "health-\(UUID().uuidString)")!)
        studio.api = api
        await studio.loadInitial()
        state.failNext(2)
        await studio.refreshConnection(); XCTAssertTrue(studio.connected)
        await studio.refreshConnection(); XCTAssertFalse(studio.connected)
        await studio.refreshConnection(); XCTAssertTrue(studio.connected)
        studio.generationPlanner.selected = "lan-dev"
        let follow = Task<Void, Never> { _ = try? await Task.sleep(for: .seconds(10)) }
        studio.generationTask = follow
        try studio.setBaseURL("http://fixture/")
        XCTAssertFalse(follow.isCancelled)
        XCTAssertEqual(studio.generationPlanner.selected, "lan-dev")
        follow.cancel()
        for planner in [studio.generationPlanner, studio.trainingPlanner, studio.sweepPlanner, studio.weightManager.planner, studio.blindManager.planner] { planner.selected = "old-device" }
        try studio.setBaseURL("http://second-fixture")
        XCTAssertTrue([studio.generationPlanner, studio.trainingPlanner, studio.sweepPlanner, studio.weightManager.planner, studio.blindManager.planner].allSatisfy { $0.selected == nil })
        for _ in 0..<100 { if state.count("/system/versions") == 2 && !studio.versions.checking { break }; try await Task.sleep(for: .milliseconds(10)) }
    }
    @MainActor func testIncompleteSweepDoesNotRequestPlan() async {
        let api = api { _, _ in (422, Data()) }
        let planner = TaskPlanner()
        await planner.refresh(api: api, input: SweepRequest(), sweep: true)
        XCTAssertEqual(ComputeProtocol.fixture.count, 0)
        XCTAssertNil(planner.error)
    }
    @MainActor func testUnavailableWithoutPlanPreservesChineseCoreReason() async {
        let api = api { _, _ in (409, Data(#"{"code":"compute_unavailable","detail":"测试算力 唤醒超时，请稍后再试","plan":null}"#.utf8)) }
        let studio = StudioStore(defaults: UserDefaults(suiteName: "unavailable-\(UUID().uuidString)")!)
        studio.api = api
        let operation = studio.perform {
            let _: Submitted = try await studio.trainingPlanner.submit(api: api, path: "/train", input: ["slug":"test"])
        }
        await operation.value
        XCTAssertEqual(studio.errorMessage, "测试算力 唤醒超时，请稍后再试")
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
    }
    @MainActor func testCloudLoginPollingRecoversAfterOneFailedRequest() async {
        let api = api { _, n in n == 1 ? (500, Data(#"{"detail":"temporary"}"#.utf8)) : (200, Data(#"{"state":"done","account":null,"error":null}"#.utf8)) }
        var errors = 0; var states: [String] = []
        await api.pollCloudLogin("fake-login", every: .milliseconds(1), update: { states.append($0.state) }, report: { _ in errors += 1 })
        XCTAssertEqual(errors, 1); XCTAssertEqual(states, ["done"])
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
    }
    func testSnapshotSpeedsAndHoursAreConsistentAndWakeBroadcastDecodes() throws {
        let cloud = try JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
        XCTAssertTrue(cloud.installed)
        XCTAssertEqual(displayUnits(0.25), "0.3")
        XCTAssertEqual(displayUnits(5.03), "5")
        let a100 = try XCTUnwrap(cloud.gpus.first { $0.gpu == "A100" })
        let g4 = try XCTUnwrap(cloud.gpus.first { $0.gpu == "G4" })
        XCTAssertEqual(a100.speeds["v2proplus"]?.grade, "快")
        XCTAssertEqual(g4.speeds["v2proplus"]?.grade, "快")
        XCTAssertNil(cloud.gpus.first { $0.gpu == "H100" }?.speeds["v2proplus"]?.grade)
        for gpu in cloud.gpus where gpu.available {
            let balance = cloud.accounts.filter { $0.gpus.contains(gpu.gpu) && !$0.disabled }.reduce(0) { $0 + ($1.balance ?? 0) }
            XCTAssertEqual(gpu.hours_left!, balance / gpu.units_per_hour!, accuracy: 0.01)
        }
        var plan = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.plan.utf8))
        plan.confirm = ["premium_gpu"]; plan.ask_over = 100; plan.ask_month = 1000
        plan.candidates[0].premium = true
        XCTAssertTrue(plan.reminder.contains("花费较高"))
        XCTAssertEqual(plan.reminder(for: plan.candidates.last), "")
        var device = try JSONDecoder().decode(ComputeList.self, from: Data(Stage2Fixtures.compute.utf8)).devices[1]
        device.status = "offline"; device.wakeable = true
        XCTAssertEqual(device.statusLabel, "睡眠中")
        let wake = DeviceWake(mac: "00:11:22:33:44:55", broadcast: "192.168.1.255")
        XCTAssertEqual(try JSONDecoder().decode(DeviceWake.self, from: JSONEncoder().encode(wake)).broadcast, "192.168.1.255")
    }
}


extension ComputeTests {
    @MainActor func testPairingAndRepairNeverSendToken() async throws {
        let api = api { request, _ in
            if request.url!.path == "/compute" { return (200, Data(Stage2Fixtures.compute.utf8)) }
            let dev = try! JSONDecoder().decode(ComputeList.self, from: Data(Stage2Fixtures.compute.utf8)).devices[1]
            return (200, try! JSONEncoder().encode(dev))
        }
        let compute = ComputeStore(api: api)
        let device = try await compute.addDevice(name: "worker", address: "http://worker.local:9981", pairCode: "ABC-DEF", wake: DeviceWake(mac: "00:11:22:33:44:55", broadcast: "192.168.1.255"))
        try await compute.repairDevice(device.id, address: "http://worker.local:9981", pairCode: "GHT-234")
        let writes = ComputeProtocol.fixture.requests.filter { $0.httpMethod != "GET" }
        XCTAssertEqual(try body(writes[0])["pair_code"] as? String, "ABC-DEF")
        XCTAssertEqual(try body(writes[1])["pair_code"] as? String, "GHT-234")
        XCTAssertNil(try body(writes[0])["token"])
        XCTAssertEqual((try body(writes[0])["wake"] as? [String: String])?["broadcast"], "192.168.1.255")
    }
    @MainActor func testPairCodeFailureIsChinese() async {
        let api = api { _, _ in (400, Data(#"{"detail":"配对码不对或已过期"}"#.utf8)) }
        let store = ComputeStore(api: api)
        do { _ = try await store.addDevice(name: "test", address: "http://worker", pairCode: "AAA-BBB"); XCTFail("Accepted expired code") }
        catch { XCTAssertEqual(error.localizedDescription, "配对码不对或已过期，请从那台电脑重新取得配对码。") }
    }
    @MainActor func testRemovedAccountLabelsAndFailedRemovalRemainRetryable() async throws {
        let api = api { request, n in
            if request.httpMethod == "DELETE" { return n == 1 ? (502, Data(#"{"detail":"forget failed"}"#.utf8)) : (204, Data()) }
            var cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
            if n == 2 { cloud.accounts[0].disabled = true }
            if n >= 4 { cloud.accounts.removeAll { $0.id == "a" } }
            return (200, try! JSONEncoder().encode(cloud))
        }
        let compute = ComputeStore(api: api)
        try await compute.removeAccount("a")
        XCTAssertEqual(compute.accountError, "这个账号的授权没删掉，已先停用，可以再试")
        XCTAssertTrue(compute.cloud!.accounts.first { $0.id == "a" }!.disabled)
        try await compute.removeAccount("a")
        XCTAssertNil(compute.accountError)
        XCTAssertFalse(compute.cloud!.accounts.contains { $0.id == "a" })
        XCTAssertEqual(compute.cloud!.account_labels["a"], "studio@example.com")
        XCTAssertEqual(compute.cloud!.account_labels["removed"], "旧账号（已移除）")
    }
    @MainActor func testDefaultDisableAndEnableUseExplicitRoutes() async throws {
        let api = api { _, _ in (200, Data(Stage2Fixtures.cloud.utf8)) }
        let compute = ComputeStore(api: api)
        try await compute.disableAccount("default"); try await compute.enableAccount("a")
        let writes = ComputeProtocol.fixture.requests.filter { $0.httpMethod != "GET" }
        XCTAssertEqual(writes.map { $0.url!.path }, ["/cloud/accounts/default/disable", "/cloud/accounts/a/enable"])
        XCTAssertTrue(writes.allSatisfy { $0.httpMethod == "POST" })
    }
    @MainActor func testModelVersionSubmissionAndInvalidationRequireReselection() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (409, Data(#"{"code":"version_unavailable","detail":"这个角色的模型已经没有了"}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        try await studio.loadComposerVersions()
        XCTAssertEqual(studio.params.version, "v4/alice-g.ckpt/alice-s.pth")
        XCTAssertEqual(studio.params.engine, "v4")
        studio.applyDefaults()
        XCTAssertEqual(studio.params.version, studio.composerVersions.selectedID)
        do { try await studio.generate(); XCTFail("Expected missing version") } catch { XCTAssertEqual((error as? APIError)?.code, "version_unavailable") }
        XCTAssertNil(studio.composerVersions.selectedID)
        XCTAssertNil(studio.params.version)
        XCTAssertEqual(studio.composerVersions.rows.count, 2)
        try await studio.loadComposerVersions()
        XCTAssertNil(studio.composerVersions.selectedID)
        let post = try XCTUnwrap(ComputeProtocol.fixture.requests.first { $0.httpMethod == "POST" })
        XCTAssertEqual(try body(post)["version"] as? String, "v4/alice-g.ckpt/alice-s.pth")
        do { try await studio.generate(); XCTFail("Resubmitted without selection") } catch { }
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.httpMethod == "POST" }.count, 1)
        studio.selectComposerVersion(studio.composerVersions.rows[1].id)
        XCTAssertEqual(studio.params.engine, "v2proplus")
        XCTAssertEqual(studio.params.version, "v2proplus/july-g.ckpt/july-s.pth")
    }
    @MainActor func testVersionEmptyAndScoreSetAreNotCrossRanked() async throws {
        let api = api { _, _ in (200, Data("[]".utf8)) }
        let catalog = ModelVersionStore(); try await catalog.load(api: api, character: "empty")
        XCTAssertTrue(catalog.rows.isEmpty); XCTAssertNil(catalog.selected)
        let values = try JSONDecoder().decode([ModelVersion].self, from: Data(Stage2Fixtures.versions.utf8))
        XCTAssertTrue(values[0].recommended)
        XCTAssertTrue(values[0].scoreLabel!.contains("难句评测"))
        XCTAssertTrue(values[1].scoreLabel!.contains("快速评测"))
        XCTAssertFalse(values[1].recommended) // higher numeric score from another set is not re-ranked by the client
    }
    @MainActor func testMixedEnginePlanPreservesEachSideAndOmitsScalarEngine() async throws {
        let api = api { _, _ in (200, Data(Stage2Fixtures.generationPlan.utf8)) }
        let planner = TaskPlanner()
        await planner.refresh(api: api, input: PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 36))
        let fields = try body(ComputeProtocol.fixture.requests[0])
        XCTAssertEqual(fields["engines"] as? [String], ["v2pro", "v4"])
        XCTAssertNil(fields["engine"])
        XCTAssertEqual(fields["items"] as? Int, 36)
        let data = try TaskPlanner.body(PlanInput(kind: "gen", engines: ["v2pro", "v2pro"], items: 3), compute: nil)
        let duplicate = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(duplicate["engines"] as? [String], ["v2pro", "v2pro"])
        let parts = try TaskPlanner.body(PlanInput(kind: "gen", parts: [PlanPart(engine: "v2pro", items: 1), PlanPart(engine: "v4", items: 5)]), compute: nil)
        XCTAssertNotNil((try JSONSerialization.jsonObject(with: parts) as? [String: Any])?["parts"])
    }
    func testBlindAndCompareVersionsEncodeBoundEngineAndReadableRef() throws {
        let a = BlindSide(engine: "v4", gpt: "g", sovits: "s", version: "v4/g/s")
        let obj = try JSONSerialization.jsonObject(with: JSONEncoder().encode(a)) as! [String: Any]
        XCTAssertEqual(obj["version"] as? String, "v4/g/s")
        let request = CompareParams(engine: "v4", text: "test", engines: ["v4", "v2pro"], version: "v4/g/s")
        let compare = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as! [String: Any]
        XCTAssertEqual(compare["version"] as? String, "v4/g/s")
        XCTAssertEqual(compare["engines"] as? [String], ["v4", "v2pro"])
        let refs = try JSONDecoder().decode(RefList.self, from: Data(Stage2Fixtures.refs.utf8))
        XCTAssertEqual(refs.refs[0].label, "默认参考音频 · 6.1 秒")
        let info = try JSONDecoder().decode(SystemInfo.self, from: Data(Stage2Fixtures.systemInfo.utf8))
        XCTAssertFalse(info.web_available)
        XCTAssertEqual(info.web_url.absoluteString, "http://studio-host.local:9980/ui/")
    }
}


extension ComputeTests {
    @MainActor func testEvaluationMapsChosenVersionToItsExactPair() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            if request.url!.path == "/weights/eval" { return (200, try! JSONEncoder().encode(Job(id: "eval-version", kind: "eval", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: ""))) }
            return (200, Data(#"{"weights":[],"total":0}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        let manager = WeightStore(api: api, defaults: MemoryDefaults(), character: "alice", engine: "v2pro")
        try await manager.versions.load(api: api, character: "alice")
        manager.selectVersion(manager.versions.selectedID)
        manager.pairEngine = "v2pro" // A metadata refresh must not change the version submitted.
        try await manager.evaluate(studio: studio)
        let request = try XCTUnwrap(ComputeProtocol.fixture.requests.first { $0.url?.path == "/weights/eval" })
        let obj = try body(request)
        XCTAssertEqual(obj["engine"] as? String, "v4")
        XCTAssertEqual(obj["gpt"] as? String, "alice-g.ckpt")
        XCTAssertEqual(obj["sovits"] as? String, "alice-s.pth")
        XCTAssertEqual(obj["version"] as? String, "v4/alice-g.ckpt/alice-s.pth")
        XCTAssertEqual(obj["character"] as? String, "alice") // required whenever version is present
    }
    @MainActor func testComparisonSubmitsSelectedVersionAndEngineList() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (200, try! JSONEncoder().encode(Job(id: "compare-version", kind: "compare", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: "")))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        studio.params.character = "alice"
        try await studio.loadComposerVersions()
        try await studio.compare(engines: ["v2pro", "v4"])
        let request = try XCTUnwrap(ComputeProtocol.fixture.requests.first { $0.url?.path == "/compare/jobs" })
        let obj = try body(request)
        XCTAssertEqual(obj["version"] as? String, "v4/alice-g.ckpt/alice-s.pth")
        XCTAssertEqual(obj["character"] as? String, "alice") // required whenever version is present
        XCTAssertEqual(obj["engine"] as? String, "v4")
        XCTAssertEqual(obj["engines"] as? [String], ["v2pro", "v4"])
    }
}

extension ComputeTests {
    @MainActor func testUnselectedVersionBlocksGenerationCommand() {
        let studio = StudioStore(defaults: MemoryDefaults())
        XCTAssertFalse(studio.composerReady)
        studio.startGeneration()
        XCTAssertNil(studio.generationTask)
        studio.useManualComposer()
        XCTAssertTrue(studio.composerReady)
    }
}

extension ComputeTests {
    @MainActor func testPreviewIdentityIsStableAcrossViewUpdates() {
        let input = PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 36)
        XCTAssertEqual(Set((0..<50).map { _ in TaskPlanner.previewID(input) }).count, 1)
        XCTAssertNotEqual(TaskPlanner.previewID(input), TaskPlanner.previewID(PlanInput(kind: "gen", engine: "v4", items: 36)))
    }
}

extension ComputeTests {
    @MainActor func testControllerSwitchResetsEvaluationVersions() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (200, Data("{}".utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        try await studio.weightManager.versions.load(api: api, character: "alice")
        XCTAssertNotNil(studio.weightManager.versions.selected)
        try studio.setBaseURL("http://other-controller")
        XCTAssertTrue(studio.weightManager.versions.rows.isEmpty)
        XCTAssertNil(studio.weightManager.versions.selectedID)
        XCTAssertEqual(studio.weightManager.versions.character, "")
    }
    @MainActor func testModelProducingJobsRefreshBothCurrentVersionLists() async throws {
        let api = api { request, _ in
            if request.url!.path == "/models" { return (200, Data(Stage2Fixtures.models.utf8)) }
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (200, Data(#"{"weights":[],"total":0}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        studio.models = try JSONDecoder().decode(ModelsResponse.self, from: Data(Stage2Fixtures.models.utf8)).characters
        studio.applyDefaults()
        for kind in ["train", "sweep", "eval"] {
            studio.composerVersions.rows = []; studio.weightManager.versions.rows = []
            _ = try await studio.follow(Job(id: kind, kind: kind, target: "alice", status: "succeeded", progress: 1, stage: "完成", created: ""))
            XCTAssertEqual(studio.composerVersions.rows.count, 2)
            XCTAssertEqual(studio.weightManager.versions.rows.count, 2)
            XCTAssertEqual(studio.composerVersions.selected?.score_set, "hard")
        }
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.url!.path.hasSuffix("/versions") }.count, 6)
    }
    @MainActor func testAccountErrorIsScopedAndClearedBySuccessfulActions() async throws {
        let api = api { request, _ in
            if request.httpMethod == "DELETE" { return (502, Data(#"{"detail":"delete failed"}"#.utf8)) }
            if request.url!.path == "/cloud" { return (200, Data(Stage2Fixtures.cloud.utf8)) }
            return (200, Data("{}".utf8))
        }
        let store = ComputeStore(api: api, defaults: MemoryDefaults())
        for action in ["disable", "enable", "refresh"] {
            try await store.removeAccount("a")
            XCTAssertEqual(store.accountErrorDevice, "a")
            store.clearAccountError("b"); XCTAssertNotNil(store.accountError)
            if action == "disable" { try await store.disableAccount("a") }
            if action == "enable" { try await store.enableAccount("a") }
            if action == "refresh" { try await store.refreshAccount("a") }
            XCTAssertNil(store.accountError); XCTAssertNil(store.accountErrorDevice)
        }
    }
    @MainActor func testOrdinaryPairingEditPreservesFieldsAndUsesChineseFailure() async throws {
        let api = api { _, _ in (400, Data(#"{"detail":"ConnectError: connection failed"}"#.utf8)) }
        let store = ComputeStore(api: api, defaults: MemoryDefaults())
        do {
            try await store.repairDevice("lan-test", address: "http://test", pairCode: "ABC-DEF", fields: ["name":.string("书房"), "wake":DeviceWake(mac: "00:11:22:33:44:55", broadcast: "192.168.1.255").jsonValue])
            XCTFail("Expected pairing failure")
        } catch { XCTAssertTrue(error.localizedDescription.contains("重新配对没有完成")) }
        let value = try body(ComputeProtocol.fixture.requests[0])
        XCTAssertEqual(value["name"] as? String, "书房")
        XCTAssertEqual(value["pair_code"] as? String, "ABC-DEF")
        XCTAssertEqual((value["wake"] as? [String: Any])?["broadcast"] as? String, "192.168.1.255")
    }
    @MainActor func testHiddenBoundVersionIsRetainedInAdvancedSelection() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (200, Data(#"{"weights":[],"total":0}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        studio.params.character = "alice"
        try await studio.loadComposerVersions()
        let gpt = studio.params.gpt
        try await studio.loadComposerWeights()
        XCTAssertEqual(studio.params.gpt, gpt)
        try await studio.weightManager.loadPair(studio: studio)
        XCTAssertTrue(studio.weightManager.gptID.hasPrefix("version-gpt:"))
        XCTAssertTrue(studio.weightManager.sovitsID.hasPrefix("version-sovits:"))
    }
    func testHumanReadableMetricsAndPremiumReminderLines() throws {
        XCTAssertEqual(displayAccuracy(0.1), "90%")
        XCTAssertEqual(displaySimilarity(0.9), "90%")
        XCTAssertEqual(displayScore(92.345), "92")
        XCTAssertEqual(displaySeconds(31), "31")
        XCTAssertEqual(displayHours(2097), "2,100")
        XCTAssertEqual(displaySplit("cut5"), "按标点分句")
        var plan = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.plan.utf8))
        var card = plan.candidates.first { $0.type == "cloud" }!
        card.premium = true; card.units = 5.03
        plan.ask_over = 2; plan.ask_month = 150; plan.month_used = 149
        XCTAssertTrue(plan.reminder(for: card).contains("一次 2 单元"))
        XCTAssertTrue(plan.reminder(for: card).contains("本月 150 单元"))
        plan.ask_over = 100; plan.ask_month = 500
        XCTAssertTrue(plan.reminder(for: card).contains("一次 100 单元"))
        XCTAssertTrue(plan.reminder(for: card).contains("单元花费较高"))
    }
    func testBlindCharacterUsesAvailableRoleAndEmptyLibrary() {
        XCTAssertEqual(preferredBlindCharacter("", available: ["bob", "carol"]), "bob")
        XCTAssertEqual(preferredBlindCharacter("carol", available: ["bob", "carol"]), "carol")
        XCTAssertNil(preferredBlindCharacter("alice", available: []))
    }
    @MainActor func testUnavailableVersionClearsOldPreview() async throws {
        let planner = TaskPlanner()
        planner.plan = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.generationPlan.utf8))
        planner.error = "old"
        planner.clearPreview()
        XCTAssertNil(planner.plan); XCTAssertNil(planner.error); XCTAssertFalse(planner.loading)
    }
    @MainActor func testCompletedJobsSurviveCatalogAndVersionRefreshErrors() async throws {
        for failedPath in ["/models", "/characters/alice/versions", "/weights"] {
            for completion in ["initial", "sse", "poll"] {
                let finished = Job(id: "done", kind: "train", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: "")
                let data = try JSONEncoder().encode(finished)
                let api = api { request, _ in
                    let path = request.url!.path
                    if path == "/jobs/done/events" {
                        return completion == "poll" ? (200, Data()) : (200, Data("data: \(String(decoding: data, as: UTF8.self))\n\n".utf8))
                    }
                    if path == "/jobs/done" { return (200, data) }
                    if path == failedPath { return (500, Data(#"{"detail":"mock metadata failure"}"#.utf8)) }
                    if path == "/models" { return (200, Data(Stage2Fixtures.models.utf8)) }
                    if path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
                    return (200, Data(#"{"weights":[],"total":0}"#.utf8))
                }
                let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
                var statuses: [String] = []
                api.onStatus = { statuses.append($0) }
                studio.models = try JSONDecoder().decode(ModelsResponse.self, from: Data(Stage2Fixtures.models.utf8)).characters
                studio.recordLocalJob(finished.id)
                var initial = finished
                if completion != "initial" { initial.status = "running" }
                let result = try await studio.follow(initial)
                XCTAssertEqual(result.status, "succeeded", "\(failedPath) / \(completion)")
                XCTAssertEqual(studio.jobs.first?.status, "succeeded")
                XCTAssertEqual(studio.status, "任务完成")
                XCTAssertTrue(statuses.isEmpty, "metadata errors must never be reported as an event disconnection")
                XCTAssertNil(studio.errorMessage)
                if completion == "sse" { XCTAssertFalse(ComputeProtocol.fixture.requests.contains { $0.url?.path == "/jobs/done" }) }
                if completion == "poll" { XCTAssertTrue(ComputeProtocol.fixture.requests.contains { $0.url?.path == "/jobs/done" }) }
            }
        }
    }
    @MainActor func testEvaluationSuccessSurvivesScoreRefreshError() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            if request.url!.path == "/weights/eval" { return (200, try! JSONEncoder().encode(Job(id: "eval-ok", kind: "eval", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: ""))) }
            return (500, Data(#"{"detail":"refresh failed"}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        let manager = WeightStore(api: api, defaults: MemoryDefaults(), character: "alice", engine: "v4")
        try await manager.versions.load(api: api, character: "alice")
        try await manager.evaluate(studio: studio)
        XCTAssertFalse(manager.evaluationBusy)
        XCTAssertTrue(manager.notice?.contains("评测完成") == true)
        XCTAssertEqual(studio.jobs.first?.status, "succeeded")
        XCTAssertNil(studio.errorMessage)
    }
    @MainActor func testManualEvaluationOmitsVersionAndPreservesFiles() async throws {
        let api = api { request, _ in
            if request.url!.path == "/weights/eval" { return (200, try! JSONEncoder().encode(Job(id: "manual", kind: "eval", target: "alice", status: "succeeded", progress: 1, stage: "完成", created: ""))) }
            return (200, Data(Stage2Fixtures.weights.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        let manager = WeightStore(api: api, defaults: MemoryDefaults(), character: "alice", engine: "v4")
        manager.pairRows = try JSONDecoder().decode(WeightList.self, from: Data(Stage2Fixtures.weights.utf8)).weights
        manager.useManualPair()
        manager.gptID = "v4:alice-g.ckpt"; manager.sovitsID = "v4:alice-s.pth"
        try await manager.evaluate(studio: studio)
        let request = try XCTUnwrap(ComputeProtocol.fixture.requests.first { $0.url?.path == "/weights/eval" })
        let obj = try body(request)
        XCTAssertNil(obj["version"])
        XCTAssertEqual(obj["gpt"] as? String, "alice-g.ckpt")
        XCTAssertEqual(obj["sovits"] as? String, "alice-s.pth")
    }
    @MainActor func testEvaluationUnavailableVersionRequiresReselection() async throws {
        let api = api { request, _ in
            if request.url!.path.hasSuffix("/versions") { return (200, Data(Stage2Fixtures.versions.utf8)) }
            return (409, Data(#"{"code":"version_unavailable","detail":"gone"}"#.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api
        let manager = WeightStore(api: api, defaults: MemoryDefaults(), character: "alice", engine: "v4")
        try await manager.versions.load(api: api, character: "alice")
        do { try await manager.evaluate(studio: studio); XCTFail("Expected unavailable version") }
        catch let failure as APIError { XCTAssertEqual(failure.code, "version_unavailable") }
        XCTAssertNil(manager.versions.selectedID)
        XCTAssertTrue(manager.versions.message?.contains("重新选择") == true)
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.url?.path == "/weights/eval" }.count, 1)
    }
    func testReadableCheckpointLabelsPreferMetadataAndNeverExposeFiles() throws {
        let values = try JSONDecoder().decode([ModelVersion].self, from: Data(Stage2Fixtures.versions.utf8))
        XCTAssertEqual(modelVersionLabel(gpt: values[0].gpt, sovits: values[0].sovits, versions: values), "11 月训练 · v4")
        XCTAssertEqual(modelVersionLabel(gpt: "run-e15.ckpt", sovits: "voice_e8_s100.pth", versions: values), "GPT 第 15 轮 · SoVITS 第 8 轮")
        var weights = try JSONDecoder().decode(WeightList.self, from: Data(Stage2Fixtures.weights.utf8)).weights
        weights[0].epoch = 20; weights[1].epoch = 10
        XCTAssertEqual(modelVersionLabel(gpt: weights[0].name, sovits: weights[1].name, versions: [], weights: weights), "GPT · 11 月训练 · GPT · SoVITS · 11 月训练 · SoVITS")
        weights[0].display_name = nil; weights[1].display_name = nil
        let autumn = modelVersionLabel(gpt: weights[0].name, sovits: weights[1].name, versions: [], weights: weights)
        XCTAssertEqual(autumn, "GPT 2026-11-20 · 第 20 轮 · SoVITS 2026-11-20 · 第 10 轮")
        weights[0].created_at = "2026-07-20"; weights[1].created_at = "2026-07-20"
        XCTAssertNotEqual(autumn, modelVersionLabel(gpt: weights[0].name, sovits: weights[1].name, versions: [], weights: weights))
        weights[0].display_name = "11 月训练"
        weights[0].epoch = nil
        XCTAssertTrue(modelVersionLabel(gpt: weights[0].name, sovits: "unknown.pth", versions: [], weights: weights).contains("11 月训练"))
    }
    func testAutomaticModelNamesDistinguishSameDayAndFormatMetrics() throws {
        var entry = try JSONDecoder().decode(WeightList.self, from: Data(Stage2Fixtures.weights.utf8)).weights[0]
        entry.display_name = nil; entry.created_at = "2026-11-20T00:25:03+00:00"
        let first = entry.modelLabel
        entry.created_at = "2026-11-20T01:25:03.123+00:00"
        XCTAssertNotEqual(first, entry.modelLabel)
        entry.created_at = "2026-11-20T00:25:04+00:00"
        XCTAssertNotEqual(first, entry.modelLabel, "same-minute entries remain distinguishable")
        let second = entry.modelLabel
        entry.epoch = 15
        XCTAssertNotEqual(second, entry.modelLabel)
        XCTAssertTrue(entry.modelLabel.contains("第 15 轮"))
        XCTAssertFalse(first.contains("T00:"))
        XCTAssertEqual(displayScore(94.2), "94")
        XCTAssertEqual(scoreHelp(94.2), "评分：94.2")
        XCTAssertEqual(displaySeconds(5.80), "5.8")
    }

    @MainActor func testBlindRegenerateUnavailableVersionPreservesRecordAndRequiresReselection() async throws {
        let api = api { _, _ in (409, Data(#"{"code":"version_unavailable","detail":"missing bound weights"}"#.utf8)) }
        var jobs: [String] = []
        let manager = BlindStore(api: api, onJobCreated: { jobs.append($0) })
        manager.select("expired")
        manager.test = BlindTest(id: "expired", title: "既有盲听", created: "", character: "alice", eval_set: "mini", repeats: 1, ref_id: "r", status: "failed", job_id: "old", n_questions: 3, n_ready: 1, n_voted: 1, revealed: false, questions: [])
        do { try await manager.regenerate(); XCTFail("Expected version conflict") }
        catch let error as APIError {
            XCTAssertEqual(error.code, "version_unavailable")
            XCTAssertTrue(error.message.contains("重新选择版本"))
        }
        XCTAssertFalse(manager.busy)
        XCTAssertTrue(manager.regenerationNeedsVersion)
        XCTAssertEqual(manager.test?.job_id, "old")
        XCTAssertEqual(manager.test?.n_ready, 1)
        XCTAssertEqual(manager.test?.n_voted, 1)
        XCTAssertTrue(jobs.isEmpty)
        try await manager.regenerate()
        XCTAssertEqual(ComputeProtocol.fixture.count, 1, "stale test cannot be resubmitted before choosing new versions")
        manager.select(nil)
        XCTAssertFalse(manager.regenerationNeedsVersion)
    }
    @MainActor func testBlindRegenerateKeepsOtherFailuresUnchanged() async throws {
        let api = api { _, _ in (409, Data(#"{"code":"compute_unavailable","detail":"设备暂不可用"}"#.utf8)) }
        let manager = BlindStore(api: api)
        manager.select("existing")
        do { try await manager.regenerate(); XCTFail("Expected compute failure") }
        catch let error as APIError {
            XCTAssertEqual(error.code, "compute_unavailable")
            XCTAssertEqual(error.message, "设备暂不可用")
        }
        XCTAssertFalse(manager.regenerationNeedsVersion)
        XCTAssertFalse(manager.busy)
    }

}

extension ComputeTests {
    @MainActor func testInviteFlowCreatesPollsConnectsAndUsesQuickBench() async throws {
        let api = api { request, _ in
            switch (request.httpMethod!, request.url!.path) {
            case ("POST", "/compute/invites"):
                return (200, Data(#"{"id":"invite-1","code":"7Q4-K9M","expires_in":600,"expires_at":"2099-11-20T12:00:00Z","host":"studio-host.local","commands":{"unix":"curl -fsSL 'http://studio-host.local/install' | sh -s -- --pair 7Q4-K9M","windows":null}}"#.utf8))
            case ("GET", "/compute/invites/invite-1"):
                return (200, Data(#"{"id":"mock-invite","expires_in":0,"redeemed":true,"state":"connected","device":{"id":"lan-new","name":"书房电脑","type":"lan","status":"idle","engines":["v2pro"],"speeds":{},"hw":{"os":"linux","accel":"cpu","cores":4}}}"#.utf8))
            case ("DELETE", "/compute/invites/invite-1"):
                return (204, Data())
            case ("POST", "/bench"):
                return (200, Data(#"{"id":"quick-bench","kind":"bench","target":"lan-new","status":"queued","progress":0,"stage":"等待","created":"2026-11-20","log_tail":[],"completed":[]}"#.utf8))
            default: return (500, Data())
            }
        }
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api))
        flow.network = "tailscale"
        await flow.generate()
        XCTAssertEqual(flow.step, 2)
        XCTAssertNil(flow.invite?.commands.windows)
        let payload = try body(ComputeProtocol.fixture.requests[0])
        XCTAssertEqual(payload["network"] as? String, "tailscale")
        flow.invite?.expires_at = "2000-01-01T00:00:00Z" // server accepted the connection just before expiry
        await flow.poll("invite-1")
        XCTAssertEqual(flow.step, 4)
        XCTAssertEqual(flow.name, "书房电脑")
        XCTAssertEqual(flow.device?.hw?.accelerationLabel, "CPU · 4 核")
        let job = try await flow.finish()
        XCTAssertEqual(job?.id, "quick-bench")
        XCTAssertEqual(try body(ComputeProtocol.fixture.requests.last!)["quick"] as? Bool, true)
        await flow.close()
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.httpMethod == "DELETE" }.count, 1)
        XCTAssertEqual(flow.compute.devices.count, 1)
    }
    @MainActor func testInviteRegenerationOnlyPostsAndCloseCancelsCurrent() async throws {
        let api = api { request, index in
            if request.httpMethod == "DELETE" { return (204, Data()) }
            return (200, Data("{\"id\":\"invite-\(index)\",\"code\":\"ABC-DEF\",\"expires_in\":600,\"expires_at\":\"2099-11-20T12:00:00Z\",\"host\":\"studio-host.local\",\"commands\":{\"unix\":\"install\",\"windows\":null}}".utf8))
        }
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api))
        await flow.generate(); await flow.generate()
        XCTAssertEqual(flow.invite?.id, "invite-2")
        await flow.close()
        XCTAssertEqual(ComputeProtocol.fixture.requests.map { $0.httpMethod! + " " + $0.url!.path }, ["POST /compute/invites", "POST /compute/invites", "DELETE /compute/invites/invite-2"])
        XCTAssertNil(flow.invite)
        XCTAssertFalse(flow.busy)
    }
    @MainActor func testExpiredInviteStopsPollingAndCanRegenerate() async throws {
        let api = api { request, _ in
            if request.httpMethod == "DELETE" { return (204, Data()) }
            if request.httpMethod == "GET" { return (200, Data(#"{"id":"mock-invite","expires_in":0,"redeemed":false,"state":"expired","device":null}"#.utf8)) }
            return (200, Data(#"{"id":"expired","code":"ABC-DEF","expires_in":600,"expires_at":"2000-01-01T00:00:00.123Z","host":"studio-host.local","commands":{"unix":"install","windows":null}}"#.utf8))
        }
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api))
        await flow.generate(); await flow.poll("expired")
        XCTAssertTrue(flow.expired)
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
        await flow.generate()
        XCTAssertFalse(flow.expired)
        XCTAssertEqual(flow.step, 2)
    }
    @MainActor func testInviteServerExpiryAndConnectionFailureAreReadable() async throws {
        let api = api { request, _ in
            if request.httpMethod == "POST" { return (200, Data(#"{"id":"i","code":"ABC-DEF","expires_in":600,"expires_at":"2099-01-01T00:00:00Z","host":"studio-host.local","commands":{"unix":"install","windows":null}}"#.utf8)) }
            return (200, Data(#"{"id":"mock-invite","expires_in":0,"redeemed":false,"state":"expired","device":null}"#.utf8))
        }
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api))
        await flow.generate(); await flow.poll("i")
        XCTAssertTrue(flow.expired)
        XCTAssertNil(flow.device)
        let failed = DeviceInviteFlow(compute: ComputeStore(api: self.api { _, _ in (503, Data(#"{"detail":"connection failed"}"#.utf8)) }))
        await failed.generate()
        XCTAssertEqual(failed.error, "配对码没能生成，请检查连接后重试。")
        XCTAssertFalse(failed.busy)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1) // invite creation never blindly retries a POST
    }
    @MainActor func testManualPairingFeedsSameConnectedStepAndLaterSkipsBench() async throws {
        let api = api { request, _ in
            if request.url!.path == "/compute/invites" { return (200, Data(Self.inviteJSON("manual").utf8)) }
            return (200, Data(#"{"id":"manual","state":"connected","expires_in":0,"redeemed":true,"device":{"id":"lan-manual","name":"书房电脑","type":"lan","status":"idle","engines":["v4"],"speeds":{},"hw":{"os":"darwin","accel":"mps"}}}"#.utf8))
        }
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api))
        await flow.generate()
        flow.manualAddress = "http://study.local:9880"
        await flow.manual()
        XCTAssertEqual(flow.step, 4)
        XCTAssertEqual(flow.name, "书房电脑")
        XCTAssertEqual(flow.device?.hw?.accelerationLabel, "Apple 芯片")
        flow.quick = false
        let job = try await flow.finish()
        XCTAssertNil(job)
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
        let request = ComputeProtocol.fixture.requests[1]
        XCTAssertEqual(request.url!.path, "/compute/invites/manual/manual")
        XCTAssertEqual(try body(request).keys.sorted(), ["address"])
        XCTAssertEqual(try body(request)["address"] as? String, flow.manualAddress)
    }
    @MainActor func testBlindAudioElsewhereKeepsMachineMessageForBothErrorShapes() throws {
        for payload in [#"{"code":"audio_elsewhere","machine":"书房电脑"}"#, #"{"detail":{"code":"audio_elsewhere","machine":"书房电脑"}}"#] {
            let error = APIClient.error(Data(payload.utf8), status: 404)
            XCTAssertEqual(error.code, "audio_elsewhere")
            XCTAssertEqual(error.message, "音频在 书房电脑 上")
        }
        let error = APIClient.error(Data(#"{"code":"audio_elsewhere","machine":null}"#.utf8), status: 404)
        XCTAssertEqual(error.message, "音频在 另一台设备 上")
        XCTAssertEqual(APIClient.error(Data(#"{"detail":"Not Found"}"#.utf8), status: 404).message, "Not Found")
    }
    func testTaskContentAndDeviceQueueLabels() throws {
        XCTAssertEqual(computeTaskContent(PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 36)), "36 句 v2Pro / v4")
        XCTAssertEqual(computeTaskContent(PlanInput(kind: "train", engine: "v2proplus", lines: 120)), "120 句 v2ProPlus")
        var candidate = try JSONDecoder().decode(ComputePlan.self, from: Data(Self.quote().utf8)).candidates[1]
        XCTAssertEqual(computeQueueLabel(candidate, device: nil), "本机 · 空闲")
        candidate.idle = false
        XCTAssertEqual(computeQueueLabel(candidate, device: nil), "本机 · 忙，要排队")
    }
}

private final class DelayedInviteProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: @Sendable (URLRequest) -> (TimeInterval, Int, String) = { _ in (0, 500, "") }
    private let lock = NSLock()
    private var stopped = false
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (delay, status, payload) = Self.handler(request)
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            lock.lock(); defer { lock.unlock() }
            guard !stopped else { return }
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(payload.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() { lock.lock(); stopped = true; lock.unlock() }
}
private final class InviteRequests: @unchecked Sendable {
    private let lock = NSLock()
    private var paths: [String] = []
    private var posts = 0
    func record(_ request: URLRequest) -> Int {
        lock.lock(); defer { lock.unlock() }
        paths.append(request.httpMethod! + " " + request.url!.path)
        if request.httpMethod == "POST" { posts += 1 }
        return posts
    }
    var snapshot: [String] { lock.lock(); defer { lock.unlock() }; return paths }
}
extension ComputeTests {
    @MainActor private func delayedAPI(_ handler: @escaping @Sendable (URLRequest) -> (TimeInterval, Int, String)) -> APIClient {
        DelayedInviteProtocol.handler = handler
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [DelayedInviteProtocol.self]
        return APIClient(baseURL: "http://fixture", session: URLSession(configuration: config))
    }
    private static func inviteJSON(_ id: String) -> String {
        "{\"id\":\"\(id)\",\"code\":\"ABC-DEF\",\"expires_in\":600,\"expires_at\":\"2099-01-01T00:00:00Z\",\"host\":\"studio-host.local\",\"commands\":{\"unix\":\"install\",\"windows\":null}}"
    }
    @MainActor func testLateInviteCreationAfterCloseIsDeletedAndDoesNotAdvance() async throws {
        let requests = InviteRequests()
        let flow = DeviceInviteFlow(compute: ComputeStore(api: delayedAPI { request in
            _ = requests.record(request)
            return request.httpMethod == "POST" ? (0.15, 200, Self.inviteJSON("late")) : (0, 204, "")
        }))
        let pending = Task { await flow.generate() }
        while requests.snapshot.isEmpty { await Task.yield() }
        await flow.close()
        await pending.value
        XCTAssertNil(flow.invite)
        XCTAssertEqual(flow.step, 1)
        XCTAssertFalse(flow.busy)
        XCTAssertEqual(requests.snapshot, ["POST /compute/invites", "DELETE /compute/invites/late"])
    }
    @MainActor func testLatePollCannotConnectReplacedInvite() async throws {
        let requests = InviteRequests()
        let flow = DeviceInviteFlow(compute: ComputeStore(api: delayedAPI { request in
            let post = requests.record(request)
            if request.httpMethod == "POST" { return (0, 200, Self.inviteJSON("i-\(post)")) }
            if request.httpMethod == "DELETE" { return (0, 204, "") }
            return (0.15, 200, #"{"id":"mock-invite","expires_in":0,"redeemed":true,"state":"connected","device":{"id":"old-device","name":"旧设备","type":"lan","status":"idle","engines":[],"speeds":{}}}"#)
        }))
        await flow.generate()
        let pending = Task { await flow.poll("i-1") }
        while !requests.snapshot.contains("GET /compute/invites/i-1") { await Task.yield() }
        await flow.generate()
        await pending.value
        XCTAssertNil(flow.device)
        XCTAssertEqual(flow.invite?.id, "i-2")
        XCTAssertEqual(flow.step, 2)
    }
    @MainActor func testTransientInvitePollFailureRecovers() async throws {
        let requests = InviteRequests()
        let flow = DeviceInviteFlow(compute: ComputeStore(api: delayedAPI { request in
            _ = requests.record(request)
            if request.httpMethod == "POST" { return (0, 200, Self.inviteJSON("retry")) }
            let gets = requests.snapshot.filter { $0.hasPrefix("GET") }.count
            if gets == 1 { return (0, 502, #"{"detail":"temporary failure"}"#) }
            return (0, 200, #"{"id":"mock-invite","expires_in":0,"redeemed":true,"state":"connected","device":{"id":"new","name":"电脑","type":"lan","status":"idle","engines":[],"speeds":{}}}"#)
        }))
        await flow.generate(); await flow.poll("retry")
        XCTAssertEqual(flow.step, 4)
        XCTAssertNil(flow.error)
        XCTAssertEqual(requests.snapshot.filter { $0.hasPrefix("GET") }.count, 2)
    }
}

extension ComputeTests {
    @MainActor func testBlindPlaybackShowsAudioMachineWithoutReadingModelMetadata() async throws {
        let studio = StudioStore(defaults: UserDefaults(suiteName: "stage3-audio-" + UUID().uuidString)!)
        let client = api { _, _ in (404, Data(#"{"code":"audio_elsewhere","machine":"书房电脑"}"#.utf8)) }
        let manager = BlindStore(api: client)
        manager.play("/audio/missing", identity: "blind-left", studio: studio)
        for _ in 0..<100 {
            if studio.errorMessage != nil { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(studio.errorMessage, "音频在 书房电脑 上")
        XCTAssertFalse(studio.audio.playing)
        XCTAssertEqual(ComputeProtocol.fixture.requests.map { $0.url!.path }, ["/audio/missing"])
    }
}

extension ComputeTests {
    @MainActor func testNetworkBackReusesValidInviteAndChangesOnlyWhenNeeded() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { _, count in (200, Data(Self.inviteJSON("i-\(count)").utf8)) }))
        await flow.nextFromNetwork()
        let first = flow.invite?.id
        flow.step = 1
        await flow.nextFromNetwork()
        XCTAssertEqual(flow.invite?.id, first)
        XCTAssertEqual(flow.step, 2)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        flow.network = "tailscale"
        await flow.nextFromNetwork()
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
        flow.expired = true
        await flow.nextFromNetwork()
        XCTAssertEqual(ComputeProtocol.fixture.count, 3)
        flow.deadline = .distantPast
        await flow.nextFromNetwork()
        XCTAssertEqual(ComputeProtocol.fixture.count, 4)
        XCTAssertTrue(ComputeProtocol.fixture.requests.allSatisfy { $0.httpMethod == "POST" })
    }
    @MainActor func testFailedRegenerationPreservesCurrentCommand() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { _, count in
            count == 1 ? (200, Data(Self.inviteJSON("old").utf8)) : (503, Data(#"{"detail":"temporary"}"#.utf8))
        }))
        await flow.generate(); await flow.generate()
        XCTAssertEqual(flow.invite?.id, "old")
        XCTAssertEqual(flow.invite?.commands.unix, "install")
        XCTAssertEqual(flow.step, 2)
        XCTAssertNotNil(flow.error)
        XCTAssertFalse(flow.busy)
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
    }
    @MainActor func testInviteNotFoundAndFailedRequestAtLocalExpiryStopPolling() async throws {
        for status in [404, 410, 503] {
            let flow = DeviceInviteFlow(compute: ComputeStore(api: api { request, _ in
                request.httpMethod == "POST" ? (200, Data(Self.inviteJSON("gone").utf8)) : (status, Data(#"{"detail":"gone"}"#.utf8))
            }))
            await flow.generate()
            if status == 503 { flow.deadline = .distantPast }
            await flow.poll("gone")
            XCTAssertTrue(flow.expired)
            XCTAssertNil(flow.error)
            XCTAssertEqual(ComputeProtocol.fixture.count, 2)
        }
    }
    @MainActor func testInviteCountdownUsesRelativeLifetimeWithClockSkew() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { _, _ in
            (200, Data(Self.inviteJSON("clock").replacingOccurrences(of: "2099-01-01T00:00:00Z", with: "2000-01-01T00:00:00+09:00").utf8))
        }))
        let before = Date()
        await flow.generate()
        XCTAssertEqual(flow.invite?.expires_in, 600)
        XCTAssertFalse(flow.locallyExpired)
        XCTAssertGreaterThan(try XCTUnwrap(flow.deadline).timeIntervalSince(before), 590)
    }
    @MainActor func testManualFailureSurvivesSuccessfulWaitingPoll() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { request, _ in
            if request.url!.path == "/compute/invites" { return (200, Data(Self.inviteJSON("m").utf8)) }
            if request.httpMethod == "POST" { return (502, Data(#"{"detail":"failed"}"#.utf8)) }
            return (200, Data(#"{"id":"mock-invite","expires_in":600,"redeemed":false,"state":"waiting"}"#.utf8))
        }))
        await flow.generate(); flow.manualAddress = "http://study.local:9880"
        await flow.manual()
        let failure = flow.manualError
        XCTAssertNotNil(failure)
        let polling = Task { await flow.poll("m") }
        while ComputeProtocol.fixture.count < 3 { await Task.yield() }
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertEqual(flow.manualError, failure)
        XCTAssertEqual(flow.manualAddress, "http://study.local:9880")
        polling.cancel(); await polling.value
    }
    @MainActor func testAutomaticConnectionWinsManualConflictWithoutDuplicatingDevice() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: delayedAPI { request in
            if request.url!.path == "/compute/invites" { return (0, 200, Self.inviteJSON("race")) }
            if request.httpMethod == "POST" { return (0, 409, #"{"detail":"already redeemed","code":"invite_used"}"#) }
            return (0.1, 200, #"{"id":"mock-invite","expires_in":0,"redeemed":true,"state":"connected","device":{"id":"same","name":"书房电脑","type":"lan","status":"idle","engines":[],"speeds":{}}}"#)
        }))
        await flow.generate(); flow.manualAddress = "http://study.local:9880"
        await flow.manual()
        XCTAssertNotNil(flow.manualError)
        await flow.poll("race")
        XCTAssertEqual(flow.name, "书房电脑")
        XCTAssertEqual(flow.compute.devices.count, 1)
        XCTAssertNil(flow.manualError)
        flow.connected(try XCTUnwrap(flow.device))
        XCTAssertEqual(flow.compute.devices.count, 1)
    }
    @MainActor func testCloudCodeSubmissionHidesInputAndBlocksResubmissionUntilStatusCompletes() async throws {
        let requests = InviteRequests()
        let client = delayedAPI { request in _ = requests.record(request); return (0.1, 200, #"{"id":"mock-invite","expires_in":600,"redeemed":false,"state":"waiting"}"#) }
        let submission = CloudCodeSubmission()
        let pending = Task { await submission.submit(api: client, id: "login", code: "mock-code") }
        while requests.snapshot.isEmpty { await Task.yield() }
        XCTAssertTrue(submission.submitted)
        XCTAssertTrue(submission.working)
        await submission.submit(api: client, id: "login", code: "another-code")
        await pending.value
        XCTAssertTrue(submission.submitted)
        XCTAssertFalse(submission.working)
        await submission.submit(api: client, id: "login", code: "another-code")
        XCTAssertEqual(requests.snapshot, ["POST /cloud/accounts/login/login"])
    }
    @MainActor func testCloudCodeFailureAllowsRetryAndDoesNotBlindlyRepeat() async throws {
        let client = api { _, index in index == 1 ? (502, Data()) : (200, Data()) }
        let submission = CloudCodeSubmission()
        await submission.submit(api: client, id: "login", code: "mock-code")
        XCTAssertFalse(submission.submitted)
        XCTAssertNotNil(submission.error)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        await submission.submit(api: client, id: "login", code: "mock-code")
        XCTAssertTrue(submission.submitted)
        XCTAssertNil(submission.error)
        XCTAssertEqual(ComputeProtocol.fixture.count, 2)
    }
    func testTaskHeadersHaveOneModelAndCorrectUnitsForEveryCaller() throws {
        let training = PlanInput(kind: "train", engine: "v4", lines: 800)
        XCTAssertEqual(computeTaskHeading(training, action: "训练", title: "alice"), "这一次在哪训练（alice · 800 句 v4）")
        let blind = PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 6)
        XCTAssertEqual(computeTaskHeading(blind, action: "出题", title: "盲听 dave"), "这一次在哪出题（盲听 dave · 6 句 v2Pro / v4）")
        XCTAssertEqual(computeTaskHeading(PlanInput(kind: "gen", engine: "v4", items: 2), action: "生成"), "这一次在哪生成（2 句 v4）")
        XCTAssertEqual(computeTaskContent(PlanInput(kind: "eval", engine: "v4", items: 36)), "36 条 v4")
        var cloud = try JSONDecoder().decode(ComputePlan.self, from: Data(Self.quote().utf8)).candidates[0]
        cloud.prep_sec = 360
        XCTAssertEqual(computeQueueLabel(cloud, device: nil), "要先准备环境约 6 分钟")
        cloud.prep_sec = 220                                         // 取整到分钟
        XCTAssertEqual(computeQueueLabel(cloud, device: nil), "要先准备环境约 4 分钟")
        cloud.prep_sec = 20                                          // 至少 1 分钟
        XCTAssertEqual(computeQueueLabel(cloud, device: nil), "要先准备环境约 1 分钟")
        cloud.prep_sec = nil
        XCTAssertEqual(computeQueueLabel(cloud, device: nil), "要先准备环境")
        let wire = try JSONDecoder().decode(PlanCandidate.self, from: Data(#"{"device":"colab-L4","name":"云端 L4","type":"cloud","gpu":"L4","seconds":600,"units":0.5,"available":true,"idle":true,"premium":false,"prep_sec":250}"#.utf8))
        XCTAssertEqual(computeQueueLabel(wire, device: nil), "要先准备环境约 4 分钟")   // core /plan 给的 prep_sec
        var info = try JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
        info.accounts[0].disabled = true
        XCTAssertEqual(info.enabledAccountCount, info.accounts.filter { !$0.disabled }.count)
        XCTAssertLessThan(info.enabledAccountCount, info.accounts.count)
    }
}

extension ComputeTests {
    @MainActor func testRedeemedInviteWaitsForHeartbeatWithoutExpiring() async throws {
        let requests = InviteRequests()
        let flow = DeviceInviteFlow(compute: ComputeStore(api: delayedAPI { request in
            _ = requests.record(request)
            if request.httpMethod == "POST" { return (0, 200, Self.inviteJSON("install")) }
            let count = requests.snapshot.filter { $0.hasPrefix("GET") }.count
            if count == 1 { return (0, 200, #"{"id":"install","state":"waiting","expires_in":0,"redeemed":true,"device":null}"#) }
            return (0, 200, #"{"id":"install","state":"connected","expires_in":0,"redeemed":true,"device":{"id":"lan","name":"worker-host","type":"lan","status":"idle","engines":[],"speeds":{},"last_seen":"2026-11-20T22:00:00+09:00"}}"#)
        }))
        await flow.generate(); flow.deadline = .distantPast
        let polling = Task { await flow.poll("install") }
        for _ in 0..<100 {
            if flow.installing { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(flow.installing)
        XCTAssertFalse(flow.expired)
        XCTAssertFalse(flow.locallyExpired)
        XCTAssertNil(flow.deadline)
        XCTAssertEqual(flow.step, 3)
        await polling.value
        XCTAssertEqual(flow.step, 4)
        XCTAssertFalse(flow.installing)
        XCTAssertEqual(flow.device?.last_seen, "2026-11-20T22:00:00+09:00")
    }
    @MainActor func testManualFinalProtocolDistinguishesUsedExpiredAndConnectionFailure() async throws {
        for code in ["invite_used", "invite_expired", "connection_failed"] {
            let flow = DeviceInviteFlow(compute: ComputeStore(api: api { request, _ in
                if request.url!.path == "/compute/invites" { return (200, Data(Self.inviteJSON("manual").utf8)) }
                return (code == "connection_failed" ? 400 : 409, Data("{\"code\":\"\(code)\",\"detail\":\"rejected\"}".utf8))
            }))
            await flow.generate(); flow.manualAddress = "http://study.local:9880"; await flow.manual()
            XCTAssertEqual(flow.expired, code == "invite_expired")
            if code == "invite_expired" { XCTAssertNil(flow.manualError) }
            else { XCTAssertNotNil(flow.manualError) }
            XCTAssertNotNil(flow.invite)
            XCTAssertNil(flow.device)
            XCTAssertFalse(flow.busy)
        }
    }
    @MainActor func testManualReturnsInviteStatusAndNullLastSeen() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { request, _ in
            if request.url!.path == "/compute/invites" { return (200, Data(Self.inviteJSON("manual").utf8)) }
            return (200, Data(#"{"id":"manual","state":"connected","expires_in":0,"redeemed":true,"device":{"id":"lan","name":"自动名称","type":"lan","status":"idle","engines":[],"speeds":{},"last_seen":null}}"#.utf8))
        }))
        await flow.generate(); flow.manualAddress = "http://study.local:9880"; await flow.manual()
        XCTAssertEqual(flow.name, "自动名称")
        XCTAssertEqual(flow.step, 4)
        XCTAssertNil(flow.device?.last_seen)
        XCTAssertNil(flow.manualError)
    }
    @MainActor func testUnavailableTailscaleStaysOnNetworkStepWithActionableMessage() async {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { _, _ in (409, Data(#"{"code":"tailscale_unavailable","detail":"raw network diagnostic"}"#.utf8)) }))
        flow.network = "tailscale"
        await flow.nextFromNetwork()
        XCTAssertEqual(flow.step, 1)
        XCTAssertTrue(flow.error?.contains("主控还没连上 Tailscale") == true)
        XCTAssertFalse(flow.error?.contains("raw") == true)
        XCTAssertNil(flow.invite)
        XCTAssertFalse(flow.busy)
    }
}

extension ComputeTests {
    @MainActor func testBlindRegenerationCarriesComputeAndExactApprovalWithoutModelMetadata() async throws {
        let response = Data(#"{"id":"b","title":"test","created":"2026-11-20","character":"alice","eval_set":"mini","repeats":1,"ref_id":"r","status":"generating","job_id":"new-job","n_questions":3,"n_ready":1,"n_voted":1,"revealed":false,"questions":[]}"#.utf8)
        let manager = BlindStore(api: api { _, index in
            index == 1 ? (409, Data("{\"code\":\"confirm_required\",\"detail\":\"confirm\",\"plan\":\(Self.quote())}".utf8)) : (200, response)
        })
        manager.select("b"); manager.regenerationPlanner.selected = "colab-L4"
        let operation = Task { try await manager.regenerate() }
        try await awaitConfirmation(manager.regenerationPlanner, units: 2.345)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        manager.regenerationPlanner.resolve(true)
        try await operation.value
        let requests = ComputeProtocol.fixture.requests
        XCTAssertEqual(requests.map { $0.url!.path }, ["/blind/b/generate", "/blind/b/generate"])
        XCTAssertEqual(try body(requests[0])["compute"] as? String, "colab-L4")
        XCTAssertEqual(try body(requests[1])["approved_device"] as? String, "colab-L4")
        XCTAssertEqual(try body(requests[1])["approved_units"] as? Double, 2.345)
        XCTAssertEqual(manager.test?.job_id, "new-job")
        XCTAssertFalse(manager.busy)
        XCTAssertNil(manager.regenerationPlanner.selected)
    }
    @MainActor func testBlindRegenerationCancellationPreservesExistingRecord() async throws {
        let manager = BlindStore(api: api { _, _ in (409, Data("{\"code\":\"confirm_required\",\"detail\":\"confirm\",\"plan\":\(Self.quote())}".utf8)) })
        manager.select("b")
        manager.test = BlindTest(id: "b", title: "old", created: "", character: "alice", eval_set: "mini", repeats: 1, ref_id: "r", status: "failed", job_id: "old-job", n_questions: 3, n_ready: 1, n_voted: 1, revealed: false, questions: [])
        let operation = Task { try await manager.regenerate() }
        try await awaitConfirmation(manager.regenerationPlanner, units: 2.345)
        manager.regenerationPlanner.resolve(false)
        do { try await operation.value; XCTFail("Expected cancellation") } catch is CancellationError { }
        XCTAssertEqual(manager.test?.job_id, "old-job")
        XCTAssertEqual(manager.test?.n_ready, 1)
        XCTAssertEqual(manager.test?.n_voted, 1)
        XCTAssertEqual(ComputeProtocol.fixture.count, 1)
        XCTAssertFalse(manager.busy)
    }
    @MainActor func testWorkBatchAndLineGenerationUseApprovalRetry() async throws {
        for singleLine in [false, true] {
            let work = Work(id: "w", title: "作品", created: "", updated: "", lines: [WorkLine(id: "line", text: "台词")])
            let workData = try JSONEncoder().encode(work)
            let jobData = try JSONEncoder().encode(Job(id: "job", kind: "work_generate", target: "w", status: "succeeded", progress: 1, stage: "完成", created: ""))
            let studio = StudioStore(defaults: UserDefaults(suiteName: "r4-work-" + UUID().uuidString)!)
            let counter = InviteRequests()
            studio.api = api { request, _ in
                let post = counter.record(request)
                if request.httpMethod == "POST" {
                    if post == 1 { return (409, Data("{\"code\":\"confirm_required\",\"detail\":\"confirm\",\"plan\":\(Self.quote())}".utf8)) }
                    return (200, jobData)
                }
                return (200, request.url!.path == "/works" ? Data(#"{"works":[]}"#.utf8) : workData)
            }
            studio.work = work; studio.workPlanner.selected = "colab-L4"
            let operation = Task {
                if singleLine { try await studio.regenerate(work.lines[0]) }
                else { try await studio.generateWork(lineIDs: ["line"]) }
            }
            try await awaitConfirmation(studio.workPlanner, units: 2.345)
            studio.workPlanner.resolve(true)
            try await operation.value
            let posts = ComputeProtocol.fixture.requests.filter { $0.httpMethod == "POST" }
            XCTAssertEqual(posts.count, 2)
            XCTAssertEqual(posts[0].url!.path, singleLine ? "/works/w/lines/line/regenerate" : "/works/w/generate")
            XCTAssertEqual(try body(posts[1])["approved"] as? Bool, true)
            XCTAssertEqual(try body(posts[1])["approved_device"] as? String, "colab-L4")
            XCTAssertEqual(try body(posts[1])["approved_units"] as? Double, 2.345)
            XCTAssertEqual(try body(posts[1])["n"] as? Int, 3)
            if !singleLine { XCTAssertEqual(try body(posts[1])["line_ids"] as? [String], ["line"]) }
            XCTAssertFalse(studio.workBusy)
            XCTAssertNil(studio.workPlanner.selected)
        }
    }
    @MainActor func testWorkApprovalCancelKeepsExistingRender() async throws {
        let studio = StudioStore(defaults: UserDefaults(suiteName: "r4-work-cancel-" + UUID().uuidString)!)
        let render = RenderResult(work_id: "w", rendered_at: "", duration: 1, wav_url: "old.wav", mp3_url: "old.mp3", srt_url: "old.srt", lrc_url: "old.lrc", lines: [])
        studio.work = Work(id: "w", title: "作品", created: "", updated: "", lines: [WorkLine(id: "line", text: "台词")], render: render)
        let saved = try JSONEncoder().encode(studio.work!)
        studio.api = api { request, _ in
            if request.httpMethod == "POST" { return (409, Data("{\"code\":\"confirm_required\",\"detail\":\"confirm\",\"plan\":\(Self.quote())}".utf8)) }
            return (200, request.url!.path == "/works" ? Data(#"{"works":[]}"#.utf8) : saved)
        }
        let operation = Task { try await studio.generateWork(lineIDs: ["line"]) }
        try await awaitConfirmation(studio.workPlanner, units: 2.345)
        studio.workPlanner.resolve(false)
        do { try await operation.value; XCTFail("Expected cancellation") } catch is CancellationError { }
        XCTAssertEqual(studio.work?.render?.wav_url, "old.wav")
        XCTAssertFalse(studio.workBusy)
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.httpMethod == "POST" }.count, 1)
    }
    @MainActor func testWorkPlanCountsModelsPerSelectedLine() {
        let studio = StudioStore(defaults: UserDefaults(suiteName: "r4-work-plan-" + UUID().uuidString)!)
        studio.work = Work(id: "w", title: "作品", created: "", updated: "", lines: [WorkLine(engine: "v2pro", id: "a", text: "a"), WorkLine(engine: "v4", id: "b", text: "b"), WorkLine(engine: "v4", id: "c", text: " ")])
        XCTAssertEqual(studio.workPlan(lineIDs: nil).items, 6)
        XCTAssertEqual(studio.workPlan(lineIDs: ["b"]).parts?.first?.engine, "v4")
        XCTAssertEqual(studio.workPlan(lineIDs: ["b"]).parts?.first?.items, 3)
    }
    @MainActor func testManualPairedOfflineDeviceStillReachesConnectedStep() async throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { request, _ in
            if request.url!.path == "/compute/invites" { return (200, Data(Self.inviteJSON("manual").utf8)) }
            return (200, Data(#"{"id":"manual","state":"connected","expires_in":0,"redeemed":true,"device":{"id":"lan","name":"书房电脑","type":"lan","status":"offline","engines":["v2pro"],"speeds":{},"last_seen":null}}"#.utf8))
        }))
        await flow.generate(); flow.manualAddress = "http://study.local:9880"; await flow.manual()
        XCTAssertEqual(flow.step, 4)
        XCTAssertEqual(flow.device?.status, "offline")
        XCTAssertFalse(flow.expired)
        XCTAssertNil(flow.manualError)
    }
    @MainActor func testInstallingFollowsRedeemedAndJumpsToStepThreeOnce() throws {
        let flow = DeviceInviteFlow(compute: ComputeStore(api: api { _, _ in (500, Data()) }))
        flow.step = 2
        func status(_ json: String) throws -> ComputeInviteStatus { try JSONDecoder().decode(ComputeInviteStatus.self, from: Data(json.utf8)) }
        flow.apply(try status(#"{"id":"i","state":"waiting","expires_in":0,"redeemed":false}"#))   // 没核销：不是「正在安装」
        XCTAssertFalse(flow.installing)
        XCTAssertEqual(flow.step, 2)
        flow.apply(try status(#"{"id":"i","state":"waiting","expires_in":0,"redeemed":true}"#))
        XCTAssertTrue(flow.installing)
        XCTAssertEqual(flow.step, 3)
        XCTAssertNil(flow.deadline)
        flow.step = 1                                                                              // 装到一半卡住，回第 1 步换网络
        flow.apply(try status(#"{"id":"i","state":"waiting","expires_in":0,"redeemed":true}"#))
        XCTAssertTrue(flow.installing)
        XCTAssertEqual(flow.step, 1)                                                               // 下一次轮询不再拽回第 3 步
    }
    @MainActor func testCloudLoginDiscardsOnlyAccountsAddedByThisLogin() async throws {
        let api = api { request, _ in
            request.httpMethod == "DELETE" ? (204, Data()) : (200, Data(Stage2Fixtures.cloud.utf8))
        }
        let compute = ComputeStore(api: api)
        let cloud = try JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
        var account = cloud.accounts[0]; account.id = "stale-cache-existing"
        let addition = CloudLoginAddition()
        // 1. Existing account (added == false), even when absent from local cloud cache: never deletes.
        addition.begin("L-existing")
        addition.finish(CloudLoginStatus(state: "done", account: account, added: false, error: nil))
        try await addition.discard(compute)
        XCTAssertEqual(ComputeProtocol.fixture.count, 0)
        // 2. Missing `added` field (older core): never deletes.
        addition.begin("L-missing")
        let decodedMissing = try JSONDecoder().decode(CloudLoginStatus.self, from: Data(#"{"state":"done","account":null,"error":null}"#.utf8))
        XCTAssertNil(decodedMissing.added)
        addition.finish(decodedMissing)
        try await addition.discard(compute)
        XCTAssertEqual(ComputeProtocol.fixture.count, 0)
        // 3. Failed login: nothing to delete.
        addition.begin("L-failed")
        addition.finish(CloudLoginStatus(state: "failed", account: nil, added: false, error: "bad code"))
        try await addition.discard(compute)
        XCTAssertEqual(ComputeProtocol.fixture.count, 0)
        // 4. Newly added account (added == true): calls DELETE /cloud/accounts/login/{id} once.
        addition.begin("L-new")
        addition.finish(CloudLoginStatus(state: "done", account: account, added: true, error: nil))
        try await addition.discard(compute)
        try await addition.discard(compute)
        // 5. Cancel before done (still waiting after submitting code): calls DELETE /cloud/accounts/login/{id}.
        addition.begin("L-waiting")
        addition.finish(CloudLoginStatus(state: "waiting", account: nil, added: false, error: nil))
        try await addition.discard(compute)
        XCTAssertEqual(
            ComputeProtocol.fixture.requests.filter { $0.httpMethod == "DELETE" }.map(\.url!.path),
            ["/cloud/accounts/login/L-new", "/cloud/accounts/login/L-waiting"]
        )
    }
    @MainActor func testCloudLoginDiscardFailureIsReported() async throws {
        let api = api { request, _ in
            request.httpMethod == "DELETE" ? (409, Data(#"{"detail":"这个账号登录后试开的卡还没释放完，稍后再移除"}"#.utf8)) : (200, Data(Stage2Fixtures.cloud.utf8))
        }
        let compute = ComputeStore(api: api)
        let addition = CloudLoginAddition()
        addition.begin("L-409")
        addition.finish(CloudLoginStatus(state: "done", account: nil, added: true, error: nil))
        do { try await addition.discard(compute); XCTFail("删不掉时要报出来") }
        catch { XCTAssertEqual(error.localizedDescription, "这个账号登录后试开的卡还没释放完，稍后再移除") }
        try await addition.discard(compute)                  // 报过一次就不再删（账号留在列表里，用户自己处理）
        XCTAssertEqual(ComputeProtocol.fixture.requests.filter { $0.httpMethod == "DELETE" }.count, 1)
        // 502 from wizard cancel must surface as a global error via studio.perform, not only accountError.
        let api502 = self.api { request, _ in
            request.httpMethod == "DELETE" ? (502, Data(#"{"detail":"delete failed"}"#.utf8)) : (200, Data(Stage2Fixtures.cloud.utf8))
        }
        let studio = StudioStore(defaults: MemoryDefaults()); studio.api = api502
        let wiz = CloudLoginAddition()
        wiz.begin("L-502")
        wiz.finish(CloudLoginStatus(state: "done", account: nil, added: true, error: nil))
        await studio.perform { try await wiz.discard(studio.compute) }.value
        XCTAssertEqual(studio.errorMessage, "这个账号的授权没删掉，已先停用，可以再试")
        XCTAssertNil(studio.compute.accountError)
        XCTAssertNotNil(studio.compute.cloud)
    }
    @MainActor func testWorkPickerEstimatesEveryLineWhateverIsSelected() {
        let studio = StudioStore(defaults: UserDefaults(suiteName: "work-picker-" + UUID().uuidString)!)
        studio.work = Work(id: "w", title: "作品", created: "", updated: "", lines: [WorkLine(engine: "v2pro", id: "a", text: "a"), WorkLine(engine: "v4", id: "b", text: "b"), WorkLine(engine: "v4", id: "c", text: "c")])
        studio.selectedWorkLineIDs = ["b"]
        XCTAssertEqual(studio.workPickerInput.items, 9)      // 「生成全部」提交的三行，每行 3 个候选
        XCTAssertEqual(studio.workPickerAction, "生成全部 3 行")
        XCTAssertEqual(computeTaskHeading(studio.workPickerInput, action: studio.workPickerAction, title: "作品"), "这一次在哪生成全部 3 行（作品 · 9 句 v2Pro / v4）")
    }
}

private final class SwitchingState: @unchecked Sendable {
    let lock = NSLock()
    let releaseFirstCloud = DispatchSemaphore(value: 0)
    private var cloudCalls = 0
    private var _firstCloudStarted = false
    var requests: [URLRequest] = []
    var firstCloudStarted: Bool {
        lock.lock(); defer { lock.unlock() }
        return _firstCloudStarted
    }
    func markFirstCloudStarted() {
        lock.lock(); _firstCloudStarted = true; lock.unlock()
    }
    func nextCloudCall() -> Int {
        lock.lock(); defer { lock.unlock() }
        cloudCalls += 1
        return cloudCalls
    }
    func record(_ request: URLRequest) {
        lock.lock(); requests.append(request); lock.unlock()
    }
}

private final class SwitchingProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var state: SwitchingState!
    private let cancelledLock = NSLock()
    private var cancelled = false
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let req = request
        let state = Self.state!
        state.record(req)
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            if req.url?.host == "127.0.0.1" {
                self.finishIfActive { $0.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost)) }
                return
            }
            let path = req.url?.path ?? ""
            if path == "/cloud", state.nextCloudCall() == 1 {
                state.markFirstCloudStarted()
                _ = state.releaseFirstCloud.wait(timeout: .now() + 5)
            }
            let body: String
            switch path {
            case "/health": body = #"{"status":"ok","time":"","workers":{},"version":"0.1.3","commit":"fixture"}"#
            case "/models": body = Stage2Fixtures.models
            case "/jobs": body = #"{"jobs":[],"total":0}"#
            case "/compute": body = Stage2Fixtures.compute
            case "/library": body = Stage2Fixtures.library
            case "/cloud": body = Stage2Fixtures.cloud
            case "/system/info": body = Stage2Fixtures.systemInfo
            case "/system/versions": body = #"{"studio":{"current":"0.1.3","latest":"0.1.3","notes_url":null},"core":{"current":"0.1.3"}}"#
            default: body = "{}"
            }
            self.finishIfActive { client in
                client.urlProtocol(self, didReceive: HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
                client.urlProtocol(self, didLoad: Data(body.utf8))
                client.urlProtocolDidFinishLoading(self)
            }
        }
    }
    override func stopLoading() {
        cancelledLock.lock(); cancelled = true; cancelledLock.unlock()
    }
    private func finishIfActive(_ work: (URLProtocolClient) -> Void) {
        cancelledLock.lock()
        let isCancelled = cancelled
        cancelledLock.unlock()
        guard !isCancelled, let client else { return }
        work(client)
    }
}

private struct InspectedControls {
    var menuButtons: [Bool] = []
    var textFields: [(value: String, enabled: Bool)] = []
    var segmentedControls: [Bool] = []
}

@MainActor
private func inspectControls<V: View>(_ view: V, studio: StudioStore, size: CGSize = CGSize(width: 960, height: 740)) async throws -> InspectedControls {
    let window = NSWindow(contentRect: NSRect(origin: CGPoint(x: 100, y: 100), size: size), styleMask: [.titled, .closable], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentViewController = NSHostingController(rootView: view.environment(studio).frame(width: size.width, height: size.height))
    window.makeKeyAndOrderFront(nil)
    defer { window.orderOut(nil); window.contentViewController = nil; window.close() }
    let root = try XCTUnwrap(window.contentView)
    root.layoutSubtreeIfNeeded()
    window.displayIfNeeded()
    try await Task.sleep(for: .milliseconds(120))
    root.layoutSubtreeIfNeeded()
    var result = InspectedControls()
    func walk(_ view: NSView) {
        if let popup = view as? NSPopUpButton {
            result.menuButtons.append(popup.isEnabled)
        } else if let seg = view as? NSSegmentedControl {
            result.segmentedControls.append(seg.isEnabled)
        } else if let field = view as? NSTextField, field.isEditable {
            result.textFields.append((field.stringValue, field.isEnabled))
        }
        for sub in view.subviews { walk(sub) }
    }
    walk(root)
    return result
}

extension ComputeTests {
    @MainActor func testClientModeAllowsSettingsEditsExceptLocalFolderPicker() async throws {
        let state = ConnectionFixtureState()
        let api = api { request, _ in
            switch (request.httpMethod ?? "GET", request.url!.path) {
            case ("POST", "/library/rescan"), ("POST", "/library/claim"):
                return (200, Data(Stage2Fixtures.library.utf8))
            case ("PUT", "/compute/auto"):
                return (200, Data(#"{"mode":"auto","device":null,"ask_over":5,"ask_month":600}"#.utf8))
            case ("POST", "/bench"):
                return (200, Data(#"{"id":"bench-1","kind":"bench","target":"local","status":"queued","progress":0,"stage":"排队中","created":"","log_tail":[],"completed":[]}"#.utf8))
            case ("PUT", "/cloud/settings"):
                return (200, Data(#"{"gpu_pref":"auto","max_minutes":null,"keep_minutes":10}"#.utf8))
            case ("POST", "/cloud/accounts/a/disable"):
                return (204, Data())
            default:
                return state.respond(request)
            }
        }
        let defaults = MemoryDefaults()
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { false })
        connection.role = "client"; connection.address = "http://fixture"; connection.alias = "mini"; connection.save()
        let studio = StudioStore(defaults: defaults, connection: connection)
        studio.api = api
        await studio.loadInitial()
        XCTAssertTrue(studio.connection.isClient)
        XCTAssertTrue(studio.connected)
        XCTAssertFalse(studio.shouldPromptLocalLibrarySelection)
        XCTAssertEqual(studio.connection.libraryFolderHint, "在 mini 上选择文件夹")
        XCTAssertEqual(studio.connection.libraryConflictHint, "在 mini 上查看")

        // Compute settings in client mode: device menus (⋯) and auto-selection text fields are enabled
        let computeUI = try await inspectControls(ComputeSettingsView(), studio: studio)
        XCTAssertEqual(computeUI.menuButtons.count, 2)
        XCTAssertTrue(computeUI.menuButtons.allSatisfy { $0 })
        XCTAssertEqual(computeUI.textFields.map(\.value), ["2", "500"])
        XCTAssertTrue(computeUI.textFields.allSatisfy(\.enabled))

        // Cloud settings in client mode: GPU preferences fields and account menus (⋯) are enabled
        studio.compute.cloud?.settings.max_minutes = 120
        studio.compute.cloud?.settings.keep_minutes = 10
        let cloudGPUUI = try await inspectControls(CloudSettingsView(initialPerspective: "gpu"), studio: studio)
        XCTAssertEqual(cloudGPUUI.textFields.map(\.value), ["2", "10"])
        XCTAssertTrue(cloudGPUUI.textFields.allSatisfy(\.enabled))
        let cloudAccountsUI = try await inspectControls(CloudSettingsView(initialPerspective: "account"), studio: studio)
        XCTAssertEqual(cloudAccountsUI.menuButtons.count, 3)
        XCTAssertTrue(cloudAccountsUI.menuButtons.allSatisfy { $0 })

        // Library settings (unselected): local Drive/local folder picker is hidden in client mode and shown in control mode
        var unselected = try JSONDecoder().decode(LibraryInfo.self, from: Data(Stage2Fixtures.library.utf8))
        unselected.selected = false
        studio.compute.library = unselected
        let clientLibraryUI = try await inspectControls(LibrarySettingsView(), studio: studio)
        XCTAssertTrue(clientLibraryUI.segmentedControls.isEmpty)
        studio.connection.role = "control"
        XCTAssertTrue(studio.shouldPromptLocalLibrarySelection)
        let controlLibraryUI = try await inspectControls(LibrarySettingsView(), studio: studio)
        XCTAssertEqual(controlLibraryUI.segmentedControls.count, 1)
        studio.connection.role = "client"

        // Client-mode mutations send requests directly to the controller
        try await studio.compute.rescan()
        try await studio.compute.claim()
        try await studio.compute.saveAutomatic(AutoSettings(mode: "auto", device: nil, ask_over: 5, ask_month: 600))
        _ = try await studio.compute.benchmark(device: "local", engine: "v4")
        try await studio.compute.saveCloud(CloudSettings(gpu_pref: "auto", max_minutes: nil, keep_minutes: 10))
        try await studio.compute.disableAccount("a")
        let paths = ComputeProtocol.fixture.requests.map { "\($0.httpMethod ?? "GET") \($0.url!.path)" }
        for expected in ["POST /library/rescan", "POST /library/claim", "PUT /compute/auto", "POST /bench", "PUT /cloud/settings", "POST /cloud/accounts/a/disable"] {
            XCTAssertTrue(paths.contains(expected), "Missing controller request: \(expected)")
        }
    }

    @MainActor func testClientToMissingControlAndBackToClientReconnectsAndShowsMissingCoreHint() async throws {
        let switching = SwitchingState()
        SwitchingProtocol.state = switching
        defer { switching.releaseFirstCloud.signal() }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SwitchingProtocol.self]
        let defaults = MemoryDefaults()
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { false })
        let studio = StudioStore(defaults: defaults, connection: connection)
        studio.api = APIClient(baseURL: "http://127.0.0.1:9880", session: URLSession(configuration: config))

        // 1. Start in client mode; wait until loadInitial is mid-flight inside /cloud (initialLoading == true)
        try studio.applyConnection(role: "client", address: "http://mini.fixture:9980", alias: "mini", wakeMAC: "", broadcast: "192.168.1.255")
        for _ in 0..<200 {
            if switching.firstCloudStarted { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        XCTAssertTrue(switching.firstCloudStarted)
        XCTAssertTrue(studio.connected)

        // 2. Switch to control mode on a machine without local core installed
        try studio.applyConnection(role: "control", address: "http://mini.fixture:9980", alias: "mini", wakeMAC: "", broadcast: "192.168.1.255")
        XCTAssertFalse(studio.connected)
        XCTAssertTrue(studio.connection.localCoreMissing)
        XCTAssertEqual(studio.computeSummary, "本机未安装主控服务")
        XCTAssertEqual(studio.unavailableTitle, "本机未安装主控服务")
        XCTAssertTrue(studio.unavailableDescription.contains("还没安装主控服务"))
        XCTAssertTrue(studio.connection.localCoreNotice.contains("还没有安装主控服务"))
        for copy in [studio.computeSummary, studio.unavailableTitle, studio.unavailableDescription, studio.connection.localCoreNotice] {
            XCTAssertFalse(copy.contains("9880"))
            XCTAssertFalse(copy.lowercased().contains("core"))
        }

        // Simulate a view/store request failing against unreachable local control
        studio.errorMessage = "未能连接到服务器。"
        studio.status = "未能连接到服务器。"
        studio.compute.error = "未能连接到服务器。"

        // 3. Switch back to client while the first /cloud request was still blocked, then release it
        try studio.applyConnection(role: "client", address: "http://mini.fixture:9980", alias: "mini", wakeMAC: "", broadcast: "192.168.1.255")
        for _ in 0..<200 {
            if studio.connected && !studio.models.isEmpty && !studio.compute.devices.isEmpty { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        switching.releaseFirstCloud.signal()
        try await Task.sleep(for: .milliseconds(60))

        XCTAssertEqual(studio.api.baseURL, "http://mini.fixture:9980")
        XCTAssertFalse(studio.connection.localCoreMissing)
        XCTAssertTrue(studio.connected)
        XCTAssertFalse(studio.models.isEmpty)
        XCTAssertFalse(studio.compute.devices.isEmpty)
        XCTAssertNil(studio.errorMessage)
        XCTAssertEqual(studio.status, "准备就绪")
        XCTAssertNil(studio.compute.error)
        XCTAssertNil(studio.compute.cloudError)
        XCTAssertFalse(studio.computeSummary.contains("主控不在线"))
        XCTAssertFalse(studio.computeSummary.contains("本机未安装主控服务"))

        // 4. Same-URL retry while disconnected still reconnects
        studio.connected = false
        try studio.setBaseURL("http://mini.fixture:9980")
        for _ in 0..<100 {
            if studio.connected { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        XCTAssertTrue(studio.connected)
    }

    @MainActor func testWorkerOnlyDotTTSCoreDirectoryDoesNotCountAsInstalledController() throws {
        let home = FileManager.default.temporaryDirectory.appending(path: "tts-home-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }

        XCTAssertFalse(StudioConnection.defaultLocalCoreInstalled(home: home))

        // Worker-only install (~/.tts-core and worker launchd agent) must NOT count as local controller
        try FileManager.default.createDirectory(at: home.appending(path: ".tts-core"), withIntermediateDirectories: true)
        let agents = home.appending(path: "Library/LaunchAgents")
        try FileManager.default.createDirectory(at: agents, withIntermediateDirectories: true)
        try Data().write(to: agents.appending(path: "com.dreaife.tts-core-worker.plist"))
        XCTAssertFalse(StudioConnection.defaultLocalCoreInstalled(home: home))
        let connection = StudioConnection(defaults: MemoryDefaults(), localCoreInstalled: { StudioConnection.defaultLocalCoreInstalled(home: home) })
        XCTAssertTrue(connection.localCoreMissing)

        // Controller LaunchAgent counts as installed
        let controllerPlist = agents.appending(path: "com.dreaife.tts-core.plist")
        try Data().write(to: controllerPlist)
        XCTAssertTrue(StudioConnection.defaultLocalCoreInstalled(home: home))
        XCTAssertFalse(connection.localCoreMissing)

        // Empty controller directory from a failed install must NOT count; valid current/core/ttsapi counts
        try FileManager.default.removeItem(at: controllerPlist)
        XCTAssertFalse(StudioConnection.defaultLocalCoreInstalled(home: home))
        try FileManager.default.createDirectory(at: home.appending(path: "Library/Application Support/TTSStudio/core"), withIntermediateDirectories: true)
        XCTAssertFalse(StudioConnection.defaultLocalCoreInstalled(home: home))
        try FileManager.default.createDirectory(at: home.appending(path: "Library/Application Support/TTSStudio/core/current/core/ttsapi"), withIntermediateDirectories: true)
        XCTAssertTrue(StudioConnection.defaultLocalCoreInstalled(home: home))
    }

    @MainActor func testDelayedResponsesFromPreviousControllerDoNotOverwriteNewController() async throws {
        let state = TwoControllerState()
        TwoControllerProtocol.state = state
        defer { state.releaseAll() }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [TwoControllerProtocol.self]
        let defaults = MemoryDefaults()
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { true })
        connection.role = "client"; connection.address = "http://controller-a"; connection.alias = "mini-a"; connection.save()
        let studio = StudioStore(defaults: defaults, connection: connection)
        studio.api = APIClient(baseURL: "http://controller-a", session: URLSession(configuration: config))

        // 1. Kick off /compute, /cloud, and /library/rescan against controller A; all three block until released
        let oldCompute = Task { try? await studio.compute.refreshDevices() }
        let oldCloud = Task { await studio.compute.refreshCloud() }
        let oldRescan = Task { try? await studio.compute.rescan() }
        for _ in 0..<200 {
            if state.allControllerARequestsStarted { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(state.allControllerARequestsStarted)
        XCTAssertTrue(studio.compute.libraryChecking)

        // 2. Switch to controller B and wait for B to finish loading
        try studio.setBaseURL("http://controller-b")
        for _ in 0..<200 {
            if studio.connected
                && studio.compute.devices.first?.name == "controller-b-mini"
                && studio.compute.cloud?.accounts.first?.email == "new-b@example.com"
                && studio.compute.library?.path == "/library/controller-b" { break }
            try await Task.sleep(for: .milliseconds(15))
        }
        XCTAssertEqual(studio.compute.devices.first?.name, "controller-b-mini")
        XCTAssertEqual(studio.compute.cloud?.accounts.first?.email, "new-b@example.com")
        XCTAssertEqual(studio.compute.library?.path, "/library/controller-b")
        XCTAssertFalse(studio.compute.libraryChecking)

        // 3. Release controller A's delayed responses and wait for those old tasks to complete
        state.releaseAll()
        await oldCompute.value
        await oldCloud.value
        await oldRescan.value

        // Controller B's data, errors, and loading flags must remain untouched
        XCTAssertEqual(studio.compute.devices.first?.name, "controller-b-mini")
        XCTAssertEqual(studio.compute.automatic.ask_over, 22)
        XCTAssertEqual(studio.compute.cloud?.accounts.first?.email, "new-b@example.com")
        XCTAssertEqual(studio.compute.cloud?.total_balance, 222)
        XCTAssertEqual(studio.compute.library?.path, "/library/controller-b")
        XCTAssertFalse(studio.compute.libraryChecking)
        XCTAssertNil(studio.compute.error)
        XCTAssertNil(studio.compute.cloudError)
    }

    @MainActor func testSameAddressReconnectPreservesInFlightLoadingFlags() async throws {
        let state = TwoControllerState(gatedPaths: [])
        TwoControllerProtocol.state = state
        defer { state.releaseAll() }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [TwoControllerProtocol.self]
        let defaults = MemoryDefaults()
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { true })
        connection.role = "client"; connection.address = "http://controller-a"; connection.alias = "mini-a"; connection.save()
        let studio = StudioStore(defaults: defaults, connection: connection)
        studio.api = APIClient(baseURL: "http://controller-a", session: URLSession(configuration: config))

        // 1. Initial connection succeeds cleanly
        await studio.loadInitial()
        XCTAssertTrue(studio.connected)
        XCTAssertFalse(studio.compute.loading)
        XCTAssertFalse(studio.compute.libraryChecking)
        XCTAssertFalse(studio.versions.checking)

        // 2. Start delayed cloud refresh, library rescan, and version check on controller A
        state.armGates(["/cloud", "/library/rescan", "/system/versions"])
        let refreshTask = Task { await studio.compute.refresh() }
        let rescanTask = Task { try? await studio.compute.rescan() }
        let versionTask = Task { await studio.versions.checkLatest(api: studio.api, refresh: true) }
        for _ in 0..<200 {
            if state.hasStarted(["/cloud", "/library/rescan", "/system/versions"]) { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(state.hasStarted(["/cloud", "/library/rescan", "/system/versions"]))
        XCTAssertTrue(studio.compute.loading)
        XCTAssertTrue(studio.compute.libraryChecking)
        XCTAssertTrue(studio.versions.checking)

        // 3. Two health failures mark disconnected; saving the same address reconnects
        state.failNextHealth(2)
        await studio.refreshConnection()
        await studio.refreshConnection()
        XCTAssertFalse(studio.connected)

        try studio.applyConnection(role: "client", address: "http://controller-a", alias: "mini-a", wakeMAC: "", broadcast: "192.168.1.255")
        for _ in 0..<200 {
            if studio.connected { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(studio.connected)

        // 4. Release the three in-flight requests; all loading flags must clear and write back
        state.releaseAll()
        await refreshTask.value
        await rescanTask.value
        await versionTask.value
        XCTAssertFalse(studio.compute.loading)
        XCTAssertFalse(studio.compute.libraryChecking)
        XCTAssertFalse(studio.versions.checking)
        XCTAssertEqual(studio.compute.library?.path, "/library/controller-a")
        XCTAssertEqual(studio.compute.cloud?.accounts.first?.email, "old-a@example.com")
        XCTAssertEqual(studio.versions.latest?.studio.latest, "0.1.3")
    }

    @MainActor func testEnqueuedPerformActionBeforeControllerSwitchDoesNotSendPatchToNewController() async throws {
        let state = TwoControllerState(gatedPaths: [])
        TwoControllerProtocol.state = state
        defer { state.releaseAll() }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [TwoControllerProtocol.self]
        let defaults = MemoryDefaults()
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { true })
        connection.role = "client"; connection.address = "http://controller-a"; connection.alias = "mini-a"; connection.save()
        let studio = StudioStore(defaults: defaults, connection: connection)
        studio.api = APIClient(baseURL: "http://controller-a", session: URLSession(configuration: config))

        await studio.loadInitial()
        XCTAssertEqual(studio.compute.devices.first?.name, "controller-a-mini")

        // Enqueue pause on A's device, then switch to B before the Task begins executing
        let pauseTask = studio.perform {
            try await studio.compute.patch("local", fields: ["paused": .bool(true)])
        }
        try studio.setBaseURL("http://controller-b")
        await pauseTask.value
        for _ in 0..<200 {
            if studio.connected && studio.compute.devices.first?.name == "controller-b-mini" { break }
            try await Task.sleep(for: .milliseconds(10))
        }

        XCTAssertEqual(studio.compute.devices.first?.name, "controller-b-mini")
        XCTAssertTrue(state.patchHosts.isEmpty)
    }
}

private final class TwoControllerState: @unchecked Sendable {
    private let lock = NSLock()
    private var gated: Set<String>
    private var started: Set<String> = []
    private var released = false
    private var healthFailures = 0
    private var _patchHosts: [String] = []
    private var gates: [String: DispatchSemaphore] = [:]

    init(gatedPaths: Set<String> = ["/compute", "/cloud", "/library/rescan"]) {
        self.gated = gatedPaths
        for path in gatedPaths { gates[path] = DispatchSemaphore(value: 0) }
    }

    var allControllerARequestsStarted: Bool {
        hasStarted(["/compute", "/cloud", "/library/rescan"])
    }

    var patchHosts: [String] {
        lock.lock(); defer { lock.unlock() }
        return _patchHosts
    }

    func hasStarted(_ paths: Set<String>) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return started.isSuperset(of: paths)
    }

    func armGates(_ paths: Set<String>) {
        lock.lock()
        gated = paths
        started.removeAll()
        released = false
        gates = Dictionary(uniqueKeysWithValues: paths.map { ($0, DispatchSemaphore(value: 0)) })
        lock.unlock()
    }

    func failNextHealth(_ count: Int) {
        lock.lock(); healthFailures = count; lock.unlock()
    }

    func consumeHealthFailure() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if healthFailures > 0 { healthFailures -= 1; return true }
        return false
    }

    func recordPatch(host: String) {
        lock.lock(); _patchHosts.append(host); lock.unlock()
    }

    func waitOnControllerA(_ path: String) {
        let gate: DispatchSemaphore?
        lock.lock()
        guard gated.contains(path) else { lock.unlock(); return }
        started.insert(path)
        gate = released ? nil : gates[path]
        lock.unlock()
        _ = gate?.wait(timeout: .now() + 5)
    }

    func releaseAll() {
        lock.lock()
        let first = !released
        released = true
        let pending = Array(gates.values)
        lock.unlock()
        guard first else { return }
        for gate in pending { gate.signal() }
    }
}

private final class TwoControllerProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var state: TwoControllerState!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let req = request
        let state = Self.state!
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            let host = req.url?.host ?? ""
            let isA = host == "controller-a"
            let path = req.url?.path ?? ""
            if req.httpMethod == "PATCH" { state.recordPatch(host: host) }
            if isA { state.waitOnControllerA(path) }
            if path == "/health", state.consumeHealthFailure() {
                guard let client = self.client else { return }
                client.urlProtocol(self, didReceive: HTTPURLResponse(url: req.url!, statusCode: 503, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
                client.urlProtocol(self, didLoad: Data(#"{"detail":"暂不可用"}"#.utf8))
                client.urlProtocolDidFinishLoading(self)
                return
            }
            let body: String
            switch path {
            case "/health":
                body = #"{"status":"ok","time":"","workers":{},"version":"0.1.3","commit":"fixture"}"#
            case "/models":
                body = Stage2Fixtures.models
            case "/jobs":
                body = #"{"jobs":[],"total":0}"#
            case "/compute":
                let name = isA ? "controller-a-mini" : "controller-b-mini"
                let askOver = isA ? "11" : "22"
                body = Stage2Fixtures.compute
                    .replacingOccurrences(of: "\"name\":\"studio-host\"", with: "\"name\":\"\(name)\"")
                    .replacingOccurrences(of: "\"ask_over\":2", with: "\"ask_over\":\(askOver)")
            case "/cloud":
                let email = isA ? "old-a@example.com" : "new-b@example.com"
                let total = isA ? "111" : "222"
                body = Stage2Fixtures.cloud
                    .replacingOccurrences(of: "\"email\":\"studio@example.com\"", with: "\"email\":\"\(email)\"")
                    .replacingOccurrences(of: "\"total_balance\":4068", with: "\"total_balance\":\(total)")
            case "/library", "/library/rescan":
                let libPath = isA ? "/library/controller-a" : "/library/controller-b"
                body = Stage2Fixtures.library
                    .replacingOccurrences(of: "/Users/test/Library/CloudStorage/GoogleDrive-test/My Drive/ai/tts-library", with: libPath)
            case "/system/info":
                body = Stage2Fixtures.systemInfo
            case "/system/versions":
                body = #"{"studio":{"current":"0.1.3","latest":"0.1.3","notes_url":null},"core":{"current":"0.1.3"}}"#
            default:
                if req.httpMethod == "PATCH" {
                    let list = try! JSONDecoder().decode(ComputeList.self, from: Data(Stage2Fixtures.compute.utf8))
                    body = String(decoding: try! JSONEncoder().encode(list.devices[0]), as: UTF8.self)
                } else {
                    body = "{}"
                }
            }
            guard let client = self.client else { return }
            client.urlProtocol(self, didReceive: HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client.urlProtocol(self, didLoad: Data(body.utf8))
            client.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() { }
}
