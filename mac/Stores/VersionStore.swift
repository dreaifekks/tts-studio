import SwiftUI
import Observation

@MainActor @Observable
final class VersionStore {
    let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as! String
    var latest: SystemVersions?
    var serverVersion: String?
    var checking = false
    var error: String?
    var serverUpdating = false
    var serverProgress = ""
    var macUpdating = false
    var macProgress = ""
    var macAvailable: Bool { latest?.studio.latest.map { hasNewerVersion($0, than: appVersion) } ?? false }
    var serverAvailable: Bool { false }
    var localCoreVersionMismatch: Bool {
        Self.isCoreVersionMismatched(serverVersion: serverVersion, appVersion: appVersion)
    }
    var updateAvailable: Bool { macAvailable || serverAvailable }

    nonisolated static func isCoreVersionMismatched(serverVersion: String?, appVersion: String) -> Bool {
        guard let serverVersion else { return false }
        let normalize: (String) -> String = { raw in
            var trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.hasPrefix("v") || trimmed.hasPrefix("V") {
                trimmed.removeFirst()
            }
            return trimmed
        }
        let lhs = normalize(serverVersion)
        let rhs = normalize(appVersion)
        guard !lhs.isEmpty, !rhs.isEmpty else { return false }
        return lhs != rhs
    }

    func checkLatest(api: APIClient, refresh: Bool = false) async {
        guard !checking else { return }
        let token = api.generation
        checking = true
        defer { api.ifCurrent(token) { checking = false } }
        do {
            let value: SystemVersions = try await api.getWithoutRetry("/system/versions", query: refresh ? ["refresh":"true"] : [:], timeout: 30)
            api.ifCurrent(token) {
                latest = value
                serverVersion = value.core.current
                error = value.studio.error == nil ? nil : "暂时查不到新版本"
            }
        } catch {
            if Task.isCancelled || error is CancellationError || (error as? URLError)?.code == .cancelled { return }
            api.ifCurrent(token) { self.error = "暂时查不到新版本" }
        }
    }
    func readCurrent(api: APIClient) async {
        guard !serverUpdating else { return }
        let token = api.generation
        do {
            let health: VersionHealth = try await api.getWithoutRetry("/health")
            api.ifCurrent(token) { if !serverUpdating { serverVersion = health.version } }
        } catch {
            api.ifCurrent(token) { self.error = "暂时查不到新版本" }
        }
    }
    func updateServer(baseURL: String, release: ReleaseInfo, session: URLSession? = nil) async {
        guard !serverUpdating else { return }
        serverUpdating = true
        error = nil
        serverProgress = "正在提交更新…"
        defer { serverUpdating = false }
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 12
        let healthAPI = APIClient(baseURL: baseURL, session: session ?? URLSession(configuration: config))
        let postConfig = URLSessionConfiguration.default
        postConfig.timeoutIntervalForRequest = 240
        postConfig.timeoutIntervalForResource = 240
        let api = APIClient(baseURL: baseURL, session: session ?? URLSession(configuration: postConfig))
        do {
            let request = try api.request("/system/update", method: "POST", body: JSONEncoder().encode(["tag":release.tag]))
            let accepted = try JSONDecoder().decode(UpdateAccepted.self, from: await api.data(request, retry503: false))
            if accepted.from == accepted.to {
                serverVersion = release.version
                serverProgress = "已是该版本：\(release.version)"
                return
            }
            let deadline = Date().addingTimeInterval(300)
            while Date() < deadline {
                serverProgress = "重启中 · 等待 \(release.version)…"
                try await Task.sleep(for: .seconds(2))
                if let health: VersionHealth = try? await healthAPI.getWithoutRetry("/health") {
                    serverVersion = health.version
                    if health.version == release.version { serverProgress = "服务端已更新到 \(release.version)"; return }
                }
            }
            throw APIError(status: 0, message: "等待服务端更新超时，目标版本 \(release.version)。请检查服务状态。")
        } catch { self.error = error.localizedDescription; serverProgress = "更新未完成" }
    }
    func updateMac(release: ReleaseInfo) async {
        guard !macUpdating else { return }
        macUpdating = true
        error = nil
        defer { macUpdating = false }
        do {
            try await TerminalUpdater.open(tag: release.tag)
            macProgress = "已请求终端打开更新脚本，请在终端查看进度。若未开始，请复制更新命令到终端执行。"
        } catch { self.error = error.localizedDescription; macProgress = "未能打开终端更新" }
    }
}
