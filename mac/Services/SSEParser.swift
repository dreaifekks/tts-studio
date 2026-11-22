import Foundation

/// SSE fields can span multiple data lines; comments are keepalives.
struct SSEParser {
    private var dataLines: [String] = []
    private var lineBytes: [UInt8] = []
    private var previousWasCR = false

    /// URLSession.AsyncBytes.lines discards blank lines; consume the raw stream.
    mutating func consume(byte: UInt8) -> Data? {
        if byte == 10 && previousWasCR { previousWasCR = false; return nil }
        previousWasCR = byte == 13
        if byte == 10 || byte == 13 {
            let line = String(decoding: lineBytes, as: UTF8.self)
            lineBytes.removeAll(keepingCapacity: true)
            return consume(line)
        }
        lineBytes.append(byte)
        return nil
    }
    mutating func consume(_ line: String) -> Data? {
        if line.isEmpty {
            guard !dataLines.isEmpty else { return nil }
            defer { dataLines.removeAll() }
            return dataLines.joined(separator: "\n").data(using: .utf8)
        }
        if line.hasPrefix("data:") {
            var field = String(line.dropFirst(5))
            if field.first == " " { field.removeFirst() }
            dataLines.append(field)
        }
        return nil
    }
}
