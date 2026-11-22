import Foundation
import Observation
import Darwin

@MainActor @Observable
final class StudioConnection {
    private let defaults: UserDefaults
    var role: String
    var localPort: Int
    var address: String
    var alias: String
    var wakeMAC: String
    var broadcast: String
    @ObservationIgnored private let sendWake: (String, String) throws -> Void
    @ObservationIgnored private let localCoreInstalled: () -> Bool
    var configured: Bool
    var waking = false
    private var localCoreRevision = 0
    private var wakeSent: Date?
    var baseURL: String { role == "control" ? "http://127.0.0.1:\(localPort)" : address }
    var isClient: Bool { role == "client" }
    var controllerName: String {
        let trimmed = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "主控" : trimmed
    }
    var isLocalCoreInstalled: Bool {
        _ = localCoreRevision
        return localCoreInstalled()
    }
    var localCoreMissing: Bool { !isClient && !isLocalCoreInstalled }
    func notifyLocalCoreChanged() { localCoreRevision &+= 1 }
    var libraryFolderHint: String { "在 \(controllerName) 上选择文件夹" }
    var libraryConflictHint: String { "在 \(controllerName) 上查看" }
    var localCoreNotice: String { "这台电脑还没有安装主控服务，请先安装或选「连接到另一台」。" }
    nonisolated static func defaultLocalCoreInstalled(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        let paths = [
            "Library/Application Support/TTSStudio/core/current/core/ttsapi",
            "Library/LaunchAgents/com.dreaife.tts-core.plist",
            "Library/LaunchAgents/com.dreaife.tts-api.plist",
        ]
        return paths.contains { FileManager.default.fileExists(atPath: home.appending(path: $0).path) }
    }
    init(
        defaults: UserDefaults = .standard,
        sendWake: @escaping (String, String) throws -> Void = { try WakePacket.send(mac: $0, broadcast: $1) },
        localCoreInstalled: @escaping () -> Bool = { StudioConnection.defaultLocalCoreInstalled() }
    ) {
        self.defaults = defaults
        self.sendWake = sendWake
        self.localCoreInstalled = localCoreInstalled
        role = defaults.string(forKey: "studioRole") ?? "control"
        localPort = defaults.object(forKey: "corePort") as? Int ?? 9880
        address = defaults.string(forKey: "controlAddress") ?? ""
        alias = defaults.string(forKey: "controlAlias") ?? "主控"
        wakeMAC = defaults.string(forKey: "controlWakeMAC") ?? ""
        broadcast = defaults.string(forKey: "controlBroadcast") ?? "192.168.1.255"
        configured = defaults.bool(forKey: "identityConfigured")
    }
    func save() {
        defaults.set(role, forKey: "studioRole")
        defaults.set(localPort, forKey: "corePort")
        defaults.set(address, forKey: "controlAddress")
        defaults.set(alias, forKey: "controlAlias")
        defaults.set(wakeMAC, forKey: "controlWakeMAC")
        defaults.set(broadcast, forKey: "controlBroadcast")
        configured = true; defaults.set(true, forKey: "identityConfigured")
        wakeSent = nil; waking = false
    }
    func retryWake() { wakeSent = nil; waking = false }
    func wakeIfNeeded() throws {
        guard isClient, !wakeMAC.isEmpty else { return }
        if let wakeSent { waking = Date().timeIntervalSince(wakeSent) < 150; return }
        try sendWake(wakeMAC, broadcast)
        wakeSent = Date(); waking = true
    }
}
enum WakePacket {
    static func bytes(mac: String) throws -> [UInt8] {
        let parts = mac.replacingOccurrences(of: "-", with: ":").split(separator: ":")
        let address = parts.compactMap { UInt8($0, radix: 16) }
        guard address.count == 6 else { throw APIError(status: 0, message: "唤醒地址需要六组字符。") }
        return Array(repeating: UInt8(255), count: 6) + Array(repeating: address, count: 16).flatMap { $0 }
    }
    static func send(mac: String, broadcast: String = "192.168.1.255") throws {
        let packet = try bytes(mac: mac)
        let descriptor = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard descriptor >= 0 else { throw POSIXError(.init(rawValue: errno)!) }
        defer { close(descriptor) }
        var enabled: Int32 = 1
        guard setsockopt(descriptor, SOL_SOCKET, SO_BROADCAST, &enabled, socklen_t(MemoryLayout.size(ofValue: enabled))) == 0 else { throw POSIXError(.init(rawValue: errno)!) }
        var destination = sockaddr_in()
        destination.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        destination.sin_family = sa_family_t(AF_INET)
        destination.sin_port = UInt16(9).bigEndian
        destination.sin_addr.s_addr = inet_addr(broadcast)
        let sent = withUnsafePointer(to: &destination) { address in
            address.withMemoryRebound(to: sockaddr.self, capacity: 1) { target in
                packet.withUnsafeBytes { sendto(descriptor, $0.baseAddress, $0.count, 0, target, socklen_t(MemoryLayout<sockaddr_in>.size)) }
            }
        }
        guard sent == packet.count else { throw POSIXError(.init(rawValue: errno)!) }
    }
}
