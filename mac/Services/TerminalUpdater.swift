import AppKit

@MainActor
enum TerminalUpdater {
    // update.command reads and consumes the same user-cache file.
    static var tagFile: URL { FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/Caches/TTSStudio/update-tag") }
    static func prepare(tag: String, directory: URL? = nil) throws -> URL {
        let folder = directory ?? tagFile.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try tag.write(to: folder.appending(path: "update-tag"), atomically: true, encoding: .utf8)
        return Bundle.main.url(forResource: "update", withExtension: "command")!
    }
    static func shellCommand(tag: String) -> String {
        let path = Bundle.main.url(forResource: "update", withExtension: "sh")!.path
        func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'" }
        return "/bin/bash " + quote(path) + " " + quote(tag)
    }
    static func open(tag: String) async throws {
        let command = try prepare(tag: tag)
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        _ = try await NSWorkspace.shared.open([command], withApplicationAt: URL(filePath: "/System/Applications/Utilities/Terminal.app"), configuration: configuration)
    }
}
