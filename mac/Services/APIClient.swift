import Foundation
import Observation

struct APIError: LocalizedError, Sendable {
    let status: Int
    let message: String
    var code: String? = nil
    var plan: ComputePlan? = nil
    var errorDescription: String? { message }
}

@MainActor @Observable
final class APIClient {
    var baseURL: String
    @ObservationIgnored var generation = 0
    @ObservationIgnored var onStatus: (String) -> Void = { _ in }
    @ObservationIgnored var onWaitState: (UUID, String?) -> Void = { _, _ in }
    @ObservationIgnored private let session: URLSession
    func isCurrent(_ token: Int) -> Bool { generation == token }
    func ifCurrent(_ token: Int, _ apply: () -> Void) { if generation == token { apply() } }
    init(baseURL: String, session: URLSession? = nil) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 200
        config.timeoutIntervalForResource = 7200
        self.session = session ?? URLSession(configuration: config)
    }
    func url(_ path: String, query: [String: String] = [:]) throws -> URL {
        guard let base = URL(string: baseURL), ["http", "https"].contains(base.scheme), base.host != nil,
              var components = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false) else {
            throw APIError(status: 0, message: "服务地址无效，请在设置中填写 HTTP 或 HTTPS 地址。")
        }
        if !query.isEmpty { components.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) } }
        guard let result = components.url else { throw APIError(status: 0, message: "无法构造请求地址。") }
        return result
    }
    func request(_ path: String, method: String = "GET", query: [String: String] = [:], body: Data? = nil) throws -> URLRequest {
        var r = URLRequest(url: try url(path, query: query))
        r.httpMethod = method
        r.httpBody = body
        if body != nil { r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return r
    }
    func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try JSONDecoder().decode(T.self, from: await data(try request(path, query: query)))
    }
    func send<T: Decodable, B: Encodable>(_ path: String, method: String = "POST", body: B) async throws -> T {
        try JSONDecoder().decode(T.self, from: await data(try request(path, method: method, body: JSONEncoder().encode(body))))
    }
    func post<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try JSONDecoder().decode(T.self, from: await data(try request(path, method: "POST", query: query)))
    }
    func data(_ request: URLRequest, retry503: Bool = true) async throws -> Data {
        let waitID = UUID()
        var waiting = false
        defer { if waiting { onWaitState(waitID, nil) } }
        while true {
            try Task.checkCancellation()
            let (data, response) = try await session.data(for: request)
            let http = response as! HTTPURLResponse
            if (200..<300).contains(http.statusCode) { return data }
            let error = Self.error(data, status: http.statusCode)
            guard http.statusCode == 503, retry503 else { throw error }
            let reason = await describeUnavailable(error.message)
            let delay = Self.retryDelay(http.value(forHTTPHeaderField: "Retry-After"))
            let message = "\(reason) · \(Int(delay)) 秒后重试"
            waiting = true
            onWaitState(waitID, message)
            onStatus(message)
            try await Task.sleep(for: .seconds(delay))
        }
    }
    static func retryDelay(_ header: String?, now: Date = Date()) -> Double {
        if let header, let seconds = Double(header) { return max(1, seconds) }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = "EEE, dd MMM yyyy HH:mm:ss z"
        if let header, let date = f.date(from: header) { return max(1, date.timeIntervalSince(now)) }
        return 5
    }
    static func error(_ data: Data, status: Int) -> APIError {
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let audioDetail = obj["detail"] as? [String: Any] ?? obj
            if audioDetail["code"] as? String == "audio_elsewhere" {
                let machine = (audioDetail["machine"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "另一台设备"
                return APIError(status: status, message: "音频在 \(machine) 上", code: "audio_elsewhere")
            }
            if let detail = obj["detail"] as? String {
                let plan = (obj["plan"] as? [String: Any]).flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { try? JSONDecoder().decode(ComputePlan.self, from: $0) }
                let code = obj["code"] as? String
                return APIError(status: status, message: code == "library_not_selected" ? "请先在设置里选择资料库文件夹。" : detail, code: code, plan: plan)
            }
            if let errors = obj["detail"] as? [[String: Any]] {
                let message = errors.map { item in
                    let field = (item["loc"] as? [Any] ?? []).map { String(describing: $0) }.joined(separator: ".")
                    return "\(field)：\(item["msg"] as? String ?? "参数无效")"
                }.joined(separator: "；")
                return APIError(status: status, message: message)
            }
        }
        return APIError(status: status, message: "请求失败（HTTP \(status)）")
    }
    private func describeUnavailable(_ detail: String) async -> String {
        if let health: Health = try? await getWithoutRetry("/health"), let id = health.training_job {
            return "正在训练：\(id)"
        } else { return detail }
    }
    func getWithoutRetry<T: Decodable>(_ path: String, query: [String: String] = [:], timeout: TimeInterval = 8) async throws -> T {
        var probe = try request(path, query: query)
        probe.timeoutInterval = timeout
        return try JSONDecoder().decode(T.self, from: await data(probe, retry503: false))
    }
    /// Reconnects to a job by id; never re-submits its creation POST.
    func follow(_ initial: Job, update: @escaping (Job) async throws -> Void) async throws -> Job {
        var job = initial
        try await update(job)
        while job.active {
            try Task.checkCancellation()
            do {
                var req = try request("/jobs/\(job.id)/events")
                req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                let (bytes, response) = try await session.bytes(for: req)
                let http = response as! HTTPURLResponse
                guard http.statusCode == 200 else { throw APIError(status: http.statusCode, message: "任务事件连接失败") }
                var parser = SSEParser()
                for try await byte in bytes {
                    try Task.checkCancellation()
                    if let data = parser.consume(byte: byte) {
                        job = try JSONDecoder().decode(Job.self, from: data)
                        try await update(job)
                        if !job.active { break }
                    }
                }
            } catch is CancellationError { throw CancellationError() }
            catch { onStatus("任务事件已断开，正在重新连接…") }
            if !job.active { return try completed(job) }
            try await Task.sleep(for: .seconds(2))
            job = try await get("/jobs/\(job.id)")
            try await update(job)
        }
        return try completed(job)
    }
    private func completed(_ job: Job) throws -> Job {
        guard job.status == "succeeded" else { throw APIError(status: 0, message: job.error ?? job.statusLabel) }
        return job
    }
    func upload(file: URL, text: String, character: String, lang: String) async throws -> Ref {
        let boundary = "TTS-\(UUID().uuidString)"
        var body = Data()
        func add(_ string: String) { body.append(Data(string.utf8)) }
        for (key, value) in [("text", text), ("character", character), ("lang", lang)] {
            add("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n")
        }
        add("--\(boundary)\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"reference.\(file.pathExtension)\"\r\nContent-Type: application/octet-stream\r\n\r\n")
        body.append(try Data(contentsOf: file))
        add("\r\n--\(boundary)--\r\n")
        var req = try request("/refs/upload", method: "POST")
        req.httpBody = body
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        return try JSONDecoder().decode(Ref.self, from: await data(req))
    }
}


extension APIClient {
    func pollCloudLogin(_ id: String, every interval: Duration = .seconds(2), update: (CloudLoginStatus) async -> Void, report: (Error) -> Void) async {
        while !Task.isCancelled {
            do {
                let value: CloudLoginStatus = try await getWithoutRetry("/cloud/accounts/login/\(id)")
                try Task.checkCancellation()
                await update(value)
                if value.state == "done" || value.state == "failed" { return }
            } catch is CancellationError { return }
            catch { if Task.isCancelled { return }; report(error) }
            do { try await Task.sleep(for: interval) } catch { return }
        }
    }
}
