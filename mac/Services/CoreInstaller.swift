import Foundation
import Observation
import Darwin

enum CoreInstallMode: Equatable, Sendable {
    case install
    case update
}

enum CoreInstallState: Equatable, Sendable {
    case idle
    case running(step: Int, total: Int, progress: Double, stage: String, startedAt: Date, canCancel: Bool = true)
    case succeeded(version: String, port: Int)
    case failed(String)
    case cancelled
}

enum CoreInstallEvent: Equatable, Sendable {
    case progress(step: Int, total: Int, progress: Double, stage: String)
    case switching
    case done(version: String, port: Int)
    case log(String)
}

struct CoreInstallExit: Sendable, Equatable {
    let exitCode: Int32
    let wasCancelled: Bool
}

protocol CoreInstallRunning: Sendable {
    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        onChunk: @escaping @Sendable (Data) -> Void
    ) async -> CoreInstallExit
    func cancel()
}

struct CoreInstallLineBuffer: Sendable {
    private var buffer = Data()

    mutating func append(_ chunk: Data) -> [String] {
        guard !chunk.isEmpty else { return [] }
        buffer.append(chunk)
        var lines: [String] = []
        let newline = UInt8(ascii: "\n")
        let carriageReturn = UInt8(ascii: "\r")
        while let idx = buffer.firstIndex(of: newline) {
            var slice = buffer[..<idx]
            if slice.last == carriageReturn {
                slice = slice.dropLast()
            }
            lines.append(String(decoding: slice, as: UTF8.self))
            buffer.removeSubrange(...idx)
        }
        return lines
    }

    mutating func flush() -> [String] {
        guard !buffer.isEmpty else { return [] }
        let carriageReturn = UInt8(ascii: "\r")
        var slice = buffer[...]
        if slice.last == carriageReturn {
            slice = slice.dropLast()
        }
        let line = String(decoding: slice, as: UTF8.self)
        buffer.removeAll(keepingCapacity: false)
        return [line]
    }
}

enum CoreInstallCopy {
    static let forbiddenTerms = ["venv", "uv", "sha256", "launchd", "plist", "git", "bundle", "端口", "core"]

    static let installTitle = "安装本机主控服务"
    static let updateTitle = "更新 / 修复本机主控服务"
    static let installDescription = "安装后这台 Mac 即可作为主控管理资料库、安排所有任务，并可直接在本机生成语音。首次安装需要下载约 5–6 GB 基础模型，大约耗时 10–15 分钟。"
    static let updateDescription = "将本机主控服务同步到当前应用版本，或补充所选语音引擎的基础模型。已有的资料库、角色、历史记录与个人设置都会完整保留。"
    static let engineSectionTitle = "选择要准备的语音引擎"
    static let engineSelectionHint = "默认包含常用引擎，之后也可以随时回来补充其他引擎。"
    static let startInstallButton = "开始安装"
    static let startUpdateButton = "开始更新"
    static let copyCommandButton = "复制安装命令"
    static let copiedCommandButton = "已复制"
    static let copyDiagnosticsButton = "复制诊断信息"
    static let copiedDiagnosticsButton = "已复制诊断信息"
    static let detailsLabel = "查看详情"
    static let backgroundHint = "关闭此窗口不会中断当前进度，可随时从设置重新打开查看。"
    static let switchingStage = "正在切换服务，请稍候"
    static let switchingHint = "正在切换服务，请稍候完成。"
    static let runningInstallTitle = "正在安装本机主控服务"
    static let runningUpdateTitle = "正在更新本机主控服务"
    static let initialStage = "准备开始…"
    static let succeededTitle = "本机主控服务已就绪"
    static let failedTitle = "未能完成安装"
    static let cancelledNotice = "上次安装已取消，你可以随时重新开始。"
    static let oneClickInstallButton = "一键安装主控服务…"
    static let viewProgressButton = "查看安装进度…"
    static let installServiceButton = "安装主控服务…"
    static let reinstallOrModelsButton = "重装 / 添加模型…"
    static let missingScriptError = "当前应用缺少主控安装组件，请重新安装最新版 TTS Studio。"
    static let missingXcodeToolsError = "需要先安装系统的命令行开发工具，请展开「查看详情」查看操作说明。"
    static let missingFFmpegError = "需要先安装音频处理组件（FFmpeg），请展开「查看详情」查看操作说明。"
    static let unsupportedArchError = "当前仅支持 Apple Silicon 芯片的 Mac。"
    static let addressInUseError = "本地服务地址已被其他程序占用，请展开「查看详情」查看原因或改用安装命令。"
    static let healthCheckError = "主控服务启动后未能正常响应，请展开「查看详情」查看日志后重试。"
    static let lockBusyError = "另一项安装或卸载正在进行，请等待完成后再重试。"
    static let missingEnginePatchError = "当前安装包的 GPT-SoVITS 补丁缺失或无法校验，请重新获取完整安装包后重试。"
    static let genericFailureError = "安装未能完成，请展开「查看详情」查看日志，或复制安装命令在终端重试。"

    static func syncUpdateButton(appVersion: String) -> String {
        "同步更新主控服务到 \(appVersion)…"
    }

    static func succeededSubtitle(version: String) -> String {
        "当前版本 \(version)，已自动切换为本机主控并开始连接。"
    }

    static var allFailureDefaultCopy: [String] {
        [
            failedTitle,
            detailsLabel,
            copyCommandButton,
            copiedCommandButton,
            copyDiagnosticsButton,
            copiedDiagnosticsButton,
            missingScriptError,
            missingXcodeToolsError,
            missingFFmpegError,
            unsupportedArchError,
            addressInUseError,
            healthCheckError,
            lockBusyError,
            missingEnginePatchError,
            genericFailureError,
        ]
    }

    static var allStaticCopy: [String] {
        [
            installTitle,
            updateTitle,
            installDescription,
            updateDescription,
            engineSectionTitle,
            engineSelectionHint,
            startInstallButton,
            startUpdateButton,
            backgroundHint,
            switchingStage,
            switchingHint,
            runningInstallTitle,
            runningUpdateTitle,
            initialStage,
            succeededTitle,
            cancelledNotice,
            oneClickInstallButton,
            viewProgressButton,
            installServiceButton,
            reinstallOrModelsButton,
            syncUpdateButton(appVersion: "1.0.0"),
            succeededSubtitle(version: "1.0.0"),
        ] + allFailureDefaultCopy
    }
}

/// Runs a subprocess in its own process group so cancelling terminates the full
/// child process tree cleanly, and reaps the child independently of pipe EOF so
/// descendants that inherit stdout/stderr cannot hang completion.
final class PosixSpawnProcessRunner: CoreInstallRunning, @unchecked Sendable {
    private let lock = NSLock()
    private let killGraceInterval: TimeInterval
    private let pipeDrainTimeout: TimeInterval
    private var childPid: pid_t = 0
    private var cancelled = false
    private var finished = false
    private var stopReading = false
    private var terminateDispatched = false

    init(killGraceInterval: TimeInterval = 15.0, pipeDrainTimeout: TimeInterval = 0.5) {
        self.killGraceInterval = killGraceInterval
        self.pipeDrainTimeout = pipeDrainTimeout
    }

    func run(
        executable: String,
        arguments: [String],
        environment: [String: String],
        onChunk: @escaping @Sendable (Data) -> Void
    ) async -> CoreInstallExit {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let exitResult = self.executeSync(
                    executable: executable,
                    arguments: arguments,
                    environment: environment,
                    onChunk: onChunk
                )
                continuation.resume(returning: exitResult)
            }
        }
    }

    func cancel() {
        let pidToSignal: pid_t? = lock.withLock {
            cancelled = true
            guard childPid > 0, !finished, !terminateDispatched else { return nil }
            terminateDispatched = true
            return childPid
        }
        if let pid = pidToSignal {
            sendTerminate(pid: pid)
        }
    }

    private func sendTerminate(pid: pid_t) {
        _ = Darwin.kill(-pid, SIGTERM)
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + killGraceInterval) { [weak self] in
            guard let self else { return }
            let stillRunning: Bool = self.lock.withLock { !self.finished && self.childPid == pid }
            if stillRunning {
                _ = Darwin.kill(-pid, SIGKILL)
            }
        }
    }

    private func executeSync(
        executable: String,
        arguments: [String],
        environment: [String: String],
        onChunk: @escaping @Sendable (Data) -> Void
    ) -> CoreInstallExit {
        let alreadyCancelled: Bool = lock.withLock { cancelled }
        if alreadyCancelled {
            return CoreInstallExit(exitCode: -1, wasCancelled: true)
        }

        var pipeFDs: [Int32] = [-1, -1]
        guard Darwin.pipe(&pipeFDs) == 0 else {
            return CoreInstallExit(exitCode: -1, wasCancelled: false)
        }
        let readFD = pipeFDs[0]
        let writeFD = pipeFDs[1]
        _ = Darwin.fcntl(readFD, F_SETFD, FD_CLOEXEC)

        var fileActions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fileActions)
        defer { posix_spawn_file_actions_destroy(&fileActions) }
        posix_spawn_file_actions_addopen(&fileActions, STDIN_FILENO, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&fileActions, writeFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&fileActions, writeFD, STDERR_FILENO)
        posix_spawn_file_actions_addclose(&fileActions, readFD)
        posix_spawn_file_actions_addclose(&fileActions, writeFD)

        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }

        // Clear any inherited thread signal mask (e.g. SIGTERM masked on libdispatch queues)
        // and restore default signal dispositions so the child installer receives SIGTERM cleanly.
        var emptyMask = sigset_t()
        sigemptyset(&emptyMask)
        posix_spawnattr_setsigmask(&attr, &emptyMask)

        var defaultSignals = sigset_t()
        sigemptyset(&defaultSignals)
        sigaddset(&defaultSignals, SIGTERM)
        sigaddset(&defaultSignals, SIGINT)
        sigaddset(&defaultSignals, SIGHUP)
        posix_spawnattr_setsigdefault(&attr, &defaultSignals)

        let spawnFlags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF)
        posix_spawnattr_setflags(&attr, spawnFlags)
        posix_spawnattr_setpgroup(&attr, 0)

        let argvStrings = [executable] + arguments
        let envStrings = environment.map { "\($0.key)=\($0.value)" }
        var argv: [UnsafeMutablePointer<CChar>?] = argvStrings.map { strdup($0) } + [nil]
        var envp: [UnsafeMutablePointer<CChar>?] = envStrings.map { strdup($0) } + [nil]
        defer {
            for ptr in argv { if let ptr { free(ptr) } }
            for ptr in envp { if let ptr { free(ptr) } }
        }

        var pid: pid_t = 0
        let spawnResult = posix_spawn(&pid, executable, &fileActions, &attr, &argv, &envp)
        Darwin.close(writeFD)

        guard spawnResult == 0 else {
            Darwin.close(readFD)
            let msg = String(cString: strerror(spawnResult))
            onChunk(Data("Failed to launch installer: \(msg)\n".utf8))
            return CoreInstallExit(exitCode: spawnResult, wasCancelled: false)
        }

        let shouldTerminateNow: Bool = lock.withLock {
            childPid = pid
            if cancelled && !terminateDispatched {
                terminateDispatched = true
                return true
            }
            return false
        }
        if shouldTerminateNow {
            sendTerminate(pid: pid)
        }

        // Drain stdout/stderr on a dedicated queue so child reaping does not depend on pipe EOF.
        let readerGroup = DispatchGroup()
        readerGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer {
                Darwin.close(readFD)
                readerGroup.leave()
            }
            var buffer = [UInt8](repeating: 0, count: 4096)
            var pfd = pollfd(fd: readFD, events: Int16(POLLIN), revents: 0)
            while true {
                let shouldStop: Bool = self.lock.withLock { self.stopReading }
                if shouldStop { break }
                pfd.revents = 0
                let ready = Darwin.poll(&pfd, 1, 50)
                if ready > 0 {
                    if (pfd.revents & Int16(POLLIN | POLLHUP)) != 0 {
                        let count = Darwin.read(readFD, &buffer, buffer.count)
                        if count > 0 {
                            onChunk(Data(buffer[0..<count]))
                        } else if count == -1 && errno == EINTR {
                            continue
                        } else {
                            break
                        }
                    } else if (pfd.revents & Int16(POLLERR | POLLNVAL)) != 0 {
                        break
                    }
                } else if ready < 0 && errno != EINTR {
                    break
                }
            }
        }

        var status: Int32 = 0
        while Darwin.waitpid(pid, &status, 0) == -1 {
            if errno != EINTR { break }
        }

        let wasCancelled: Bool = lock.withLock {
            finished = true
            return cancelled
        }

        // Wait briefly for remaining buffered bytes, then close the read end even if a descendant kept writeFD open.
        if readerGroup.wait(timeout: .now() + pipeDrainTimeout) == .timedOut {
            lock.withLock { stopReading = true }
            readerGroup.wait()
        }

        let exitCode: Int32
        if (status & 0x7f) == 0 {
            exitCode = (status >> 8) & 0xff
        } else {
            exitCode = 128 + (status & 0x7f)
        }
        return CoreInstallExit(exitCode: exitCode, wasCancelled: wasCancelled)
    }
}

@MainActor @Observable
final class CoreInstaller {
    static let maxLogLines = 800
    static let availableEngines = ["v2proplus", "v2pro", "v4", "v5turbo", "v5dev"]

    var state: CoreInstallState = .idle
    private(set) var logs: [String] = []
    @ObservationIgnored var onSucceeded: (@MainActor (String, Int) async -> Void)?

    @ObservationIgnored private let scriptURLProvider: () -> URL?
    @ObservationIgnored private let plistURLProvider: () -> URL
    @ObservationIgnored private let runnerFactory: () -> any CoreInstallRunning
    @ObservationIgnored private var activeRunner: (any CoreInstallRunning)?
    @ObservationIgnored private var runTask: Task<Void, Never>?
    @ObservationIgnored private var runToken = 0

    var isRunning: Bool {
        if case .running = state { return true }
        return false
    }

    var canCancel: Bool {
        if case .running(_, _, _, _, _, let allowed) = state { return allowed }
        return false
    }

    init(
        scriptURLProvider: @escaping () -> URL? = {
            Bundle.main.url(forResource: "install_control_macos", withExtension: "sh")
        },
        plistURLProvider: @escaping () -> URL = {
            FileManager.default.homeDirectoryForCurrentUser
                .appending(path: "Library/LaunchAgents/com.dreaife.tts-core.plist")
        },
        runnerFactory: @escaping () -> any CoreInstallRunning = {
            PosixSpawnProcessRunner()
        }
    ) {
        self.scriptURLProvider = scriptURLProvider
        self.plistURLProvider = plistURLProvider
        self.runnerFactory = runnerFactory
    }

    func detectInstalledPort() -> Int? {
        let url = plistURLProvider()
        guard let data = try? Data(contentsOf: url),
              let raw = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              let env = raw["EnvironmentVariables"] as? [String: Any]
        else { return nil }
        if let portString = env["TTSAPI_PORT"] as? String,
           let port = Int(portString.trimmingCharacters(in: .whitespacesAndNewlines)),
           (1...65535).contains(port) {
            return port
        }
        if let portInt = env["TTSAPI_PORT"] as? Int, (1...65535).contains(portInt) {
            return portInt
        }
        return nil
    }

    func shellCommand(port: Int? = nil, engines: [String] = ["v2proplus", "v2pro"]) -> String {
        let scriptPath = scriptURLProvider()?.path ?? "install_control_macos.sh"
        func quote(_ value: String) -> String {
            "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
        }
        var parts = ["/bin/bash", quote(scriptPath)]
        let resolvedPort = port ?? detectInstalledPort()
        if let resolvedPort {
            parts.append("--port")
            parts.append(quote(String(resolvedPort)))
        }
        let cleanedEngines = engines.filter { !$0.isEmpty }
        if !cleanedEngines.isEmpty {
            parts.append("--engines")
            parts.append(quote(cleanedEngines.joined(separator: ",")))
        }
        return parts.joined(separator: " ")
    }

    func diagnosticsText() -> String {
        var sections: [String] = []
        if case .failed(let message) = state {
            sections.append("Summary: \(message)")
        }
        if !logs.isEmpty {
            sections.append(logs.joined(separator: "\n"))
        }
        return sections.joined(separator: "\n\n")
    }

    func start(port: Int? = nil, engines: [String] = ["v2proplus", "v2pro"]) {
        guard !isRunning else { return }
        guard let scriptURL = scriptURLProvider() else {
            state = .failed(CoreInstallCopy.missingScriptError)
            return
        }

        let targetPort = port ?? detectInstalledPort()
        var args = [scriptURL.path]
        if let targetPort {
            args.append(contentsOf: ["--port", String(targetPort)])
        }
        let cleanedEngines = engines.filter { !$0.isEmpty }
        if !cleanedEngines.isEmpty {
            args.append(contentsOf: ["--engines", cleanedEngines.joined(separator: ",")])
        }

        var env = ProcessInfo.processInfo.environment
        let currentPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(currentPath)"

        runToken += 1
        let token = runToken
        let startedAt = Date()
        logs = []
        state = .running(
            step: 1,
            total: 6,
            progress: 0.02,
            stage: CoreInstallCopy.initialStage,
            startedAt: startedAt,
            canCancel: true
        )

        let runner = runnerFactory()
        activeRunner = runner

        runTask = Task { [weak self] in
            guard let self else { return }
            let stateBox = LineStreamState()
            let (lineStream, lineContinuation) = AsyncStream<[String]>.makeStream()

            let consumer = Task { @MainActor [weak self] in
                for await batch in lineStream {
                    guard let self, self.runToken == token else { break }
                    for line in batch {
                        self.handleLine(line, startedAt: startedAt, stateBox: stateBox)
                    }
                }
            }

            let result = await runner.run(
                executable: "/bin/bash",
                arguments: args,
                environment: env
            ) { chunk in
                let lines = stateBox.append(chunk)
                if !lines.isEmpty {
                    lineContinuation.yield(lines)
                }
            }

            let trailing = stateBox.flush()
            if !trailing.isEmpty {
                lineContinuation.yield(trailing)
            }
            lineContinuation.finish()
            await consumer.value

            guard self.runToken == token else { return }
            self.activeRunner = nil

            if result.wasCancelled {
                self.state = .cancelled
                return
            }

            if result.exitCode == 0 {
                let done = stateBox.doneResult
                let installedVersion = done?.version ?? ""
                let installedPort = done?.port ?? targetPort ?? 9880
                self.state = .succeeded(version: installedVersion, port: installedPort)
                await self.onSucceeded?(installedVersion, installedPort)
            } else {
                let summary = Self.friendlyErrorMessage(from: self.logs, exitCode: result.exitCode)
                self.state = .failed(summary)
            }
        }
    }

    func cancel() {
        guard canCancel else { return }
        activeRunner?.cancel()
    }

    func reset() {
        guard !isRunning else { return }
        state = .idle
    }

    func waitForCompletion() async {
        await runTask?.value
    }

    static func parseLine(_ line: String) -> CoreInstallEvent {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed == "@@SWITCHING" {
            return .switching
        }
        if line.hasPrefix("@@PROGRESS ") {
            let body = line.dropFirst("@@PROGRESS ".count)
            let parts = body.split(separator: " ", maxSplits: 3, omittingEmptySubsequences: true)
            if parts.count == 4,
               let step = Int(parts[0]),
               let total = Int(parts[1]),
               let rawProgress = Double(parts[2]) {
                let stage = String(parts[3]).trimmingCharacters(in: .whitespacesAndNewlines)
                if !stage.isEmpty {
                    return .progress(
                        step: step,
                        total: max(total, 1),
                        progress: min(max(rawProgress, 0), 1),
                        stage: stage
                    )
                }
            }
        } else if line.hasPrefix("@@DONE ") {
            let body = line.dropFirst("@@DONE ".count)
            let parts = body.split(separator: " ", omittingEmptySubsequences: true)
            if parts.count >= 2, let port = Int(parts[1]) {
                return .done(version: String(parts[0]), port: port)
            }
        }
        return .log(line)
    }

    static func friendlyErrorMessage(from logs: [String], exitCode: Int32) -> String {
        let joined = logs.joined(separator: "\n").lowercased()
        if joined.contains("已有另一安装或卸载正在进行") {
            return CoreInstallCopy.lockBusyError
        }
        if joined.contains("xcode-select --install") {
            return CoreInstallCopy.missingXcodeToolsError
        }
        if joined.contains("brew install ffmpeg") {
            return CoreInstallCopy.missingFFmpegError
        }
        if joined.contains("apple silicon") || joined.contains("arm64") {
            return CoreInstallCopy.unsupportedArchError
        }
        if joined.contains("gptsovits-bf81cdb.patch") {
            return CoreInstallCopy.missingEnginePatchError
        }
        if joined.contains("already in use") || joined.contains("被别的程序占着") {
            return CoreInstallCopy.addressInUseError
        }
        if joined.contains("health check") || joined.contains("没有响应") {
            return CoreInstallCopy.healthCheckError
        }
        _ = exitCode
        return CoreInstallCopy.genericFailureError
    }

    private func handleLine(_ line: String, startedAt: Date, stateBox: LineStreamState) {
        switch Self.parseLine(line) {
        case .progress(let step, let total, let progress, let stage):
            let currentCancelable = canCancel
            state = .running(
                step: step,
                total: total,
                progress: progress,
                stage: stage,
                startedAt: startedAt,
                canCancel: currentCancelable
            )
            appendLog("[\(step)/\(total)] \(stage)")
        case .switching:
            if case .running(let step, let total, let progress, _, _, _) = state {
                state = .running(
                    step: step,
                    total: total,
                    progress: max(progress, 0.94),
                    stage: CoreInstallCopy.switchingStage,
                    startedAt: startedAt,
                    canCancel: false
                )
            } else {
                state = .running(
                    step: 6,
                    total: 6,
                    progress: 0.94,
                    stage: CoreInstallCopy.switchingStage,
                    startedAt: startedAt,
                    canCancel: false
                )
            }
            appendLog(CoreInstallCopy.switchingStage)
        case .done(let version, let port):
            stateBox.setDone(version: version, port: port)
            appendLog("完成：版本 \(version)")
        case .log(let text):
            if !text.isEmpty {
                appendLog(text)
            }
        }
    }

    private func appendLog(_ line: String) {
        logs.append(line)
        if logs.count > Self.maxLogLines {
            logs.removeFirst(logs.count - Self.maxLogLines)
        }
    }
}

private final class LineStreamState: @unchecked Sendable {
    private let lock = NSLock()
    private var splitter = CoreInstallLineBuffer()
    private var _doneResult: (version: String, port: Int)?

    var doneResult: (version: String, port: Int)? {
        lock.withLock { _doneResult }
    }

    func append(_ chunk: Data) -> [String] {
        lock.withLock { splitter.append(chunk) }
    }

    func flush() -> [String] {
        lock.withLock { splitter.flush() }
    }

    func setDone(version: String, port: Int) {
        lock.withLock { _doneResult = (version, port) }
    }
}
