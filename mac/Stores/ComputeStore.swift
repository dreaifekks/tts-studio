import Foundation
import Observation

@MainActor @Observable
final class ComputeStore {
    let api: APIClient
    private let defaults: UserDefaults
    var devices: [ComputeDevice] = []
    var engines: [String] = []
    var automatic = AutoSettings()
    var monthUsed: Double = 0
    var library: LibraryInfo?
    var cloud: CloudInfo?
    var systemInfo: SystemInfo?
    var storage: StorageOverview?
    var storageLoading = false
    var accountErrorDevice: String?
    var accountError: String?
    var libraryChecking = false
    var loading = false
    var error: String?
    var cloudError: String?
    @ObservationIgnored private var storageTask: Task<Void, Never>?
    @ObservationIgnored private var devicesRefreshSeq = 0
    @ObservationIgnored private var storageRefreshSeq = 0
    @ObservationIgnored private var refreshRevision = 0
    var commonModels: Set<String> { didSet { defaults.set(Array(commonModels).sorted(), forKey: "commonModels") } }
    var preferredEngine: String {
        Self.resolvePreferredEngine(commonModels: commonModels, availableEngines: engines)
    }
    static func resolvePreferredEngine(commonModels: Set<String>, availableEngines: [String]) -> String {
        let available = sortedEngines(availableEngines)
        if !commonModels.isEmpty {
            let orderedCommon = sortedEngines(commonModels)
            if available.isEmpty {
                return orderedCommon.first ?? "v2proplus"
            }
            if commonModels.count == 1, let only = orderedCommon.first, available.contains(only) {
                return only
            }
            if commonModels.contains("v2proplus") && available.contains("v2proplus") {
                return "v2proplus"
            }
            if let match = orderedCommon.first(where: { available.contains($0) }) {
                return match
            }
        }
        if available.isEmpty || available.contains("v2proplus") {
            return "v2proplus"
        }
        if available.contains("v2pro") {
            return "v2pro"
        }
        return available.first ?? "v2proplus"
    }
    var summary: String {
        var items: [String] = []
        if library?.selected == true { items.append(libraryChecking ? "资料库正在同步" : "资料库已同步") }
        for device in devices where device.type != "cloud" { items.append("\(device.name) \(device.statusLabel)") }
        let activeCloud = devices.filter(\.isCloudActive)
        if !activeCloud.isEmpty {
            items.append(contentsOf: activeCloud.map { "\($0.name) \($0.statusLabel)" })
        } else {
            let warmCloud = devices.filter(\.isCloudWarm)
            if !warmCloud.isEmpty {
                items.append(contentsOf: warmCloud.map { "\($0.name) 保留中" })
            } else {
                items.append("云端 GPU 未开")
            }
        }
        let localDiskFree = devices.first(where: { $0.type == "local" })?.disk_free_bytes ?? storage?.disk.free_bytes
        if let free = localDiskFree, free < StorageOverview.lowDiskThresholdBytes {
            items.append("磁盘剩余 \(displayBytes(free))")
        }
        return items.joined(separator: " · ")
    }
    init(api: APIClient, defaults: UserDefaults = .standard) {
        self.api = api; self.defaults = defaults
        commonModels = Set(defaults.stringArray(forKey: "commonModels") ?? ["v2proplus"])
    }
    func reset() {
        storageTask?.cancel(); storageTask = nil
        devicesRefreshSeq += 1; storageRefreshSeq += 1; refreshRevision += 1
        devices = []; engines = []; automatic = AutoSettings(); monthUsed = 0
        library = nil; cloud = nil; systemInfo = nil
        storage = nil; storageLoading = false
        accountErrorDevice = nil; accountError = nil
        libraryChecking = false; loading = false; error = nil; cloudError = nil
    }
    private func isCancelled(_ error: Error) -> Bool {
        Task.isCancelled || error is CancellationError || (error as? URLError)?.code == .cancelled
    }
    func refresh(refreshAccounts: Bool = false) async {
        let token = api.generation
        refreshRevision += 1
        let rev = refreshRevision
        loading = true; error = nil
        defer {
            api.ifCurrent(token) {
                if refreshRevision == rev { loading = false }
            }
        }
        if !storageLoading {
            storageTask?.cancel()
            storageTask = Task { [weak self] in
                guard let self else { return }
                await self.refreshStorage(refresh: false)
            }
        }
        do { try await refreshDevices() } catch {
            if isCancelled(error) { return }
            api.ifCurrent(token) { self.error = error.localizedDescription }
        }
        guard api.isCurrent(token), !Task.isCancelled else { return }
        do {
            let value: LibraryInfo = try await api.getWithoutRetry("/library")
            api.ifCurrent(token) { library = value }
        } catch {
            if isCancelled(error) { return }
            api.ifCurrent(token) { self.error = error.localizedDescription }
        }
        guard api.isCurrent(token), !Task.isCancelled else { return }
        await refreshCloud(refresh: refreshAccounts)
        guard api.isCurrent(token), !Task.isCancelled else { return }
        do {
            let info: SystemInfo = try await api.getWithoutRetry("/system/info")
            api.ifCurrent(token) { systemInfo = info }
        } catch {
            if isCancelled(error) { return }
            api.ifCurrent(token) { self.error = error.localizedDescription }
        }
    }
    func refreshStorage(refresh: Bool = false) async {
        let token = api.generation
        storageRefreshSeq += 1
        let seq = storageRefreshSeq
        storageLoading = true
        defer {
            api.ifCurrent(token) {
                if storageRefreshSeq == seq { storageLoading = false }
            }
        }
        do {
            let value: StorageOverview = try await api.getWithoutRetry(
                "/system/storage",
                query: refresh ? ["refresh": "true"] : [:],
                timeout: refresh ? 60 : 15
            )
            api.ifCurrent(token) {
                guard seq == storageRefreshSeq else { return }
                storage = value
            }
        } catch {
            if isCancelled(error) { return }
            // Silently ignore errors (e.g. 404 on older controllers) without polluting compute.error.
        }
    }
    func refreshDevices() async throws {
        let token = api.generation
        devicesRefreshSeq += 1
        let seq = devicesRefreshSeq
        let value: ComputeList = try await api.getWithoutRetry("/compute")
        api.ifCurrent(token) {
            guard seq == devicesRefreshSeq else { return }
            devices = value.devices; engines = value.engines; automatic = value.auto; monthUsed = value.month_used
        }
    }
    func refreshCloud(refresh: Bool = false) async {
        let token = api.generation
        do {
            let value: CloudInfo = try await api.getWithoutRetry("/cloud", query: refresh ? ["refresh":"true"] : [:], timeout: 200)
            api.ifCurrent(token) { cloud = value; cloudError = nil }
        } catch {
            if isCancelled(error) { return }
            api.ifCurrent(token) { cloudError = "云端 GPU 信息暂时取不到" }
        }
    }
    func selectLibrary(_ path: String) async throws {
        let token = api.generation
        let value: LibraryInfo = try await api.send("/library", body: ["path":path])
        guard api.isCurrent(token) else { return }
        library = value
        storageTask?.cancel(); storageTask = nil
        storageRefreshSeq += 1
        storage = nil
        await refreshStorage(refresh: true)
    }
    func rescan() async throws {
        let token = api.generation
        libraryChecking = true
        defer { api.ifCurrent(token) { libraryChecking = false } }
        let value: LibraryInfo = try await api.post("/library/rescan")
        api.ifCurrent(token) { library = value }
    }
    func claim() async throws {
        let token = api.generation
        let value: LibraryInfo = try await api.post("/library/claim")
        api.ifCurrent(token) { library = value }
    }
    func saveAutomatic(_ value: AutoSettings) async throws {
        let token = api.generation
        let saved: AutoSettings = try await api.send("/compute/auto", method: "PUT", body: value)
        api.ifCurrent(token) { automatic = saved }
    }
    func patch(_ id: String, fields: [String: JSONValue]) async throws {
        let token = api.generation
        let _: ComputeDevice = try await api.send("/compute/devices/\(id)", method: "PATCH", body: fields)
        guard api.isCurrent(token) else { return }
        try await refreshDevices()
    }
    func remove(_ id: String) async throws {
        let token = api.generation
        _ = try await api.data(api.request("/compute/devices/\(id)", method: "DELETE"), retry503: false)
        guard api.isCurrent(token) else { return }
        try await refreshDevices()
    }
    func addDevice(name: String, address: String, pairCode: String, wake: DeviceWake? = nil) async throws -> ComputeDevice {
        let token = api.generation
        var fields: [String: JSONValue] = ["name":.string(name), "address":.string(address), "pair_code":.string(pairCode)]
        if let wake { fields["wake"] = wake.jsonValue }
        do {
            let device: ComputeDevice = try await api.send("/compute/devices", body: fields)
            api.ifCurrent(token) { devices.append(device) }
            return device
        } catch let failure as APIError where failure.status == 400 {
            throw APIError(status: 400, message: failure.message.contains("配对") ? "配对码不对或已过期，请从那台电脑重新取得配对码。" : "连不上这台设备，请检查连接地址。")
        }
    }
    func repairDevice(_ id: String, address: String, pairCode: String, fields: [String: JSONValue] = [:]) async throws {
        var values = fields
        values["address"] = .string(address); values["pair_code"] = .string(pairCode)
        do { try await patch(id, fields: values) }
        catch let failure as APIError where failure.status == 400 {
            throw APIError(status: 400, message: "重新配对没有完成，请检查连接地址和配对码是否有效。")
        }
    }
    func benchmark(device: String, engine: String? = nil, quick: Bool = false) async throws -> Job {
        var fields: [String: JSONValue] = ["device":.string(device), "quick":.bool(quick)]
        if let engine { fields["engines"] = .array([.string(engine)]) }
        return try await api.send("/bench", body: fields)
    }
    func saveCloud(_ value: CloudSettings) async throws {
        let token = api.generation
        let request = try api.request("/cloud/settings", method: "PUT", body: JSONEncoder().encode(value))
        _ = try await api.data(request, retry503: false)
        guard api.isCurrent(token) else { return }
        await refreshCloud()
    }
    func disableAccount(_ id: String) async throws {
        let token = api.generation
        _ = try await api.data(api.request("/cloud/accounts/\(id)/disable", method: "POST"), retry503: false)
        guard api.isCurrent(token) else { return }
        clearAccountError(id)
        await refreshCloud()
    }
    func refreshAccount(_ id: String) async throws {
        let token = api.generation
        _ = try await api.data(api.request("/cloud/accounts/\(id)/refresh", method: "POST"), retry503: false)
        guard api.isCurrent(token) else { return }
        clearAccountError(id)
        await refreshCloud()
    }
}

extension ComputeStore {
    func clearAccountError(_ id: String) {
        if accountErrorDevice == id { accountError = nil; accountErrorDevice = nil }
    }
    func enableAccount(_ id: String) async throws {
        let token = api.generation
        _ = try await api.data(api.request("/cloud/accounts/\(id)/enable", method: "POST"), retry503: false)
        guard api.isCurrent(token) else { return }
        clearAccountError(id)
        await refreshCloud()
    }
    func removeAccount(_ id: String) async throws {
        let token = api.generation
        clearAccountError(id)
        do { _ = try await api.data(api.request("/cloud/accounts/\(id)", method: "DELETE"), retry503: false) }
        catch let failure as APIError where failure.status == 502 {
            guard api.isCurrent(token) else { return }
            await refreshCloud()
            api.ifCurrent(token) {
                accountErrorDevice = id
                accountError = "这个账号的授权没删掉，已先停用，可以再试"
            }
            return
        }
        guard api.isCurrent(token) else { return }
        await refreshCloud()
    }
    func cancelLogin(_ id: String) async throws {
        let token = api.generation
        do { _ = try await api.data(api.request("/cloud/accounts/login/\(id)", method: "DELETE"), retry503: false) }
        catch let failure as APIError where failure.status == 502 {
            guard api.isCurrent(token) else { return }
            await refreshCloud()
            throw APIError(status: 502, message: "这个账号的授权没删掉，已先停用，可以再试")
        } catch {
            guard api.isCurrent(token) else { return }
            await refreshCloud()
            throw error
        }
        guard api.isCurrent(token) else { return }
        await refreshCloud()
    }
}

/// Owns one invitation. Replaced/cancelled requests cannot advance the visible flow.
@MainActor @Observable
final class DeviceInviteFlow {
    let compute: ComputeStore
    var network = "lan"
    var step = 1
    var invite: ComputeInvite?
    var device: ComputeDevice?
    var name = ""
    var quick = true
    var busy = false
    var expired = false
    var installing = false
    var error: String?
    var manualError: String?
    var manualAddress = ""
    var deadline: Date?
    private var invitationNetwork: String?
    private var revision = 0
    init(compute: ComputeStore) { self.compute = compute }
    var pollingKey: String { "\(invite?.id ?? "")-\(revision)-\(busy)" }
    func nextFromNetwork() async {
        if device != nil { step = 4; return }
        if invite != nil, invitationNetwork == network, !expired, !locallyExpired {
            step = 2
        } else { await generate() }
    }
    var locallyExpired: Bool { !installing && (deadline.map { $0 <= Date() } ?? false) }
    func generate() async {
        revision += 1
        let version = revision
        let selectedNetwork = network
        error = nil; manualError = nil; busy = true
        defer { if revision == version { busy = false } }
        do {
            let value = try await compute.createInvite(network: selectedNetwork)
            guard revision == version, !Task.isCancelled else {
                try? await compute.cancelInvite(value.id)
                return
            }
            invite = value; deadline = Date().addingTimeInterval(value.expires_in)
            invitationNetwork = selectedNetwork; expired = false; installing = false; step = 2
        } catch is CancellationError { }
        catch {
            guard revision == version, !Task.isCancelled else { return }
            if let failure = error as? APIError, failure.code == "tailscale_unavailable" {
                step = 1; self.error = "这台主控还没连上 Tailscale，请连接后重试，或选择家里的局域网。"
            } else { self.error = "配对码没能生成，请检查连接后重试。" }
        }
    }
    func poll(_ id: String) async {
        guard !busy else { return }
        let version = revision
        while !Task.isCancelled, revision == version, invite?.id == id, device == nil, !expired {
            do {
                let value = try await compute.inviteStatus(id)
                guard !Task.isCancelled, revision == version, invite?.id == id, device == nil else { return }
                apply(value)
                if expired || device != nil { return }
            } catch is CancellationError { return }
            catch {
                guard !Task.isCancelled, revision == version, invite?.id == id, device == nil else { return }
                if let failure = error as? APIError, [404, 410].contains(failure.status) {
                    expired = true; self.error = nil; return
                }
                if locallyExpired { expired = true; self.error = nil; return }
                self.error = "连接状态暂时取不到，正在重试…"
            }
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
        }
    }
    func apply(_ value: ComputeInviteStatus) {
        error = nil
        if value.state == "expired" { expired = true; installing = false; return }
        if value.state == "connected", let device = value.device { connected(device); return }
        let started = value.installing && !installing
        installing = value.installing
        expired = false
        // 刚开始安装时跳到第 3 步一次；之后用户自己回前面几步（换网络、看命令）不再拽回来
        if installing { deadline = nil; if started && step < 3 { step = 3 } }
        else { deadline = Date().addingTimeInterval(value.expires_in) }
    }
    func connected(_ value: ComputeDevice) {
        device = value; name = value.name; step = 4; expired = false; installing = false; error = nil; manualError = nil
        if let index = compute.devices.firstIndex(where: { $0.id == value.id }) { compute.devices[index] = value }
        else { compute.devices.append(value) }
    }
    func manual() async {
        guard device == nil, !busy, !installing else { return }
        guard let id = invite?.id, !expired, !locallyExpired else { expired = true; return }
        let version = revision
        busy = true; manualError = nil
        defer { if version == revision { busy = false } }
        do {
            let value = try await compute.manualInvite(id, address: manualAddress)
            guard version == revision, invite?.id == id, !Task.isCancelled, device == nil else { return }
            apply(value)
        } catch {
            guard version == revision, invite?.id == id, !Task.isCancelled, device == nil else { return }
            if let failure = error as? APIError, failure.code == "invite_expired" || [404, 410].contains(failure.status) { expired = true; installing = false }
            else if let failure = error as? APIError, failure.code == "invite_used" {
                manualError = "配对码已使用，正在查看连接结果…"
            } else { manualError = "连接失败，请检查连接地址后重试。" }
        }
    }
    func finish() async throws -> Job? {
        guard let device else { return nil }
        if name != device.name { try await compute.patch(device.id, fields: ["name":.string(name)]) }
        return quick ? try await compute.benchmark(device: device.id, quick: true) : nil
    }
    func close() async {
        revision += 1
        let old = invite
        invite = nil; deadline = nil; busy = false
        if let old { try? await compute.cancelInvite(old.id) }
    }
}

extension ComputeStore {
    func createInvite(network: String) async throws -> ComputeInvite {
        let request = try api.request("/compute/invites", method: "POST", body: JSONEncoder().encode(["network":network]))
        return try JSONDecoder().decode(ComputeInvite.self, from: await api.data(request, retry503: false))
    }
    func inviteStatus(_ id: String) async throws -> ComputeInviteStatus { try await api.getWithoutRetry("/compute/invites/\(id)") }
    func cancelInvite(_ id: String) async throws { _ = try await api.data(api.request("/compute/invites/\(id)", method: "DELETE"), retry503: false) }
    func manualInvite(_ id: String, address: String) async throws -> ComputeInviteStatus {
        let request = try api.request("/compute/invites/\(id)/manual", method: "POST", body: JSONEncoder().encode(["address":address]))
        return try JSONDecoder().decode(ComputeInviteStatus.self, from: await api.data(request, retry503: false))
    }
}

/// Tracks the active login in the add-cloud wizard so cancelling, switching accounts, or leaving before step 3
/// calls DELETE /cloud/accounts/login/{id}. Post-done deletion follows core's `added` flag exclusively.
@MainActor @Observable
final class CloudLoginAddition {
    private(set) var pendingLoginID: String?
    func begin(_ loginID: String) { pendingLoginID = loginID }
    func finish(_ status: CloudLoginStatus) {
        if status.state == "done" {
            if status.added != true { pendingLoginID = nil }
        } else if status.state == "failed" {
            pendingLoginID = nil
        }
    }
    func discard(_ compute: ComputeStore) async throws {
        guard let id = pendingLoginID else { return }
        pendingLoginID = nil
        try await compute.cancelLogin(id)
    }
}

@MainActor @Observable
final class CloudCodeSubmission {
    var submitted = false
    var working = false
    var error: String?
    func submit(api: APIClient, id: String, code: String) async {
        guard !submitted, !working else { return }
        submitted = true; working = true; error = nil
        defer { working = false }
        do {
            _ = try await api.data(api.request("/cloud/accounts/login/\(id)", method: "POST", body: JSONEncoder().encode(["code":code])), retry503: false)
            try Task.checkCancellation()
        } catch is CancellationError { submitted = false }
        catch { submitted = false; self.error = "授权码没有提交成功，请重试。" }
    }
}
