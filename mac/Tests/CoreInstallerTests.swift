import XCTest
import Darwin
@testable import TTSStudio

private final class MockInstallRunner: CoreInstallRunning, @unchecked Sendable {
    let chunks: [Data]
    let exit: CoreInstallExit
    private let lock = NSLock()
    private(set) var receivedExecutable: String?
    private(set) var receivedArguments: [String] = []
    private(set) var cancelCalled = false

    init(chunks: [Data], exit: CoreInstallExit) {
        self.chunks = chunks
        self.exit = exit
    }

    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        onChunk: @escaping @Sendable (Data) -> Void
    ) async -> CoreInstallExit {
        _ = environment
        lock.withLock {
            receivedExecutable = executable
            receivedArguments = arguments
        }
        for chunk in chunks {
            onChunk(chunk)
        }
        return exit
    }

    func cancel() {
        lock.withLock { cancelCalled = true }
    }
}

private final class GatedSwitchingRunner: CoreInstallRunning, @unchecked Sendable {
    private let lock = NSLock()
    private let releaseGate = DispatchSemaphore(value: 0)
    private(set) var cancelCalled = false

    func release() {
        releaseGate.signal()
    }

    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        onChunk: @escaping @Sendable (Data) -> Void
    ) async -> CoreInstallExit {
        _ = (executable, arguments, environment)
        onChunk(Data("@@PROGRESS 6 6 0.92 启动本机主控服务\n@@SWITCHING\n".utf8))
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            DispatchQueue.global(qos: .userInitiated).async {
                self.releaseGate.wait()
                cont.resume()
            }
        }
        onChunk(Data("@@DONE 0.9.4 9880\n".utf8))
        return CoreInstallExit(exitCode: 0, wasCancelled: false)
    }

    func cancel() {
        lock.withLock { cancelCalled = true }
    }
}

final class CoreInstallerTests: XCTestCase {
    @MainActor
    func testChunkedProgressAndDoneParsingAcrossUTF8Boundaries() async {
        let fullOutput = Data(
            (
                "@@PROGRESS 1 6 0.05 检查本机环境\n" +
                "[00:00:01] ok\n" +
                "@@PROGRESS 2 6 0.15 解包主控代码\r\n" +
                "@@DONE 0.9.3 9980\n"
            ).utf8
        )
        // Slice into tiny 3-byte chunks so multi-byte Chinese UTF-8 characters and
        // protocol lines are split across chunk boundaries.
        var chunks: [Data] = []
        var offset = 0
        while offset < fullOutput.count {
            let end = min(offset + 3, fullOutput.count)
            chunks.append(fullOutput[offset..<end])
            offset = end
        }

        let runner = MockInstallRunner(chunks: chunks, exit: CoreInstallExit(exitCode: 0, wasCancelled: false))
        let scriptURL = URL(filePath: "/tmp/fake-install.sh")
        let installer = CoreInstaller(
            scriptURLProvider: { scriptURL },
            plistURLProvider: { URL(filePath: "/tmp/nonexistent-\(UUID().uuidString).plist") },
            runnerFactory: { runner }
        )

        var callbackResult: (String, Int)?
        installer.onSucceeded = { version, port in
            callbackResult = (version, port)
        }

        installer.start(port: 9980, engines: ["v2pro", "v2proplus"])
        await installer.waitForCompletion()

        XCTAssertEqual(installer.state, .succeeded(version: "0.9.3", port: 9980))
        XCTAssertEqual(callbackResult?.0, "0.9.3")
        XCTAssertEqual(callbackResult?.1, 9980)
        XCTAssertEqual(runner.receivedExecutable, "/bin/bash")
        XCTAssertEqual(runner.receivedArguments, ["/tmp/fake-install.sh", "--port", "9980", "--engines", "v2pro,v2proplus"])
        XCTAssertTrue(installer.logs.contains("[1/6] 检查本机环境"))
        XCTAssertTrue(installer.logs.contains("[2/6] 解包主控代码"))
        XCTAssertTrue(installer.logs.contains("[00:00:01] ok"))
    }

    @MainActor
    func testSwitchingEventDisablesCancellationUntilCompletion() async throws {
        let runner = GatedSwitchingRunner()
        let installer = CoreInstaller(
            scriptURLProvider: { URL(filePath: "/tmp/fake-install.sh") },
            plistURLProvider: { URL(filePath: "/tmp/nonexistent-\(UUID().uuidString).plist") },
            runnerFactory: { runner }
        )

        installer.start(port: 9880, engines: ["v2pro"])

        var enteredSwitching = false
        for _ in 0..<100 {
            if case .running(_, _, _, let stage, _, let canCancel) = installer.state,
               !canCancel,
               stage == CoreInstallCopy.switchingStage {
                enteredSwitching = true
                break
            }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(enteredSwitching, "Expected @@SWITCHING to disable cancellation")
        XCTAssertFalse(installer.canCancel)

        // Calling cancel during @@SWITCHING must be ignored so rollback/commit cannot be interrupted.
        installer.cancel()
        XCTAssertFalse(runner.cancelCalled)

        runner.release()
        await installer.waitForCompletion()
        XCTAssertEqual(installer.state, .succeeded(version: "0.9.4", port: 9880))
    }

    @MainActor
    func testFailedExitProducesFriendlyMessageAndKeepsRawLogs() async {
        let runner = MockInstallRunner(
            chunks: [Data("Missing ffmpeg. Run 'brew install ffmpeg' in Terminal.\n".utf8)],
            exit: CoreInstallExit(exitCode: 1, wasCancelled: false)
        )
        let installer = CoreInstaller(
            scriptURLProvider: { URL(filePath: "/tmp/fake-install.sh") },
            plistURLProvider: { URL(filePath: "/tmp/nonexistent-\(UUID().uuidString).plist") },
            runnerFactory: { runner }
        )

        installer.start(port: 9880, engines: ["v2pro"])
        await installer.waitForCompletion()

        XCTAssertEqual(installer.state, .failed(CoreInstallCopy.missingFFmpegError))
        XCTAssertTrue(installer.logs.contains { $0.contains("brew install ffmpeg") })
        XCTAssertTrue(installer.diagnosticsText().contains("brew install ffmpeg"))

        // Verify other error categories map cleanly without exposing forbidden terms.
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["Run 'xcode-select --install'"], exitCode: 1),
            CoreInstallCopy.missingXcodeToolsError
        )
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["Port 9880 is already in use"], exitCode: 1),
            CoreInstallCopy.addressInUseError
        )
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["Control service failed health check"], exitCode: 1),
            CoreInstallCopy.healthCheckError
        )
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["已有另一安装或卸载正在进行 (PID 123)"], exitCode: 1),
            CoreInstallCopy.lockBusyError
        )
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["Missing patches/gptsovits-bf81cdb.patch"], exitCode: 1),
            CoreInstallCopy.missingEnginePatchError
        )
        XCTAssertEqual(
            CoreInstaller.friendlyErrorMessage(from: ["unknown error"], exitCode: 1),
            CoreInstallCopy.genericFailureError
        )
    }

    @MainActor
    func testCancelTerminatesRealChildProcessGroup() async throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "tts-cancel-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let childPidFile = dir.appending(path: "child.pid")
        let scriptURL = dir.appending(path: "slow_install.sh")
        let script = """
        #!/usr/bin/env bash
        echo "@@PROGRESS 1 6 0.05 检查本机环境"
        /bin/sleep 60 &
        echo $! > "\(childPidFile.path)"
        wait
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)

        let installer = CoreInstaller(
            scriptURLProvider: { scriptURL },
            plistURLProvider: { dir.appending(path: "missing.plist") },
            runnerFactory: { PosixSpawnProcessRunner(killGraceInterval: 1.0, pipeDrainTimeout: 0.2) }
        )

        installer.start(port: 9880, engines: ["v2pro"])

        var spawnedChildPid: pid_t?
        for _ in 0..<100 {
            if let text = try? String(contentsOf: childPidFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
               let pid = pid_t(text) {
                spawnedChildPid = pid
                break
            }
            try await Task.sleep(for: .milliseconds(30))
        }
        let childPid = try XCTUnwrap(spawnedChildPid, "Expected child process PID to be written")

        installer.cancel()
        await installer.waitForCompletion()

        XCTAssertEqual(installer.state, .cancelled)

        // Allow brief kernel reap time and confirm grandchild process was terminated too.
        var alive = true
        for _ in 0..<40 {
            if Darwin.kill(childPid, 0) != 0 && errno == ESRCH {
                alive = false
                break
            }
            try await Task.sleep(for: .milliseconds(25))
        }
        if alive {
            _ = Darwin.kill(childPid, SIGKILL)
        }
        XCTAssertFalse(alive, "Cancelling installer must terminate grandchild processes in its process group")
    }

    @MainActor
    func testProcessCompletesEvenWhenDescendantHoldsPipeWriteEndOpen() async throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "tts-eof-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let sleeperPidFile = dir.appending(path: "sleeper.pid")
        let scriptURL = dir.appending(path: "leak_fd.sh")
        // Spawn a background child that inherits stdout/stderr and lives for 30s, while parent exits immediately.
        let script = """
        #!/usr/bin/env bash
        echo "@@PROGRESS 6 6 0.92 启动本机主控服务"
        /bin/sleep 30 &
        echo $! > "\(sleeperPidFile.path)"
        echo "@@DONE 0.9.9 9880"
        exit 0
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)

        let installer = CoreInstaller(
            scriptURLProvider: { scriptURL },
            plistURLProvider: { dir.appending(path: "missing.plist") },
            runnerFactory: { PosixSpawnProcessRunner(killGraceInterval: 15.0, pipeDrainTimeout: 0.3) }
        )

        let start = Date()
        installer.start(port: 9880, engines: ["v2pro"])
        await installer.waitForCompletion()
        let elapsed = Date().timeIntervalSince(start)

        // Clean up the background sleeper process spawned by the test script.
        if let text = try? String(contentsOf: sleeperPidFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           let pid = pid_t(text) {
            _ = Darwin.kill(pid, SIGKILL)
        }

        XCTAssertEqual(installer.state, .succeeded(version: "0.9.9", port: 9880))
        XCTAssertLessThan(elapsed, 3.0, "Runner must finish after child exits without waiting for descendant pipe EOF")
    }

    func testPosixSpawnUnmasksSIGTERMForGracefulChildExit() async throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "tts-sigterm-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let readyFlag = dir.appending(path: "ready.flag")
        let scriptURL = dir.appending(path: "trap_term.sh")
        let script = """
        #!/usr/bin/env bash
        trap 'echo CAUGHT_TERM; exit 42' TERM
        touch "\(readyFlag.path)"
        while :; do
          /bin/sleep 0.1
        done
        """
        try script.write(to: scriptURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scriptURL.path)

        final class LockedOutput: @unchecked Sendable {
            private let lock = NSLock()
            private var data = Data()
            func append(_ chunk: Data) { lock.withLock { data.append(chunk) } }
            func text() -> String { lock.withLock { String(decoding: data, as: UTF8.self) } }
        }

        let runner = PosixSpawnProcessRunner(killGraceInterval: 10.0, pipeDrainTimeout: 0.5)
        let collected = LockedOutput()

        let task = Task {
            await runner.run(
                executable: "/bin/bash",
                arguments: [scriptURL.path],
                environment: ProcessInfo.processInfo.environment
            ) { chunk in
                collected.append(chunk)
            }
        }

        for _ in 0..<100 {
            if FileManager.default.fileExists(atPath: readyFlag.path) { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(FileManager.default.fileExists(atPath: readyFlag.path), "Child script never became ready")

        runner.cancel()
        let result = await task.value
        let output = collected.text()

        XCTAssertTrue(result.wasCancelled)
        XCTAssertTrue(output.contains("CAUGHT_TERM"), "Child must receive SIGTERM cleanly; output=\(output)")
        XCTAssertEqual(result.exitCode, 42, "Child should exit via TERM trap (42), not SIGKILL (137)")
    }

    func testFailureStateForcesDetailsCollapsed() {
        XCTAssertTrue(CoreInstallView.shouldCollapseDetails(for: .failed(CoreInstallCopy.genericFailureError)))
        XCTAssertFalse(
            CoreInstallView.shouldCollapseDetails(
                for: .running(step: 2, total: 6, progress: 0.2, stage: "解包主控代码", startedAt: Date(), canCancel: true)
            )
        )
        XCTAssertFalse(CoreInstallView.shouldCollapseDetails(for: .idle))
        XCTAssertFalse(CoreInstallView.shouldCollapseDetails(for: .succeeded(version: "0.9.0", port: 9880)))
    }

    @MainActor
    func testShellCommandEscapingAndMockPlistPortDetection() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "tts-plist-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let plistURL = dir.appending(path: "com.dreaife.tts-core.plist")
        let plistDict: [String: Any] = [
            "Label": "com.dreaife.tts-core",
            "EnvironmentVariables": [
                "TTSAPI_PORT": "9980",
                "TTSAPI_ROLE": "control",
            ],
        ]
        let plistData = try PropertyListSerialization.data(fromPropertyList: plistDict, format: .xml, options: 0)
        try plistData.write(to: plistURL)

        let weirdScript = URL(filePath: "/tmp/User's App/TTS Studio/install_control_macos.sh")
        let installer = CoreInstaller(
            scriptURLProvider: { weirdScript },
            plistURLProvider: { plistURL }
        )

        XCTAssertEqual(installer.detectInstalledPort(), 9980)

        let autoPortCmd = installer.shellCommand(port: nil, engines: ["v2pro", "v2proplus", "v4"])
        XCTAssertEqual(
            autoPortCmd,
            "/bin/bash '/tmp/User'\"'\"'s App/TTS Studio/install_control_macos.sh' --port '9980' --engines 'v2pro,v2proplus,v4'"
        )

        let explicitPortCmd = installer.shellCommand(port: 9880, engines: ["v2pro"])
        XCTAssertEqual(
            explicitPortCmd,
            "/bin/bash '/tmp/User'\"'\"'s App/TTS Studio/install_control_macos.sh' --port '9880' --engines 'v2pro'"
        )

        // Missing plist returns nil.
        let missingInstaller = CoreInstaller(
            scriptURLProvider: { weirdScript },
            plistURLProvider: { dir.appending(path: "does-not-exist.plist") }
        )
        XCTAssertNil(missingInstaller.detectInstalledPort())
    }

    func testEmptyCoreDirectoryDoesNotCountAsInstalled() throws {
        let home = FileManager.default.temporaryDirectory.appending(path: "tts-empty-core-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }

        let emptyCore = home.appending(path: "Library/Application Support/TTSStudio/core")
        try FileManager.default.createDirectory(at: emptyCore, withIntermediateDirectories: true)
        XCTAssertFalse(StudioConnection.defaultLocalCoreInstalled(home: home))

        let validCore = emptyCore.appending(path: "current/core/ttsapi")
        try FileManager.default.createDirectory(at: validCore, withIntermediateDirectories: true)
        XCTAssertTrue(StudioConnection.defaultLocalCoreInstalled(home: home))
    }

    func testMainUICopyAndFailureDefaultSummaryExcludeForbiddenTerms() {
        for copy in CoreInstallCopy.allStaticCopy {
            let lowered = copy.lowercased()
            for term in CoreInstallCopy.forbiddenTerms {
                XCTAssertFalse(
                    lowered.contains(term),
                    "Main UI copy must not contain forbidden term '\(term)': \(copy)"
                )
            }
        }
        // Explicitly assert default failure summary copy (when details disclosure is collapsed).
        for failureCopy in CoreInstallCopy.allFailureDefaultCopy {
            let lowered = failureCopy.lowercased()
            for term in CoreInstallCopy.forbiddenTerms {
                XCTAssertFalse(
                    lowered.contains(term),
                    "Default failure view copy must not contain forbidden term '\(term)': \(failureCopy)"
                )
            }
        }
    }

    func testCoreVersionMismatchDetection() {
        XCTAssertFalse(VersionStore.isCoreVersionMismatched(serverVersion: nil, appVersion: "0.1.3"))
        XCTAssertFalse(VersionStore.isCoreVersionMismatched(serverVersion: "", appVersion: "0.1.3"))
        XCTAssertFalse(VersionStore.isCoreVersionMismatched(serverVersion: "0.1.3", appVersion: "0.1.3"))
        XCTAssertFalse(VersionStore.isCoreVersionMismatched(serverVersion: "v0.1.3", appVersion: "0.1.3"))
        XCTAssertFalse(VersionStore.isCoreVersionMismatched(serverVersion: "0.1.3-dev+abc1234", appVersion: "0.1.3-dev+abc1234"))
        XCTAssertTrue(VersionStore.isCoreVersionMismatched(serverVersion: "0.1.2", appVersion: "0.1.3"))
        XCTAssertTrue(VersionStore.isCoreVersionMismatched(serverVersion: "0.1.3-dev+abc1234", appVersion: "0.1.3-dev+def5678"))
    }

    @MainActor
    func testStudioStoreHandleCoreInstallSucceededUpdatesConnectionAndSamePortBumpsGeneration() async throws {
        let suite = "tts-installer-store-\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        var installed = false
        let connection = StudioConnection(defaults: defaults, localCoreInstalled: { installed })
        connection.role = "client"
        connection.address = "http://remote.invalid:9880"
        let studio = StudioStore(defaults: defaults, connection: connection)

        installed = true
        let initialGeneration = studio.api.generation
        await studio.handleCoreInstallSucceeded(version: "0.9.5", port: 9985)

        XCTAssertEqual(studio.connection.role, "control")
        XCTAssertEqual(studio.connection.localPort, 9985)
        XCTAssertTrue(studio.connection.configured)
        XCTAssertFalse(studio.connection.localCoreMissing)
        XCTAssertEqual(studio.api.baseURL, "http://127.0.0.1:9985")
        XCTAssertEqual(studio.versions.serverVersion, "0.9.5")
        XCTAssertGreaterThan(studio.api.generation, initialGeneration)

        // Same-port update must STILL increment api.generation and cancel stale SSE monitors.
        let stubJob = Job(
            id: "job-stale",
            kind: "generate",
            target: "c1",
            status: "running",
            progress: 0.1,
            stage: "运行中",
            created: "2026-11-22T00:00:00Z"
        )
        studio.monitor(stubJob)
        XCTAssertEqual(studio.activeMonitorCount, 1)

        let generationBeforeSamePort = studio.api.generation
        await studio.handleCoreInstallSucceeded(version: "0.9.6", port: 9985)
        XCTAssertEqual(studio.api.baseURL, "http://127.0.0.1:9985")
        XCTAssertEqual(studio.versions.serverVersion, "0.9.6")
        XCTAssertEqual(studio.api.generation, generationBeforeSamePort + 1)
        XCTAssertEqual(studio.activeMonitorCount, 0)
    }

    func testBundledInstallerResourcesArePresentInHostApp() throws {
        let installScript = try XCTUnwrap(Bundle.main.url(forResource: "install_control_macos", withExtension: "sh"))
        let uninstallScript = try XCTUnwrap(Bundle.main.url(forResource: "uninstall_control_macos", withExtension: "sh"))
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: installScript.path))
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: uninstallScript.path))

        let bundleDir = installScript.deletingLastPathComponent().appending(path: "core-bundle")
        XCTAssertTrue(FileManager.default.fileExists(atPath: bundleDir.appending(path: "core-head.bundle").path))
        let metadata = try String(contentsOf: bundleDir.appending(path: "bundle.env"), encoding: .utf8)
        XCTAssertTrue(metadata.contains("\nGSV_BRANCH=tts-studio\n"))
        XCTAssertTrue(metadata.contains("\nGSV_PATCH_REL=patches/gptsovits-bf81cdb.patch\n"))
        XCTAssertNotNil(metadata.range(of: #"(?m)^GSV_PATCH_SHA256=[a-f0-9]{64}$"#, options: .regularExpression))
    }
}
