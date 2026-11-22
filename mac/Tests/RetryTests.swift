import XCTest
@testable import TTSStudio

private final class RetryFixture: @unchecked Sendable {
    let lock = NSLock()
    private var attempts = 0
    let training: Bool
    init(training: Bool) { self.training = training }
    func response(_ request: URLRequest) -> (Int, [String: String], Data) {
        lock.lock(); defer { lock.unlock() }
        switch request.url!.path {
        case "/health":
            return (200, [:], Data((training ? #"{"status":"ok","time":"","training_job":"train-test","workers":{},"version":"0.1.1","commit":"fixture"}"# : #"{"status":"ok","time":"","workers":{},"version":"0.1.1","commit":"fixture"}"#).utf8))
        case "/gateway/status": return (200, [:], Data(#"{"reachable":false,"waking":true}"#.utf8))
        default:
            attempts += 1
            return attempts == 1 ? (503, ["Retry-After":"1"], Data(#"{"detail":"暂不可用"}"#.utf8)) : (200, [:], Data("完成".utf8))
        }
    }
    var count: Int { lock.lock(); defer { lock.unlock() }; return attempts }
}
private final class MockProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var fixture: RetryFixture!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, headers, data) = Self.fixture.response(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}
final class RetryTests: XCTestCase {
    @MainActor private func makeAPI(training: Bool) -> APIClient {
        MockProtocol.fixture = RetryFixture(training: training)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockProtocol.self]
        return APIClient(baseURL: "http://fixture", session: URLSession(configuration: config))
    }
    @MainActor func test503TrainingRetriesWithSpecificStatus() async throws {
        let api = makeAPI(training: true)
        var statuses: [String] = []
        var waits: [String?] = []
        api.onWaitState = { _, message in waits.append(message) }
        api.onStatus = { statuses.append($0) }
        let data = try await api.data(api.request("/probe", method: "POST"))
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "完成")
        XCTAssertEqual(MockProtocol.fixture.count, 2)
        XCTAssertTrue(waits.first??.contains("正在训练") == true)
        XCTAssertNil(waits.last!)
        XCTAssertTrue(statuses.contains { $0.contains("正在训练：train-test") && $0.contains("1 秒后重试") })
    }
    @MainActor func test503WakingWaitIsCancellable() async throws {
        let api = makeAPI(training: false)
        var statuses: [String] = []
        var waits: [String?] = []
        api.onWaitState = { _, message in waits.append(message) }
        api.onStatus = { statuses.append($0) }
        let request = try api.request("/probe")
        let task = Task { try await api.data(request) }
        try await Task.sleep(for: .milliseconds(100))
        task.cancel()
        do { _ = try await task.value; XCTFail("Cancelled retry should stop") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertEqual(MockProtocol.fixture.count, 1)
        XCTAssertTrue(waits.first??.contains("暂不可用") == true)
        XCTAssertNil(waits.last!)
        XCTAssertTrue(statuses.contains { $0.contains("暂不可用") })
    }
}
