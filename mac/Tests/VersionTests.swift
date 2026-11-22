import XCTest
import AppKit
@testable import TTSStudio

private final class UpdateFixture: @unchecked Sendable {
    let lock = NSLock()
    var requests: [URLRequest] = []
    var healthCount = 0
    var rejected = false
    var upToDate = false
    func response(_ request: URLRequest) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }
        requests.append(request)
        if request.url!.path == "/system/update" {
            if upToDate { return (200, Data(#"{"from":"0.1.1","to":"0.1.1"}"#.utf8)) }
            return rejected ? (409, Data(#"{"detail":"有任务正在运行"}"#.utf8)) : (202, Data(#"{"from":"0.1.0","to":"0.1.1"}"#.utf8))
        }
        healthCount += 1
        if healthCount == 1 { return (503, Data(#"{"detail":"重启中"}"#.utf8)) }
        return (200, Data((healthCount == 2 ? #"{"version":"0.1.0","commit":"old"}"# : #"{"version":"0.1.1","commit":"new"}"#).utf8))
    }
}
private final class UpdateProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var fixture: UpdateFixture!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, data) = Self.fixture.response(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}
final class VersionTests: XCTestCase {
    @MainActor func testTerminalRequestIsDataAndUsesBundledExecutable() throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let command = try TerminalUpdater.prepare(tag: "v0.1.1", directory: folder)
        XCTAssertEqual(try String(contentsOf: folder.appending(path: "update-tag"), encoding: .utf8), "v0.1.1")
        let reader = try String(contentsOf: command, encoding: .utf8)
        let tagLine = try XCTUnwrap(reader.split(separator: "\n").first { $0.hasPrefix("TAG_FILE=") })
        let readPath = String(tagLine.dropFirst("TAG_FILE=\"".count).dropLast()).replacingOccurrences(of: "$HOME", with: FileManager.default.homeDirectoryForCurrentUser.path)
        XCTAssertEqual(readPath, TerminalUpdater.tagFile.path)
        XCTAssertTrue(reader.contains("rm \"$TAG_FILE\""))
        XCTAssertEqual(command.lastPathComponent, "update.command")
        XCTAssertTrue(command.path.hasPrefix(Bundle.main.bundlePath))
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: command.path))
        let shell = TerminalUpdater.shellCommand(tag: "v0.1.1")
        XCTAssertTrue(shell.hasPrefix("/bin/bash '"))
        XCTAssertTrue(shell.hasSuffix("'v0.1.1'"))
    }
    @MainActor func testSandboxTerminalHandoff() async throws {
        guard ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "terminal-handoff" else { throw XCTSkip("Opt-in Terminal handoff probe") }
        let command = try XCTUnwrap(Bundle(for: VersionTests.self).url(forResource: "TerminalHandoff", withExtension: "command"))
        _ = try await NSWorkspace.shared.open([command], withApplicationAt: URL(filePath: "/System/Applications/Utilities/Terminal.app"), configuration: .init())
        print("TERMINAL_LAUNCH_ACCEPTED")
    }
    func testVersionOrdering() {
        XCTAssertFalse(hasNewerVersion("0.1.0", than: "0.1.0-dev+abc"))
        XCTAssertFalse(hasNewerVersion("0.1.3", than: "0.1.3-dev+abc"))
        XCTAssertTrue(hasNewerVersion("0.1.4", than: "0.1.3-dev+abc"))
        XCTAssertFalse(hasNewerVersion("0.1.3", than: "0.1.3+abc"))
        XCTAssertTrue(hasNewerVersion("v0.10.0", than: "0.9.9"))
        XCTAssertFalse(hasNewerVersion("0.1.0", than: "0.1.0"))
        XCTAssertFalse(hasNewerVersion("0.1.0", than: "0.2.0-dev+abc"))
        XCTAssertFalse(hasNewerVersion("0.1.1", than: nil))
    }
    @MainActor func testRestartFailuresArePolledWithoutResubmittingUpdate() async throws {
        let fixture = UpdateFixture(); UpdateProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [UpdateProtocol.self]
        let store = VersionStore()
        let release = ReleaseInfo(latest: "0.1.1", url: URL(string: "https://github.com/dreaifekks/tts-studio/releases/tag/v0.1.1")!, published_at: "2026-10-24T00:00:00Z", notes: "更新")
        await store.updateServer(baseURL: "http://fixture", release: release, session: URLSession(configuration: config))
        XCTAssertEqual(store.serverVersion, "0.1.1")
        XCTAssertEqual(store.serverProgress, "服务端已更新到 0.1.1")
        XCTAssertNil(store.error)
        XCTAssertFalse(store.serverUpdating)
        XCTAssertEqual(fixture.requests.filter { $0.url!.path == "/system/update" }.count, 1)
        XCTAssertEqual(fixture.healthCount, 3)
        let post = try XCTUnwrap(fixture.requests.first)
        XCTAssertEqual(post.httpMethod, "POST")
        // URLProtocol may receive the body as a stream; the request itself must contain exactly the agreed tag key.
    }
    @MainActor func testAlreadyCurrent200DoesNotWaitForRestart() async {
        let fixture = UpdateFixture(); fixture.upToDate = true; UpdateProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [UpdateProtocol.self]
        let store = VersionStore()
        let release = ReleaseInfo(latest: "v0.1.1", url: URL(string: "https://github.com/dreaifekks/tts-studio/releases/tag/v0.1.1")!, published_at: "2026-10-24T00:00:00Z", notes: "更新")
        await store.updateServer(baseURL: "http://fixture", release: release, session: URLSession(configuration: config))
        XCTAssertEqual(store.serverProgress, "已是该版本：0.1.1")
        XCTAssertEqual(store.serverVersion, "0.1.1")
        XCTAssertEqual(fixture.requests.count, 1)
        XCTAssertEqual(fixture.healthCount, 0)
        XCTAssertFalse(store.serverUpdating)
        XCTAssertNil(store.error)
    }
    @MainActor func testRejectedUpdateShowsDetailAndDoesNotPoll() async {
        let fixture = UpdateFixture(); fixture.rejected = true; UpdateProtocol.fixture = fixture
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [UpdateProtocol.self]
        let store = VersionStore()
        let release = ReleaseInfo(latest: "v0.1.1", url: URL(string: "https://github.com/dreaifekks/tts-studio/releases/tag/v0.1.1")!, published_at: "2026-10-24T00:00:00Z", notes: "更新")
        await store.updateServer(baseURL: "http://fixture", release: release, session: URLSession(configuration: config))
        XCTAssertEqual(store.error, "有任务正在运行")
        XCTAssertEqual(fixture.requests.count, 1)
        XCTAssertFalse(store.serverUpdating)
    }
}

extension VersionTests {
    func testUpdaterRunsInstallerAtCurrentAndLegacyTagPaths() throws {
        let script = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().appending(path: "script/update.sh")
        for layout in ["fresh", "mac/script", "script", "missing"] {
            let root = FileManager.default.temporaryDirectory.appending(path: "tts-update-fixture-" + UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: root) }
            let source = root.appending(path: "Library/Application Support/TTSStudio/release-src")
            if layout != "fresh" {
                try FileManager.default.createDirectory(at: source.appending(path: ".git"), withIntermediateDirectories: true)
            }
            if layout != "missing" {
                let folder = source.appending(path: layout == "fresh" ? "mac/script" : layout)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                try "#!/bin/bash\nprintf '%s' \"$TTS_DERIVED_DATA\" > \"$HOME/installed\"\n".write(to: folder.appending(path: "install.sh"), atomically: true, encoding: .utf8)
            }
            // Prevent all real Git/network operations; execute the actual updater with a local tag-layout fixture.
            let shellEnvironment = root.appending(path: "bash-env")
            let gitFixture = """
            git() {
                case "$1" in
                    clone)
                        [[ "$2" == https://github.com/dreaifekks/tts-studio.git ]] || return 91
                        mkdir "$3/.git"
                        ;;
                    fetch)
                        [[ "$2" == https://github.com/dreaifekks/tts-studio.git ]] || return 92
                        ;;
                    *) return 0 ;;
                esac
            }
            gh() { return 93; }
            """
            try gitFixture.write(to: shellEnvironment, atomically: true, encoding: .utf8)
            let process = Process(); process.executableURL = URL(filePath: "/bin/bash"); process.arguments = [script.path, "v0.1.3"]
            var environment = ProcessInfo.processInfo.environment
            environment["HOME"] = root.path; environment["BASH_ENV"] = shellEnvironment.path
            process.environment = environment
            let output = Pipe(); process.standardOutput = output; process.standardError = output
            try process.run(); process.waitUntilExit()
            let log = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            XCTAssertEqual(process.terminationStatus, layout == "missing" ? 1 : 0, log)
            if layout != "missing" {
                XCTAssertEqual(try String(contentsOf: root.appending(path: "installed"), encoding: .utf8), root.appending(path: "Library/Caches/TTSStudio/Release.noindex").path)
            } else { XCTAssertTrue(log.contains("这个版本没有安装脚本")) }
            XCTAssertFalse(FileManager.default.fileExists(atPath: root.appending(path: "Library/Caches/TTSStudio/update.lock").path))
        }
    }
}

extension VersionTests {
    @MainActor func testTerminalCommandConsumesWrittenTagAndIgnoresStaleSandboxTag() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "tts-terminal-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let cache = root.appending(path: "Library/Caches/TTSStudio")
        let bundledCommand = try TerminalUpdater.prepare(tag: "v0.1.4", directory: cache)
        let stale = root.appending(path: "Library/Containers/com.dreaife.tts-studio/Data/Library/Caches/TTSStudio/update-tag")
        try FileManager.default.createDirectory(at: stale.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "v0.1.3".write(to: stale, atomically: true, encoding: .utf8)
        let runner = root.appending(path: "update.command")
        try FileManager.default.copyItem(at: bundledCommand, to: runner)
        try "#!/bin/bash\nprintf '%s' \"$1\" > \"$HOME/received-tag\"\nexit 23\n".write(to: root.appending(path: "update.sh"), atomically: true, encoding: .utf8)
        let process = Process(); process.executableURL = URL(filePath: "/bin/bash"); process.arguments = [runner.path]
        var environment = ProcessInfo.processInfo.environment; environment["HOME"] = root.path; process.environment = environment
        let output = Pipe(); process.standardOutput = output; process.standardError = output
        try process.run(); process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 23) // inert installer exits before any read/open of the installed app
        XCTAssertEqual(try String(contentsOf: root.appending(path: "received-tag"), encoding: .utf8), "v0.1.4")
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.appending(path: "update-tag").path))
        XCTAssertEqual(try String(contentsOf: stale, encoding: .utf8), "v0.1.3")
    }
}
