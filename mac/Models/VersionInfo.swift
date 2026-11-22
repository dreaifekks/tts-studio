import Foundation

struct ReleaseInfo: Decodable, Sendable {
    let latest: String
    let url: URL
    let published_at: String
    let notes: String
    var tag: String { latest.hasPrefix("v") ? latest : "v" + latest }
    var version: String { latest.hasPrefix("v") ? String(latest.dropFirst()) : latest }
    var published: String {
        guard let date = ISO8601DateFormatter().date(from: published_at) else { return published_at }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}
struct VersionHealth: Decodable, Sendable { let version: String?; let commit: String? }
struct UpdateAccepted: Decodable { let from: String; let to: String }

func hasNewerVersion(_ latest: String, than current: String?) -> Bool {
    guard let current else { return false }
    func parts(_ value: String) -> [Int] {
        value.trimmingCharacters(in: CharacterSet(charactersIn: "v"))
            .split(whereSeparator: { $0 == "-" || $0 == "+" })[0].split(separator: ".").compactMap { Int($0) }
    }
    let left = parts(latest), right = parts(current)
    if left != right { return right.lexicographicallyPrecedes(left) }
    return false
}
