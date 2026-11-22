import XCTest
import SwiftUI
@testable import TTSStudio

private final class Stage4AccountState: @unchecked Sendable {
    private let lock = NSLock()
    private var disabled: [String: Bool] = [:]
    func set(_ id: String, disabled: Bool) { lock.lock(); defer { lock.unlock() }; self.disabled[id] = disabled }
    func value(_ id: String) -> Bool { lock.lock(); defer { lock.unlock() }; return disabled[id] ?? false }
}
private final class Stage2Protocol: URLProtocol, @unchecked Sendable {
    private static let accountState = Stage4AccountState()
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        let host = request.url!.host!
        if host == "offline.fixture" { client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost, userInfo: [NSLocalizedDescriptionKey: "主控暂时连不上"])); return }
        if host == "remove-failure.fixture", request.httpMethod == "DELETE" { respond(502, #"{"detail":"mock authorization deletion failed"}"#); return }
        if host == "failure.fixture", ["/cloud", "/system/versions"].contains(path) {
            respond(404, #"{"detail":"Not Found"}"#); return
        }
        if host == "account-state.fixture", request.httpMethod == "POST", path.hasPrefix("/cloud/accounts/") {
            let id = request.url!.pathComponents[3]
            if path.hasSuffix("/disable") { Self.accountState.set(id, disabled: true) }
            if path.hasSuffix("/enable") { Self.accountState.set(id, disabled: false) }
            respond(200, "{}"); return
        }
        if host == "pair-expired.fixture", request.httpMethod == "POST" { respond(400, #"{"detail":"配对码已过期"}"#); return }
        if host == "pair-failed.fixture", request.httpMethod == "PATCH" { respond(400, #"{"detail":"ConnectError: unavailable"}"#); return }
        if host == "evaluation-failure.fixture", path.hasSuffix("/versions") || path == "/weights" {
            respond(404, #"{"detail":"character no longer exists"}"#); return
        }
        if host == "blind-unavailable.fixture", path == "/blind/expired/generate" {
            respond(409, #"{"code":"version_unavailable","detail":"missing bound weights"}"#); return
        }
        if host == "blind-unavailable.fixture", path == "/blind/expired" {
            respond(200, #"{"id":"expired","title":"既有盲听","created":"2026-11-20","character":"alice","eval_set":"mini","repeats":1,"ref_id":"r","status":"failed","job_id":"old","n_questions":3,"n_ready":0,"n_voted":0,"revealed":false,"questions":[]}"#); return
        }
        if host == "r4.fixture", path == "/blind/r4-blind" {
            respond(200, #"{"id":"r4-blind","title":"alice · mini · 2026-11-20 23:00","created":"2026-11-20T23:00:00","character":"alice","eval_set":"mini","repeats":1,"ref_id":"r","status":"failed","job_id":"old","n_questions":3,"n_ready":1,"n_voted":0,"revealed":false,"questions":[]}"#); return
        }
        if host == "r4.fixture", path == "/blind" {
            respond(200, #"{"total":1,"tests":[{"id":"r4-blind","title":"alice · mini · 2026-11-20 23:00","created":"2026-11-20T23:00:00","character":"alice","eval_set":"mini","repeats":1,"ref_id":"r","status":"failed","job_id":"old","n_questions":3,"n_ready":1,"n_voted":0,"revealed":false}]}"#); return
        }
        let value: String
        switch path {
        case "/library": value = Stage2Fixtures.library
        case "/system/info": value = Stage2Fixtures.systemInfo
        case let versionPath where versionPath.hasPrefix("/characters/") && versionPath.hasSuffix("/versions"):
            value = host == "empty-version.fixture" ? "[]" : (versionPath.contains("/bob/") ? Stage2Fixtures.versions.replacingOccurrences(of: "11 月训练", with: "bob的训练") : Stage2Fixtures.versions)
        case "/refs": value = Stage2Fixtures.refs
        case "/eval-sets": value = Stage2Fixtures.evalSets
        case "/models": value = Stage2Fixtures.models
        case "/jobs": value = #"{"jobs":[],"total":0}"#
        case "/compute":
            if host == "empty.fixture" {
                var obj = try! JSONSerialization.jsonObject(with: Data(Stage2Fixtures.compute.utf8)) as! [String: Any]
                obj["devices"] = []
                value = String(decoding: try! JSONSerialization.data(withJSONObject: obj), as: UTF8.self)
            } else if host == "empty-version.fixture" {
                var obj = try! JSONSerialization.jsonObject(with: Data(Stage2Fixtures.compute.utf8)) as! [String: Any]
                var devices = obj["devices"] as! [[String: Any]]
                for i in devices.indices where devices[i]["type"] as? String != "cloud" { devices[i]["status"] = "paused" }
                obj["devices"] = devices
                value = String(decoding: try! JSONSerialization.data(withJSONObject: obj), as: UTF8.self)
            } else { value = Stage2Fixtures.compute }
        case "/cloud":
            if host == "empty.fixture" {
                var cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
                cloud.installed = false; cloud.accounts = []; cloud.total_balance = 0; cloud.month_used_estimate = 0; cloud.usage_monthly = []
                for i in cloud.gpus.indices { cloud.gpus[i].available = false; cloud.gpus[i].account_count = 0; cloud.gpus[i].hours_left = nil; cloud.gpus[i].reason = "没有装云端 GPU 组件" }
                value = String(decoding: try! JSONEncoder().encode(cloud), as: UTF8.self)
            } else if host == "account-state.fixture" {
                var cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
                for i in cloud.accounts.indices { cloud.accounts[i].disabled = Self.accountState.value(cloud.accounts[i].id) }
                cloud.total_balance = cloud.accounts.filter { !$0.disabled }.reduce(0) { $0 + ($1.balance ?? 0) }
                value = String(decoding: try! JSONEncoder().encode(cloud), as: UTF8.self)
            } else if host == "remove-failure.fixture" {
                var cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
                cloud.accounts[0].disabled = true; cloud.total_balance = 1240
                value = String(decoding: try! JSONEncoder().encode(cloud), as: UTF8.self)
            } else { value = Stage2Fixtures.cloud }
        case "/plan":
            let fields = body()
            var plan = try! JSONDecoder().decode(ComputePlan.self, from: Data((fields["kind"] as? String == "gen" ? Stage2Fixtures.generationPlan : Stage2Fixtures.plan).utf8))
            if ["gen", "eval"].contains(fields["kind"] as? String ?? "") {
                if fields["kind"] as? String == "eval" { plan = try! JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.generationPlan.utf8)) }
                let items = fields["items"] as? Int ?? 1
                let parts: [(String, Int)]
                if let engines = fields["engines"] as? [String] { parts = engines.map { ($0, items) } }
                else if let values = fields["parts"] as? [[String: Any]] { parts = values.map { ($0["engine"] as! String, $0["items"] as! Int) } }
                else { parts = [(fields["engine"] as? String ?? "v2pro", items)] }
                let devices = try! JSONDecoder().decode(ComputeList.self, from: Data(Stage2Fixtures.compute.utf8)).devices
                for i in plan.candidates.indices {
                    let candidate = plan.candidates[i]
                    if let device = devices.first(where: { $0.id == candidate.device }) {
                        let work = parts.reduce(0.0) { $0 + Double($1.1) * (device.speeds[$1.0]?.sec_per_item ?? 0) }
                        let prep = candidate.type == "cloud" ? 360 : 15
                        let queue = candidate.type == "lan" ? 165 : 0
                        plan.candidates[i].seconds = Int(work.rounded()) + prep + queue
                        if candidate.type == "cloud", let gpu = candidate.gpu {
                            let cloud = try! JSONDecoder().decode(CloudInfo.self, from: Data(Stage2Fixtures.cloud.utf8))
                            let rate = cloud.gpus.first { $0.gpu == gpu }!.units_per_hour!
                            plan.candidates[i].units = (max(0.25, Double(plan.candidates[i].seconds!) / 3600 * rate) * 100).rounded() / 100
                        }
                    }
                }
                plan.reason_code = (plan.selected?.seconds ?? 0) <= 120 ? "local_fast_enough" : "fastest"
                if fields["kind"] as? String == "eval" {
                    for i in plan.candidates.indices where plan.candidates[i].type == "cloud" { plan.candidates[i].available = true; plan.candidates[i].reason_code = nil }
                    plan.chosen = "local"; plan.recommended = "local"; plan.reason_code = "fastest"
                }
                if host == "empty-version.fixture" {
                    plan.chosen = nil; plan.recommended = nil; plan.reason_code = "no_device"
                    for i in plan.candidates.indices { plan.candidates[i].available = false; plan.candidates[i].reason_code = plan.candidates[i].type == "cloud" ? "kind_unsupported" : "paused" }
                }
            }
            for i in plan.candidates.indices where plan.candidates[i].type == "cloud" {
                plan.candidates[i].prep_sec = host == "warm.fixture" ? 60 : 360
                if host == "warm.fixture" {
                    plan.candidates[i].available = true; plan.candidates[i].reason_code = nil
                    plan.candidates[i].seconds = max(60, (plan.candidates[i].seconds ?? 360) - 300)
                }
            }
            if host == "warm.fixture" { plan.chosen = "colab-L4"; plan.recommended = "colab-L4"; plan.reason_code = "fastest" }
            if let compute = fields["compute"] as? String { plan.chosen = compute }
            value = String(decoding: try! JSONEncoder().encode(plan), as: UTF8.self)
        case "/system/versions": value = #"{"studio":{"current":"0.1.3","latest":"0.1.3","notes_url":null},"core":{"current":"0.1.3"}}"#
        case "/weights":
            var list = try! JSONDecoder().decode(WeightList.self, from: Data(Stage2Fixtures.weights.utf8))
            if host == "round5.fixture" {
                for i in list.weights.indices {
                    list.weights[i].display_name = nil
                    list.weights[i].created_at = i < 2 ? "2026-11-20T00:25:03+00:00" : "2026-11-20T01:35:12+00:00"
                }
                let base = list.weights[0].evals[0]
                for epoch in [15, 20, 25, 30, 35] {
                    var score = base
                    score.gpt = "run-e\(epoch).ckpt"; score.sovits = "voice_e8_s100.pth"
                    score.score_median = 91 + Double(epoch) / 100
                    list.weights[0].evals.append(score)
                }
            }
            if host == "round7.fixture" {
                let templates = list.weights
                list.weights = []
                for (tag, date, label, score) in [("summer", "2026-07-20", "", 91.1), ("autumn", "2026-11-20", "", 92.2), ("named", "2026-11-20", "我保存的声音", 93.3)] {
                    var gpt = templates[0], sovits = templates[1]
                    gpt.name = "\(tag)-e15.ckpt"; sovits.name = "\(tag)_e8_s100.pth"
                    gpt.id = "v4:" + gpt.name; sovits.id = "v4:" + sovits.name
                    gpt.epoch = 15; sovits.epoch = 8
                    gpt.created_at = date; sovits.created_at = date
                    gpt.display_name = label.isEmpty ? nil : label + " GPT"
                    sovits.display_name = label.isEmpty ? nil : label + " SoVITS"
                    var combo = templates[0].evals[0]
                    combo.gpt = gpt.name; combo.sovits = sovits.name; combo.score_median = score
                    gpt.evals = [combo]; sovits.evals = [combo]
                    gpt.best_eval = combo; sovits.best_eval = combo
                    list.weights += [gpt, sovits]
                }
            }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            if let engine = query.first(where: { $0.name == "engine" })?.value { list.weights.removeAll { $0.engine != engine } }
            if let source = query.first(where: { $0.name == "source" })?.value { list.weights.removeAll { $0.source != source } }
            if let set = query.first(where: { $0.name == "eval_set" })?.value {
                for i in list.weights.indices { list.weights[i].evals.removeAll { $0.eval_set != set }; list.weights[i].best_eval = list.weights[i].evals.first }
            }
            list.total = list.weights.count
            value = String(decoding: try! JSONEncoder().encode(list), as: UTF8.self)
        case "/works": value = #"{"works":[]}"#
        case "/health": value = #"{"status":"ok","time":"","workers":{},"version":"0.1.3","commit":"fixture"}"#
        default: value = "{}"
        }
        respond(200, value)
    }
    private func body() -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(contentsOf: bytes.prefix(n)) }
        }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }
    private func respond(_ status: Int, _ value: String) {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(value.utf8)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}

/// Records only evaluation bodies/statuses; forwards every request to the real development core.
private final class Stage6EvalLog: @unchecked Sendable {
    struct Entry: Sendable { let body: Data; let status: Int }
    private let lock = NSLock()
    private var values: [Entry] = []
    func append(body: Data, status: Int) { lock.lock(); defer { lock.unlock() }; values.append(Entry(body: body, status: status)) }
    var entries: [Entry] { lock.lock(); defer { lock.unlock() }; return values }
}
private final class Stage6LiveProtocol: URLProtocol, @unchecked Sendable {
    static let log = Stage6EvalLog()
    private var forwarding: URLSessionDataTask?
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "127.0.0.1" && request.url?.port == stageAcceptancePort }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var forwarded = request
        if forwarded.httpBody == nil, let stream = forwarded.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data(), bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                data.append(contentsOf: bytes.prefix(count))
            }
            forwarded.httpBodyStream = nil; forwarded.httpBody = data
        }
        let outbound = forwarded
        forwarding = URLSession.shared.dataTask(with: outbound) { [self] data, response, error in
            if let error { client?.urlProtocol(self, didFailWithError: error); return }
            guard let data, let response else { return }
            if outbound.url?.path == "/weights/eval" {
                Self.log.append(body: outbound.httpBody ?? Data(), status: (response as! HTTPURLResponse).statusCode)
            }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        forwarding?.resume()
    }
    override func stopLoading() { forwarding?.cancel() }
}

private let stageAcceptancePort = Int(ProcessInfo.processInfo.environment["TTS_ACCEPTANCE_PORT"] ?? "") ?? 9983

final class Stage2AcceptanceTests: XCTestCase {
    private let output = FileManager.default.temporaryDirectory.appending(path: "tts-studio-screens-\(UUID().uuidString)")
    @MainActor private func store(mock: Bool, host: String = "fixture") -> StudioStore {
        let defaults = UserDefaults(suiteName: "stage2-render-\(UUID().uuidString)")!
        defaults.set(stageAcceptancePort, forKey: "corePort") // the development override is never written to the installed app's domain
        let studio = StudioStore(defaults: defaults)
        studio.connection.configured = true
        if mock {
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stage2Protocol.self]
            studio.api = APIClient(baseURL: "http://\(host)", session: URLSession(configuration: configuration))
        }
        return studio
    }
    @MainActor private func prepareMock(host: String = "fixture") async -> StudioStore {
        let studio = store(mock: true, host: host)
        await studio.loadInitial()
        return studio
    }
    @MainActor private func capture<V: View>(_ name: String, size: CGSize, studio: StudioStore, view: V) async throws {
        let window = NSWindow(contentRect: NSRect(origin: CGPoint(x: 100, y: 100), size: size), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "TTS Studio · Stage 2"
        window.isReleasedWhenClosed = false
        window.contentViewController = NSHostingController(rootView: view.environment(studio).frame(width: size.width, height: size.height).background(Color(nsColor: .windowBackgroundColor)))
        window.makeKeyAndOrderFront(nil)
        defer { window.orderOut(nil); window.contentViewController = nil; window.close() }
        try await Task.sleep(for: .milliseconds(900))
        let content = try XCTUnwrap(window.contentView)
        content.layoutSubtreeIfNeeded()
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let bitmap = try XCTUnwrap(content.bitmapImageRepForCachingDisplay(in: content.bounds))
        content.cacheDisplay(in: content.bounds, to: bitmap)
        try XCTUnwrap(bitmap.representation(using: .png, properties: [:])).write(to: output.appending(path: name + ".png"))
        print("STAGE2_SCREEN:\(name)")
    }
    @MainActor func testDevelopmentCoreAndNativePages() async throws {
        try XCTSkipUnless(["stage2", "stage3", "stage4"].contains(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] ?? ""), "Opt-in development core only")
        let studio = store(mock: false)
        await studio.loadInitial()
        XCTAssertTrue(studio.connected)
        XCTAssertEqual(studio.compute.library?.path, "/tmp/tts-library-test")
        XCTAssertTrue(studio.compute.devices.contains { $0.id == "local" && $0.name == "mini" })
        let cloud = try XCTUnwrap(studio.compute.cloud)
        XCTAssertFalse(cloud.installed); XCTAssertTrue(cloud.accounts.isEmpty)
        XCTAssertNotNil(studio.versions.latest)
        await studio.generationPlanner.refresh(api: studio.api, input: PlanInput(kind: "gen", engine: "v2proplus", items: 6))
        XCTAssertEqual(studio.generationPlanner.plan?.chosen, "local")
        studio.params.engine = "v2proplus"
        try await capture("live-settings-general", size: CGSize(width: 960, height: 700), studio: studio, view: GeneralSettingsView())
        try await capture("live-settings-library", size: CGSize(width: 960, height: 670), studio: studio, view: LibrarySettingsView())
        try await capture("live-settings-compute", size: CGSize(width: 960, height: 670), studio: studio, view: ComputeSettingsView())
        try await capture("live-settings-cloud", size: CGSize(width: 960, height: 760), studio: studio, view: CloudSettingsView())
        try await capture("live-generate", size: CGSize(width: 1280, height: 820), studio: studio, view: ContentView())
        try await capture("live-generation-versions", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
        try await capture("live-evaluation-versions", size: CGSize(width: 1280, height: 850), studio: studio, view: ModelsView())
        studio.connection.role = "client"; studio.connection.alias = "mini"
        try await capture("live-client-compute", size: CGSize(width: 960, height: 670), studio: studio, view: ComputeSettingsView())
        try await capture("live-client-cloud", size: CGSize(width: 960, height: 760), studio: studio, view: CloudSettingsView())
        print("STAGE2_LIVE_CONNECTED:library=tts-library-dev,plan=local,cloud.installed=false,accounts=0,versions=decoded")
    }
    @MainActor func testMockCloudAndConfirmationScreens() async throws {
        try XCTSkipUnless(["stage2", "stage3", "stage4"].contains(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] ?? ""), "Opt-in native render")
        let studio = await prepareMock()
        XCTAssertNil(studio.compute.error); XCTAssertNil(studio.compute.cloudError)
        try await capture("mock-cloud-gpu", size: CGSize(width: 960, height: 760), studio: studio, view: CloudSettingsView())
        try await capture("mock-cloud-accounts", size: CGSize(width: 960, height: 700), studio: studio, view: CloudSettingsView(initialPerspective: "account"))
        let gpu = try XCTUnwrap(studio.compute.cloud?.gpus.first { $0.gpu == "L4" })
        try await capture("mock-gpu-models", size: CGSize(width: 960, height: 560), studio: studio, view: CloudGPURow(gpu: gpu, expanded: true).padding(24))
        let planner = TaskPlanner()
        planner.confirmation = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.plan.utf8))
        planner.confirmationDevice = "colab-L4"; planner.previewEngine = "v4"; planner.previewKind = "train"; planner.taskSummary = "训练 alice · v4（800 句）"
        try await capture("mock-confirmation", size: CGSize(width: 760, height: 540), studio: studio, view: ComputeConfirmation(planner: planner, action: "训练"))
        planner.confirmationDevice = "colab-T4"
        try await capture("mock-confirmation-t4", size: CGSize(width: 760, height: 540), studio: studio, view: ComputeConfirmation(planner: planner, action: "训练"))
        planner.confirmationDevice = "local"
        try await capture("mock-confirmation-free", size: CGSize(width: 760, height: 540), studio: studio, view: ComputeConfirmation(planner: planner, action: "训练"))
        planner.confirmationDevice = "colab-L4"; planner.confirmation?.ask_over = 100; planner.confirmation?.ask_month = 150; planner.confirmation?.month_used = 149; planner.confirmation?.confirm = ["over_month"]
        XCTAssertFalse(planner.confirmation!.reminder.contains("一次")); XCTAssertTrue(planner.confirmation!.reminder.contains("本月"))
        try await capture("mock-confirmation-month", size: CGSize(width: 760, height: 540), studio: studio, view: ComputeConfirmation(planner: planner, action: "训练"))
        var premium = planner.confirmation!.candidates[0]; premium.device = "colab-G4"; premium.gpu = "G4"; premium.name = "云端 RTX PRO 6000"; premium.premium = true; premium.seconds = 1800; premium.units = 5.03
        planner.confirmation?.candidates.append(premium); planner.confirmation?.ask_over = 2; planner.confirmation?.ask_month = 150; planner.confirmation?.confirm = ["premium_gpu", "over_once", "over_month"]; planner.confirmationDevice = premium.device
        try await capture("mock-confirmation-premium", size: CGSize(width: 760, height: 600), studio: studio, view: ComputeConfirmation(planner: planner, action: "训练"))
        let selector = TaskPlanner(); let input = PlanInput(kind: "gen", engine: "v2proplus", items: 6)
        await selector.refresh(api: studio.api, input: input)
        XCTAssertEqual(selector.plan?.candidates.count, 4)
        try await capture("mock-selector", size: CGSize(width: 780, height: 440), studio: studio, view: ComputePicker(planner: selector, input: input, action: "生成", expanded: true).padding(24))
        selector.selected = "lan-dev"; await selector.refresh(api: studio.api, input: input)
        XCTAssertEqual(selector.plan?.chosen, "lan-dev")
        try await capture("mock-selector-once", size: CGSize(width: 900, height: 160), studio: studio, view: ComputePicker(planner: selector, input: input, action: "生成").padding(24))
        selector.selected = nil; await selector.refresh(api: studio.api, input: input)
        try await capture("mock-selector-auto", size: CGSize(width: 900, height: 160), studio: studio, view: ComputePicker(planner: selector, input: input, action: "生成").padding(24))
        let device = studio.compute.devices[0]
        try await capture("mock-device-models", size: CGSize(width: 960, height: 560), studio: studio, view: DeviceRow(device: device, onEdit: {}, expanded: true).padding(24))
        try await capture("mock-device-menu", size: CGSize(width: 320, height: 260), studio: studio, view: VStack(alignment: .leading, spacing: 14) { Text("gpu-worker · ⋯").font(.headline); DeviceMenuActions(device: studio.compute.devices[1], onEdit: {}) }.padding(24))
        studio.jobs = [Job(id: "mock-bench", kind: "bench", target: device.id, status: "running", progress: 0.4, stage: "完整测速中…", created: "")]
        try await capture("mock-benchmark", size: CGSize(width: 960, height: 200), studio: studio, view: DeviceRow(device: device, onEdit: {}).padding(24))
        studio.jobs = []
        var sleeping = studio.compute.devices[1]; sleeping.status = "offline"; sleeping.activity = nil; sleeping.wakeable = true
        try await capture("mock-sleeping-device", size: CGSize(width: 960, height: 200), studio: studio, view: DeviceRow(device: sleeping, onEdit: {}).padding(24))
        try await capture("mock-add-compute", size: CGSize(width: 600, height: 440), studio: studio, view: AddComputeView())
        try await capture("mock-add-device", size: CGSize(width: 580, height: 310), studio: studio, view: DeviceEditor(device: nil))
        try await capture("mock-cloud-login", size: CGSize(width: 580, height: 270), studio: studio, view: CloudLoginView())
        try await capture("mock-cloud-preparing", size: CGSize(width: 580, height: 220), studio: studio, view: VStack(alignment: .leading, spacing: 22) { Text("添加云端 GPU").font(.title2.bold()); CloudLoginProgress(preparing: true) }.padding(28))
        studio.connection.role = "client"; studio.connection.alias = "mini"
        try await capture("mock-client-library", size: CGSize(width: 960, height: 670), studio: studio, view: LibrarySettingsView())
        try await capture("mock-client-compute", size: CGSize(width: 960, height: 670), studio: studio, view: ComputeSettingsView())
        try await capture("mock-client-cloud", size: CGSize(width: 960, height: 760), studio: studio, view: CloudSettingsView())
        try await capture("mock-client-accounts", size: CGSize(width: 960, height: 700), studio: studio, view: CloudSettingsView(initialPerspective: "account"))
        studio.connection.role = "control"
        studio.compute.library = try JSONDecoder().decode(LibraryInfo.self, from: Data(Stage2Fixtures.library.replacingOccurrences(of: "\"selected\":true", with: "\"selected\":false").utf8))
        try await capture("mock-library-empty", size: CGSize(width: 960, height: 420), studio: studio, view: LibrarySettingsView())
        let empty = await prepareMock(host: "empty.fixture")
        try await capture("mock-no-devices", size: CGSize(width: 960, height: 670), studio: empty, view: ComputeSettingsView())
        try await capture("mock-no-accounts", size: CGSize(width: 960, height: 760), studio: empty, view: CloudSettingsView())
        let offline = store(mock: true, host: "offline.fixture")
        try await capture("mock-disconnected", size: CGSize(width: 1280, height: 820), studio: offline, view: ContentView())
        let failure = await prepareMock(host: "failure.fixture")
        XCTAssertEqual(failure.compute.cloudError, "云端 GPU 信息暂时取不到"); XCTAssertEqual(failure.versions.error, "暂时查不到新版本")
        try await capture("mock-cloud-failure", size: CGSize(width: 960, height: 500), studio: failure, view: CloudSettingsView())
        try await capture("mock-version-failure", size: CGSize(width: 960, height: 700), studio: failure, view: GeneralSettingsView())
    }
    @MainActor func testStage3VersionsAndAccountScreens() async throws {
        try XCTSkipUnless(["stage3", "stage4"].contains(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] ?? ""), "Opt-in stage3 render")
        let studio = await prepareMock()
        try await studio.loadComposerVersions(); try await studio.loadRefs(character: studio.params.character)
        XCTAssertEqual(studio.params.version, studio.composerVersions.selectedID)
        try await capture("mock-generate-version", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
        try await capture("mock-version-more", size: CGSize(width: 800, height: 1080), studio: studio, view: VStack(alignment: .leading, spacing: 16) { Text("更多设置").font(.title2.bold()); ComposerAdvancedView(compareEngines: .constant(["v2pro"])) }.padding(24))
        try await capture("mock-evaluation-versions", size: CGSize(width: 1280, height: 960), studio: studio, view: ModelsView())
        try await capture("mock-version-list", size: CGSize(width: 700, height: 250), studio: studio, view: ModelVersionPicker(rows: studio.composerVersions.rows, selection: .constant(studio.composerVersions.selectedID)).padding(24))
        try await capture("mock-blind-versions", size: CGSize(width: 850, height: 790), studio: studio, view: BlindCreateView(manager: studio.blindManager))
        try await studio.composerVersions.invalidate(api: studio.api, character: studio.params.character)
        studio.params.version = nil
        try await capture("mock-version-unavailable", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
        studio.selectComposerVersion(studio.composerVersions.rows[0].id)
        try await capture("mock-mixed-plan", size: CGSize(width: 960, height: 420), studio: studio, view: ComputePicker(planner: TaskPlanner(), input: PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 36), action: "出题", expanded: true).padding(24))
        let empty = await prepareMock(host: "empty-version.fixture")
        try await empty.loadComposerVersions(); try await empty.loadRefs(character: empty.params.character)
        XCTAssertTrue(empty.composerVersions.rows.isEmpty)
        try await capture("mock-zero-shot-empty", size: CGSize(width: 1280, height: 820), studio: empty, view: GenerateView())
        let zero = ModelVersion(id: "v2pro/base-g/base-s", label: "底模（零样本）", engine: "v2pro", gpt: "base-g", sovits: "base-s", score: nil, score_set: nil, recommended: true, source_run: nil, created: "")
        try await capture("mock-zero-shot-version", size: CGSize(width: 700, height: 260), studio: studio, view: ModelVersionPicker(rows: [zero], selection: .constant(zero.id)).padding(24))
        try await capture("mock-local-menu", size: CGSize(width: 320, height: 240), studio: studio, view: VStack(alignment: .leading, spacing: 14) { Text("mini · ⋯").font(.headline); DeviceMenuActions(device: studio.compute.devices[0], onEdit: {}) }.padding(24))
        try await capture("mock-repair-device", size: CGSize(width: 600, height: 360), studio: studio, view: DeviceEditor(device: studio.compute.devices[1], rePair: true))
        let removal = await prepareMock(host: "remove-failure.fixture")
        try await removal.compute.removeAccount("a")
        try await capture("mock-account-remove-failed", size: CGSize(width: 960, height: 800), studio: removal, view: CloudSettingsView(initialPerspective: "account"))
        studio.compute.systemInfo?.web_available = true
        try await capture("mock-browser-link", size: CGSize(width: 960, height: 700), studio: studio, view: GeneralSettingsView())
    }
    @MainActor func testStage3RealPairingAndVersions() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "stage3", "Opt-in real development worker")
        let studio = store(mock: false)
        await studio.loadInitial()
        try await studio.loadComposerVersions(); try await studio.loadRefs(character: studio.params.character)
        XCTAssertFalse(studio.composerVersions.rows.isEmpty)
        XCTAssertNotNil(studio.compute.systemInfo)
        XCTAssertFalse(studio.compute.systemInfo!.web_available)
        XCTAssertTrue(studio.compute.systemInfo!.web_url.host!.hasSuffix(".local"))
        XCTAssertTrue(studio.refs.contains { $0.label.hasPrefix("默认参考音频") })
        let mixed: ComputePlan = try await studio.api.send("/plan", body: PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 1))
        XCTAssertNotNil(mixed.selected?.seconds)
        print("STAGE3_MIXED_PLAN: engines=2; selected=\(mixed.chosen ?? "none"); seconds=\(mixed.selected?.seconds ?? 0)")
        let log = try String(contentsOfFile: "/private/tmp/tts-stage3-worker-code.log", encoding: .utf8)
        let range = try XCTUnwrap(log.range(of: #"[2-9A-HJKMNP-Z]{3}-[2-9A-HJKMNP-Z]{3}"#, options: .regularExpression))
        let code = String(log[range])
        let added = try await studio.compute.addDevice(name: "stage3-worker", address: "http://127.0.0.1:9981", pairCode: code)
        XCTAssertEqual(added.type, "lan"); XCTAssertEqual(added.status, "idle")
        let local = try XCTUnwrap(studio.compute.devices.first { $0.id == "local" })
        do {
            try await studio.compute.patch("local", fields: ["name":.string("mini · Stage3")])
            XCTAssertEqual(studio.compute.devices.first { $0.id == "local" }?.name, "mini · Stage3")
            try await capture("live-local-renamed", size: CGSize(width: 960, height: 700), studio: studio, view: ComputeSettingsView())
            try await capture("live-device-paired", size: CGSize(width: 960, height: 700), studio: studio, view: DeviceRow(device: added, onEdit: {}).padding(24))
            try await capture("live-generation-versions", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
            try await studio.compute.patch("local", fields: ["name":.string(local.name == studio.compute.systemInfo?.machine ? "" : local.name)])
            try await studio.compute.remove(added.id)
        } catch {
            try await studio.compute.patch("local", fields: ["name":.string(local.name == studio.compute.systemInfo?.machine ? "" : local.name)])
            try await studio.compute.remove(added.id)
            throw error
        }
        print("STAGE3_REAL_PAIRED:type=lan,status=idle; local rename restored; test device removed; version list/ref labels/system-info verified")
    }

    @MainActor func testStage4ClosingScreens() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "stage4", "Opt-in final content renders")
        let studio = await prepareMock(host: "account-state.fixture")
        try await studio.compute.enableAccount("a")
        let first = try XCTUnwrap(studio.compute.cloud?.accounts.first { $0.id == "a" })
        try await capture("mock-account-disable-menu", size: CGSize(width: 450, height: 230), studio: studio, view: VStack(alignment: .leading, spacing: 16) { Text(first.displayName).font(.headline); CloudAccountActions(account: first) }.padding(24))
        try await studio.compute.disableAccount("a")
        let disabled = try XCTUnwrap(studio.compute.cloud?.accounts.first { $0.id == "a" })
        XCTAssertTrue(disabled.disabled)
        try await capture("mock-account-enable-menu", size: CGSize(width: 450, height: 230), studio: studio, view: VStack(alignment: .leading, spacing: 16) { Text(disabled.displayName).font(.headline); CloudAccountActions(account: disabled) }.padding(24))
        try await studio.compute.enableAccount("a")
        XCTAssertFalse(studio.compute.cloud!.accounts.first { $0.id == "a" }!.disabled)
        try await capture("mock-account-enabled", size: CGSize(width: 960, height: 730), studio: studio, view: CloudSettingsView(initialPerspective: "account"))
        let expired = await prepareMock(host: "pair-expired.fixture")
        var pairError: String?
        do { _ = try await expired.compute.addDevice(name: "书房电脑", address: "http://study.local:9880", pairCode: "ABC-DEF") }
        catch { pairError = error.localizedDescription }
        XCTAssertTrue(pairError?.contains("过期") == true)
        try await capture("mock-pair-expired", size: CGSize(width: 600, height: 370), studio: expired, view: DeviceEditor(device: nil, failure: pairError))
        let failed = await prepareMock(host: "pair-failed.fixture")
        do { try await failed.compute.repairDevice("lan-dev", address: "http://study.local:9880", pairCode: "ABC-DEF", fields: ["name":.string("书房电脑")]) }
        catch { pairError = error.localizedDescription }
        try await capture("mock-pair-failed", size: CGSize(width: 600, height: 410), studio: failed, view: DeviceEditor(device: failed.compute.devices[1], failure: pairError))
        try await studio.loadComposerVersions()
        try await capture("mock-model-version-options", size: CGSize(width: 620, height: 340), studio: studio, view: Form { ModelVersionOptions(rows: studio.composerVersions.rows) }.formStyle(.grouped).padding(20))
        studio.candidates = [AudioItem(id: "mock-result", created: "2026-11-20", audio_url: "/audio/mock.wav", text: "おはよう。今日はいい天気だね。", character: "alice", engine: "v4", gpt: "alice-g.ckpt", sovits: "alice-s.pth", ref_id: "r-default", seed: 42, duration: 5.8, infer_sec: 20, synth_wall_sec: 21, semantic_tokens: 120, vram_peak_mb: 0, ratio_vs_ref: [], asr_text: "おはよう。今日はいい天気だね。", cer: 0.04, spk_sim: 0.91, score: 94.2)]
        studio.selectedCandidate = "mock-result"
        try await capture("mock-generated-candidates", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
        let empty = await prepareMock(host: "empty.fixture")
        try await capture("mock-cloud-empty-preferences", size: CGSize(width: 960, height: 450), studio: empty, view: ScrollViewReader { proxy in CloudSettingsView().task { try? await Task.sleep(for: .milliseconds(300)); proxy.scrollTo("cloud-preferences", anchor: .bottom) } })
        empty.models = []
        try await capture("mock-blind-empty-library", size: CGSize(width: 850, height: 790), studio: empty, view: BlindCreateView(manager: empty.blindManager))
        let withoutAlice = await prepareMock()
        withoutAlice.models[0].slug = "bob"
        try await capture("mock-blind-first-character", size: CGSize(width: 850, height: 790), studio: withoutAlice, view: BlindCreateView(manager: withoutAlice.blindManager))
        studio.composerWeights = []
        try await capture("mock-hidden-version-more", size: CGSize(width: 800, height: 1050), studio: studio, view: ComposerAdvancedView(compareEngines: .constant([])).padding(24))
    }

    @MainActor func testStage5AffectedScreens() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "stage5", "Opt-in round5 render")
        let studio = await prepareMock(host: "round5.fixture")
        studio.weightManager.engine = "v4"
        try await studio.weightManager.refreshVersions(studio: studio)
        try await studio.weightManager.load()
        try await capture("mock-evaluation-versions", size: CGSize(width: 1280, height: 960), studio: studio, view: ModelsView())
        let combos = studio.weightManager.comparisons.map { ComboEval(gpt: $0.gpt, sovits: $0.sovits, n: $0.n, cer_median: $0.cer_median, spk_sim_median: $0.spk_sim_median, score_median: $0.score_median) }
        let evaluation = TrainEval(job_id: "round5", engine: "v4", character: "bob", ref_id: "r", eval_set: "hard", sentences: [], candidates_per_sentence: 3, combos: combos, best: combos[0])
        try await capture("mock-training-evaluation-combinations", size: CGSize(width: 1050, height: 410), studio: studio, view: EvaluationView(evaluation: evaluation).padding(24))
        studio.candidates = [AudioItem(id: "mock-result", created: "2026-11-20", audio_url: "/audio/mock.wav", text: "おはよう。今日はいい天気だね。", character: "alice", engine: "v4", gpt: "alice-g.ckpt", sovits: "alice-s.pth", ref_id: "r-default", seed: 42, duration: 5.8, infer_sec: 20, synth_wall_sec: 21, semantic_tokens: 120, vram_peak_mb: 0, ratio_vs_ref: [], asr_text: "おはよう。今日はいい天気だね。", cer: 0.04, spk_sim: 0.91, score: 94.2)]
        studio.selectedCandidate = "mock-result"
        try await capture("mock-generated-candidates", size: CGSize(width: 1280, height: 820), studio: studio, view: GenerateView())
        try await capture("mock-version-list", size: CGSize(width: 700, height: 250), studio: studio, view: ModelVersionPicker(rows: studio.composerVersions.rows, selection: .constant(studio.composerVersions.selectedID)).padding(24))
        try await capture("mock-model-version-options", size: CGSize(width: 620, height: 340), studio: studio, view: Form { ModelVersionOptions(rows: studio.composerVersions.rows) }.formStyle(.grouped).padding(20))
        let empty = await prepareMock(host: "empty-version.fixture")
        try await capture("mock-zero-shot-empty", size: CGSize(width: 1280, height: 820), studio: empty, view: GenerateView())
        let live = store(mock: false)
        await live.loadInitial()
        XCTAssertTrue(live.connected)
        try await capture("live-evaluation-versions", size: CGSize(width: 1280, height: 850), studio: live, view: ModelsView())
    }

    @MainActor func testStage6RealVersionEvaluationAndMissingWeight() async throws {
        try XCTSkipUnless(["stage6", "stage7"].contains(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] ?? ""), "Explicit local development version-evaluation smoke")
        let studio = store(mock: false)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Stage6LiveProtocol.self]
        config.timeoutIntervalForResource = 600
        studio.api = APIClient(baseURL: "http://127.0.0.1:\(stageAcceptancePort)", session: URLSession(configuration: config))
        await studio.loadInitial()
        let library = try XCTUnwrap(studio.compute.library)
        XCTAssertEqual(library.path, "/tmp/tts-library-test")
        XCTAssertTrue(library.owned)
        XCTAssertTrue(studio.connected)
        let health: Health = try await studio.api.getWithoutRetry("/health")
        XCTAssertNil(health.training_job)
        let manager = studio.weightManager
        manager.character = "alice"; manager.engine = "v2pro"
        manager.pairCharacter = "alice"; manager.evalSet = "mini"; manager.evaluationSet = "mini"
        try await manager.refreshVersions(studio: studio)
        let version = try XCTUnwrap(manager.versions.rows.first { $0.engine == "v2pro" })
        manager.selectVersion(version.id)
        try await manager.load()
        try await manager.loadPair(studio: studio)
        let sets: EvalSetList = try await studio.api.get("/eval-sets")
        let smallest = try XCTUnwrap(sets.sets.min { $0.sentences.count * $0.candidates < $1.sentences.count * $1.candidates })
        XCTAssertEqual(smallest.name, "mini")
        manager.evaluationItems = smallest.sentences.count * smallest.candidates
        XCTAssertEqual(manager.evaluationItems, 9)
        manager.planner.selected = "local"
        await manager.planner.refresh(api: studio.api, input: PlanInput(kind: "eval", engine: version.engine, items: manager.evaluationItems))
        XCTAssertEqual(manager.planner.plan?.chosen, "local")
        studio.characterTab = .versions
        studio.page = .characters
        try await capture("live-version-eval-ready", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
        print("STAGE6_BEFORE:version=\(version.id),character=alice,score=\(version.score.map(String.init(describing:)) ?? "nil"),set=\(version.score_set ?? "nil"),items=\(manager.evaluationItems),compute=local")
        let start = Date()
        manager.evaluationTask = studio.perform { try await manager.evaluate(studio: studio) }
        var runningCaptured = false
        for _ in 0..<5400 {
            try await Task.sleep(for: .milliseconds(100))
            if manager.evaluationJobID != nil, !runningCaptured {
                runningCaptured = true
                try await capture("live-version-eval-running", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
            }
            if runningCaptured, !manager.evaluationBusy { break }
            if studio.errorMessage != nil, !manager.evaluationBusy { break }
            if Date().timeIntervalSince(start) > 540 {
                manager.evaluationTask?.cancel()
                if let id = manager.evaluationJobID, let job = studio.jobs.first(where: { $0.id == id }) { try await studio.cancel(job) }
                XCTFail("Local evaluation exceeded nine-minute budget")
                return
            }
        }
        await manager.evaluationTask?.value
        XCTAssertNil(studio.errorMessage, studio.errorMessage ?? "")
        let jobID = try XCTUnwrap(manager.evaluationJobID)
        let job: Job = try await studio.api.get("/jobs/\(jobID)")
        XCTAssertEqual(job.status, "succeeded", job.error ?? "")
        XCTAssertEqual(job.compute?.device, "local")
        let result = try XCTUnwrap(job.eval_result)
        let combo = try XCTUnwrap(result.combos.first)
        XCTAssertEqual(result.character, "alice")
        XCTAssertEqual(result.eval_set, "mini")
        XCTAssertEqual(combo.gpt, version.gpt); XCTAssertEqual(combo.sovits, version.sovits)
        let refreshed = try XCTUnwrap(manager.versions.rows.first { $0.id == version.id })
        XCTAssertEqual(refreshed.score_set, "mini")
        XCTAssertEqual(refreshed.score, combo.score_median)
        XCTAssertTrue(manager.comparisons.contains { $0.gpt == version.gpt && $0.sovits == version.sovits && $0.score_median == combo.score_median })
        let label = modelVersionLabel(gpt: combo.gpt, sovits: combo.sovits, versions: manager.versions.rows, weights: manager.pairRows)
        XCTAssertEqual(label, "\(refreshed.label) · \(displayEngine(refreshed.engine))")
        let first = try XCTUnwrap(Stage6LiveProtocol.log.entries.last)
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: first.body) as? [String: Any])
        XCTAssertEqual(first.status, 200)
        XCTAssertEqual(sent["version"] as? String, version.id)
        XCTAssertEqual(sent["character"] as? String, "alice")
        XCTAssertEqual(sent["compute"] as? String, "local")
        XCTAssertEqual(sent["eval_set"] as? String, "mini")
        print("STAGE6_SUCCESS:job=\(jobID),seconds=\(Int(Date().timeIntervalSince(start))),label=\(label),score=\(combo.score_median),set=\(refreshed.score_set ?? ""),body=\(String(decoding: first.body, as: UTF8.self))")
        try await capture("live-version-eval-completed", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())

        // Move the development-library directory entry only; never modify a symlink target.
        let weights: WeightList = try await studio.api.get("/weights", query: ["character":"alice", "engine":version.engine])
        let weight = try XCTUnwrap(weights.weights.first { $0.name == version.gpt && $0.type == "gpt" })
        let original = URL(filePath: weight.path).standardizedFileURL
        XCTAssertTrue(original.path.hasPrefix("/tmp/tts-library-test/weights/"))
        guard original.path.hasPrefix("/tmp/tts-library-test/weights/") else { return }
        let hidden = original.deletingLastPathComponent().appending(path: original.lastPathComponent + ".stage6-hidden")
        let fm = FileManager.default
        XCTAssertFalse(fm.fileExists(atPath: hidden.path))
        let originalAttributes = try fm.attributesOfItem(atPath: original.path)
        let manifest = URL(filePath: "/private/tmp/tts-stage6-weight-restore.json")
        let manifestData = try JSONSerialization.data(withJSONObject: ["original":original.path,"hidden":hidden.path], options: [.sortedKeys])
        try manifestData.write(to: manifest, options: [.atomic])
        try fm.moveItem(at: original, to: hidden)
        var needsRestore = true
        defer {
            if needsRestore {
                do { try fm.moveItem(at: hidden, to: original) }
                catch { XCTFail("RESTORE REQUIRED: \(hidden.path) → \(original.path): \(error)") }
            }
        }
        let jobsBefore: JobList = try await studio.api.get("/jobs", query: ["limit":"500"])
        let priorCalls = Stage6LiveProtocol.log.entries.count
        studio.errorMessage = nil
        manager.selectVersion(version.id); manager.planner.selected = "local"
        let failed = studio.perform { try await manager.evaluate(studio: studio) }
        await failed.value
        XCTAssertTrue(studio.errorMessage?.contains("重新选择") == true)
        XCTAssertNil(manager.versions.selectedID)
        XCTAssertNil(manager.evaluationJobID)
        XCTAssertFalse(manager.evaluationBusy)
        XCTAssertEqual(Stage6LiveProtocol.log.entries.count, priorCalls + 1)
        XCTAssertEqual(Stage6LiveProtocol.log.entries.last?.status, 409)
        let jobsAfter: JobList = try await studio.api.get("/jobs", query: ["limit":"500"])
        XCTAssertEqual(Set(jobsBefore.jobs.map(\.id)), Set(jobsAfter.jobs.map(\.id)))
        print("STAGE6_BINARY_MISSING:status=\(Stage6LiveProtocol.log.entries.last?.status ?? 0),prompt=\(studio.errorMessage ?? ""),selected=\(manager.versions.selectedID ?? "nil"),new_jobs=\(Set(jobsAfter.jobs.map(\.id)).subtracting(Set(jobsBefore.jobs.map(\.id))).count)")
        try await capture("live-version-eval-binary-missing-rejected", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
        try fm.moveItem(at: hidden, to: original)
        needsRestore = false
        let restoredAttributes = try fm.attributesOfItem(atPath: original.path)
        XCTAssertEqual(originalAttributes[.systemFileNumber] as? NSNumber, restoredAttributes[.systemFileNumber] as? NSNumber)
        XCTAssertEqual(originalAttributes[.size] as? NSNumber, restoredAttributes[.size] as? NSNumber)
        try fm.removeItem(at: manifest)
        try await manager.refreshVersions(studio: studio)
        manager.selectVersion(version.id)
        XCTAssertNotNil(manager.versions.selected)
        studio.errorMessage = nil
        try await capture("live-version-eval-restored", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
        print("STAGE6_RESTORED:path=\(original.path),same_inode=true,same_size=true,version_available=true")
    }

    @MainActor func testStage6RemovedRecordRequiresReselection() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "stage6-record", "Explicit real 409 from removed development weight record")
        let studio = store(mock: false)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Stage6LiveProtocol.self]
        studio.api = APIClient(baseURL: "http://127.0.0.1:\(stageAcceptancePort)", session: URLSession(configuration: config))
        await studio.loadInitial()
        XCTAssertEqual(studio.compute.library?.path, "/tmp/tts-library-test")
        let manager = studio.weightManager
        manager.character = "alice"; manager.engine = "v2pro"
        manager.pairCharacter = "alice"; manager.evalSet = "mini"; manager.evaluationSet = "mini"
        try await manager.refreshVersions(studio: studio)
        let version = try XCTUnwrap(manager.versions.rows.first { $0.engine == "v2pro" })
        manager.selectVersion(version.id)
        try await manager.load(); try await manager.loadPair(studio: studio)
        let weight = try XCTUnwrap(manager.pairRows.first { $0.name == version.gpt && $0.type == "gpt" })
        let original = URL(filePath: weight.path).deletingLastPathComponent().appending(path: "meta.json").standardizedFileURL
        guard original.path.hasPrefix("/tmp/tts-library-test/weights/") else { XCTFail("Development-only path required"); return }
        let hidden = original.appendingPathExtension("stage6-hidden")
        let fm = FileManager.default
        XCTAssertFalse(fm.fileExists(atPath: hidden.path))
        let attributes = try fm.attributesOfItem(atPath: original.path)
        let manifest = URL(filePath: "/private/tmp/tts-stage6-record-restore.json")
        try JSONSerialization.data(withJSONObject: ["original":original.path,"hidden":hidden.path]).write(to: manifest, options: [.atomic])
        try fm.moveItem(at: original, to: hidden)
        var needsRestore = true
        defer {
            if needsRestore {
                do { try fm.moveItem(at: hidden, to: original) }
                catch { XCTFail("RESTORE REQUIRED: \(hidden.path): \(error)") }
            }
        }
        let _: LibraryInfo = try await studio.api.post("/library/rescan")
        let before: JobList = try await studio.api.get("/jobs", query: ["limit":"500"])
        let calls = Stage6LiveProtocol.log.entries.count
        manager.planner.selected = "local"
        let operation = studio.perform { try await manager.evaluate(studio: studio) }
        await operation.value
        let rejected = try XCTUnwrap(Stage6LiveProtocol.log.entries.last)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: rejected.body) as? [String: Any])
        XCTAssertEqual(body["version"] as? String, version.id)
        XCTAssertEqual(body["character"] as? String, "alice")
        XCTAssertEqual(body["compute"] as? String, "local")
        XCTAssertEqual(rejected.status, 409)
        XCTAssertEqual(Stage6LiveProtocol.log.entries.count, calls + 1)
        XCTAssertTrue(studio.errorMessage?.contains("重新选择") == true)
        XCTAssertNil(manager.versions.selectedID); XCTAssertNil(manager.evaluationJobID)
        XCTAssertFalse(manager.evaluationBusy)
        let after: JobList = try await studio.api.get("/jobs", query: ["limit":"500"])
        XCTAssertEqual(Set(before.jobs.map(\.id)), Set(after.jobs.map(\.id)))
        studio.characterTab = .versions
        studio.page = .characters
        try await capture("live-version-eval-unavailable", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
        print("STAGE6_RECORD_409:status=\(rejected.status),prompt=\(studio.errorMessage ?? ""),selected=\(manager.versions.selectedID ?? "nil"),new_jobs=\(Set(after.jobs.map(\.id)).subtracting(Set(before.jobs.map(\.id))).count),eval_posts=\(Stage6LiveProtocol.log.entries.count - calls)")
        try fm.moveItem(at: hidden, to: original)
        needsRestore = false
        XCTAssertEqual(attributes[.systemFileNumber] as? NSNumber, try fm.attributesOfItem(atPath: original.path)[.systemFileNumber] as? NSNumber)
        try fm.removeItem(at: manifest)
        let _: LibraryInfo = try await studio.api.post("/library/rescan")
        try await manager.refreshVersions(studio: studio)
        manager.selectVersion(version.id)
        XCTAssertNotNil(manager.versions.selected)
        studio.errorMessage = nil
        try await capture("live-version-eval-restored", size: CGSize(width: 1450, height: 900), studio: studio, view: ContentView())
        print("STAGE6_RECORD_RESTORED:same_inode=true,version_available=true,path=\(original.path)")
    }

    @MainActor func testStage7ClosingScreens() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "stage7", "Opt-in round7 content verification")
        let studio = await prepareMock(host: "round7.fixture")
        studio.weightManager.engine = "v4"
        try await studio.weightManager.refreshVersions(studio: studio)
        try await studio.weightManager.load()
        try await capture("mock-evaluation-training-dates", size: CGSize(width: 1450, height: 960), studio: studio, view: ModelsView())
        let historical = store(mock: true, host: "evaluation-failure.fixture")
        let combo = ComboEval(gpt: "old-e15.ckpt", sovits: "old_e8_s100.pth", n: 3, cer_median: 0, spk_sim_median: 0.9, score_median: 96.5)
        let evaluation = TrainEval(job_id: "old", engine: "v2pro", character: "deleted-character", ref_id: "r", eval_set: "mini", sentences: [], candidates_per_sentence: 3, combos: [combo], best: combo)
        try await capture("mock-training-evaluation-label-failure", size: CGSize(width: 1050, height: 340), studio: historical, view: EvaluationView(evaluation: evaluation).padding(24))
        XCTAssertNil(historical.errorMessage, "metadata 404 must not become a global error")
        let blind = await prepareMock(host: "blind-unavailable.fixture")
        blind.blindManager.select("expired")
        try await blind.blindManager.refresh()
        let operation = blind.perform { try await blind.blindManager.regenerate() }
        await operation.value
        XCTAssertTrue(blind.blindManager.regenerationNeedsVersion)
        XCTAssertTrue(blind.errorMessage?.contains("重新选择版本") == true)
        blind.characterTab = .blind
        blind.page = .characters
        try await capture("mock-blind-regenerate-unavailable", size: CGSize(width: 1450, height: 850), studio: blind, view: ContentView())
    }

}

extension Stage2AcceptanceTests {
    @MainActor func testStage3ComputeWizardScreens() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "compute-wizard", "Opt-in stage 3 mock screens")
        let studio = await prepareMock()
        try await capture("mock-add-type", size: CGSize(width: 570, height: 390), studio: studio, view: AddComputeView())
        func flow(_ step: Int) throws -> DeviceInviteFlow {
            let value = DeviceInviteFlow(compute: studio.compute)
            value.step = step
            value.invite = try JSONDecoder().decode(ComputeInvite.self, from: Data(#"{"id":"render","code":"7Q4-K9M","expires_in":600,"expires_at":"2099-01-01T00:00:00Z","host":"studio-host.local","commands":{"unix":"curl -fsSL 'http://studio-host.local/install' | sh -s -- --pair 7Q4-K9M","windows":null}}"#.utf8))
            value.deadline = Date().addingTimeInterval(480)
            return value
        }
        try await capture("mock-lan-network", size: CGSize(width: 660, height: 370), studio: studio, view: AddComputerView(flow: DeviceInviteFlow(compute: studio.compute), polling: false))
        let command = try flow(2)
        try await capture("mock-lan-command", size: CGSize(width: 660, height: 520), studio: studio, view: AddComputerView(flow: command, polling: false))
        try await capture("mock-lan-windows", size: CGSize(width: 660, height: 490), studio: studio, view: AddComputerView(flow: try flow(2), polling: false, os: "windows"))
        try await capture("mock-lan-copy-failed", size: CGSize(width: 660, height: 550), studio: studio, view: AddComputerView(flow: try flow(2), polling: false, copyError: "复制失败，请选中命令后手动复制。"))
        try await capture("mock-lan-waiting", size: CGSize(width: 660, height: 360), studio: studio, view: AddComputerView(flow: try flow(3), polling: false))
        let installing = try flow(3); installing.apply(ComputeInviteStatus(id: "render", state: "waiting", expires_in: 0, redeemed: true))
        try await capture("mock-lan-installing", size: CGSize(width: 660, height: 400), studio: studio, view: AddComputerView(flow: installing, polling: false))
        let expired = try flow(3); expired.expired = true
        try await capture("mock-lan-expired", size: CGSize(width: 660, height: 350), studio: studio, view: AddComputerView(flow: expired, polling: false))
        let manual = try flow(3); manual.manualError = "连接失败，请检查连接地址后重试。"; manual.manualAddress = "http://study.local:9880"
        try await capture("mock-lan-manual-failed", size: CGSize(width: 660, height: 510), studio: studio, view: AddComputerView(flow: manual, polling: false, manual: true))
        let connected = DeviceInviteFlow(compute: studio.compute)
        let device = try JSONDecoder().decode(ComputeDevice.self, from: Data(#"{"id":"lan-new","name":"worker-host","type":"lan","status":"idle","engines":["v2pro","v2proplus","v4"],"speeds":{},"hw":{"os":"linux","accel":"cpu","cores":4}}"#.utf8))
        connected.connected(device)
        try await capture("mock-lan-connected", size: CGSize(width: 660, height: 550), studio: studio, view: AddComputerView(flow: connected, polling: false))
        try await capture("mock-cloud-login", size: CGSize(width: 580, height: 390), studio: studio, view: CloudLoginView())
        let account = try XCTUnwrap(studio.compute.cloud?.accounts.first)
        try await capture("mock-cloud-recognized", size: CGSize(width: 580, height: 480), studio: studio, view: CloudLoginView(account: account, step: 2))
        try await capture("mock-cloud-completed", size: CGSize(width: 580, height: 330), studio: studio, view: CloudLoginView(account: account, step: 3))
        let input = PlanInput(kind: "gen", engine: "v4", items: 2)
        try await capture("mock-selector-task-queue", size: CGSize(width: 900, height: 490), studio: studio, view: ComputePicker(planner: TaskPlanner(), input: input, action: "生成", expanded: true).padding(24))
        try await capture("mock-selector-training-context", size: CGSize(width: 900, height: 560), studio: studio, view: ComputePicker(planner: TaskPlanner(), input: PlanInput(kind: "train", engine: "v4", lines: 800), action: "训练", taskTitle: "alice", expanded: true).padding(24))
        try await capture("mock-selector-blind-context", size: CGSize(width: 900, height: 490), studio: studio, view: ComputePicker(planner: TaskPlanner(), input: PlanInput(kind: "gen", engines: ["v2pro", "v4"], items: 6), action: "出题", taskTitle: "盲听 dave", expanded: true).padding(24))
        let preview = TaskPlanner()
        await preview.refresh(api: studio.api, input: PlanInput(kind: "train", engine: "v4", lines: 800))
        preview.confirmation = preview.plan; preview.confirmationDevice = preview.plan?.chosen
        preview.taskSummary = "训练（" + computeTaskContent(PlanInput(kind: "train", engine: "v4", lines: 800), title: "alice") + "）"
        try await capture("mock-confirmation-training-context", size: CGSize(width: 780, height: 650), studio: studio, view: ComputeConfirmation(planner: preview, action: "训练"))
        try await capture("mock-cloud-checking", size: CGSize(width: 580, height: 390), studio: studio, view: CloudLoginView(checking: true))
        try await capture("mock-cloud-logged-in", size: CGSize(width: 580, height: 330), studio: studio, view: CloudLoginView(account: account, step: 1))
        try await capture("mock-settings-speed-legend", size: CGSize(width: 960, height: 800), studio: studio, view: ComputeSettingsView())
        let warm = await prepareMock(host: "warm.fixture")
        try await capture("mock-selector-cloud-preparation", size: CGSize(width: 900, height: 490), studio: warm, view: ComputePicker(planner: TaskPlanner(), input: input, action: "生成", expanded: true).padding(24))
    }
}

extension Stage2AcceptanceTests {
    @MainActor func testStage4ReviewScreens() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "compute-round4", "Opt-in round4 mock renders")
        let studio = await prepareMock(host: "r4.fixture")
        let device = try XCTUnwrap(studio.compute.devices.first { $0.type == "lan" })
        try await capture("mock-r4-device-speeds", size: CGSize(width: 960, height: 600), studio: studio, view: DeviceRow(device: device, onEdit: {}, expanded: true).padding(24))
        try await capture("mock-r4-cloud-gpus", size: CGSize(width: 960, height: 800), studio: studio, view: CloudSettingsView())
        try await capture("mock-r4-gpu-speeds", size: CGSize(width: 960, height: 620), studio: studio, view: CloudGPURow(gpu: try XCTUnwrap(studio.compute.cloud?.gpus.first), expanded: true).padding(24))
        try await capture("mock-r4-device-table", size: CGSize(width: 960, height: 800), studio: studio, view: ComputeSettingsView())
        let originalDevices = studio.compute.devices
        let offline = DeviceInviteFlow(compute: studio.compute)
        var paired = device; paired.status = "offline"
        offline.connected(paired)
        try await capture("mock-r4-paired-offline", size: CGSize(width: 660, height: 600), studio: studio, view: AddComputerView(flow: offline, polling: false))
        studio.compute.devices = originalDevices
        let item = AudioItem(id: "r4-debug", created: "", audio_url: "/audio/r4", text: "台词", character: "alice", engine: "v2pro", gpt: "g", sovits: "s", ref_id: "r", seed: 1, duration: 1, infer_sec: 1, synth_wall_sec: 1, semantic_tokens: 25, vram_peak_mb: 0, ratio_vs_ref: [])
        try await capture("mock-r4-debug-empty", size: CGSize(width: 940, height: 800), studio: studio, view: DebugView(item: item))
        let manager = studio.blindManager
        manager.select("r4-blind"); try await manager.refresh()
        manager.regenerationPlanner.selected = device.id
        try await capture("mock-r4-blind-regeneration-selector", size: CGSize(width: 920, height: 380), studio: studio, view: BlindRegenerationControls(manager: manager, expanded: true).padding(24))
        manager.regenerationPlanner.confirmation = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.plan.utf8))
        manager.regenerationPlanner.confirmationDevice = "colab-L4"; manager.regenerationPlanner.taskSummary = "补生成（alice · 还有 2 题）"
        try await capture("mock-r4-blind-confirmation", size: CGSize(width: 780, height: 650), studio: studio, view: ComputeConfirmation(planner: manager.regenerationPlanner, action: "补生成", showSpeed: false))
        manager.select(nil)
        try await capture("mock-r4-blind-readable-title", size: CGSize(width: 1100, height: 750), studio: studio, view: BlindView())
        studio.work = Work(id: "r4-work", title: "试音对话", created: "", updated: "", lines: [WorkLine(id: "line", text: "おはよう。今日はいい天気だね。")])
        try await capture("mock-r4-work-selector", size: CGSize(width: 1450, height: 900), studio: studio, view: WorksView())
        studio.workPlanner.confirmation = try JSONDecoder().decode(ComputePlan.self, from: Data(Stage2Fixtures.plan.utf8))
        studio.workPlanner.confirmationDevice = "colab-L4"; studio.workPlanner.taskSummary = "生成作品（试音对话 · 3 句 v2Pro）"
        try await capture("mock-r4-work-confirmation", size: CGSize(width: 780, height: 650), studio: studio, view: ComputeConfirmation(planner: studio.workPlanner, action: "生成"))
    }
    @MainActor func testStage4LiveBlindRegenerationOnWorker() async throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["TTS_LIVE_ACCEPTANCE"] == "blind-regenerate-live", "Opt-in real blind regeneration on a development worker")
        XCTAssertEqual(stageAcceptancePort, 9981)
        let studio = store(mock: false)
        await studio.loadInitial()
        XCTAssertEqual(studio.compute.library?.path, "/tmp/tts-library-test")
        let device = try XCTUnwrap(studio.compute.devices.first { $0.type == "lan" && $0.status == "idle" })
        let versions: [ModelVersion] = try await studio.api.getWithoutRetry("/characters/alice/versions")
        let version = try XCTUnwrap(versions.first { $0.engine == "v2pro" })
        let manager = studio.blindManager
        manager.planner.selected = device.id
        try await manager.create(BlindCreate(character: "alice", a: BlindSide(engine: "v2pro", version: version.id), b: BlindSide(engine: "v2pro", version: version.id), eval_set: "mini", repeats: 1, title: "zz-test-mac-r4-regeneration"))
        let id = try XCTUnwrap(manager.selectedID)
        let initialID = try XCTUnwrap(manager.test?.job_id)
        let initial: Job = try await studio.api.getWithoutRetry("/jobs/\(initialID)")
        if initial.active { try await studio.cancel(initial) }
        for _ in 0..<50 {
            try await manager.refresh()
            if manager.test?.status == "failed" { break }
            try await Task.sleep(for: .milliseconds(200))
        }
        XCTAssertEqual(manager.test?.status, "failed")
        try await Task.sleep(for: .milliseconds(500)) // drain the intentionally cancelled creation monitor
        studio.errorMessage = nil
        XCTAssertNil(manager.test?.a); XCTAssertNil(manager.test?.b)
        manager.regenerationPlanner.selected = device.id
        try await capture("live-r4-blind-regeneration-selector", size: CGSize(width: 1100, height: 420), studio: studio, view: BlindRegenerationControls(manager: manager, expanded: true).padding(24))
        try await manager.regenerate()
        let regenerationID = try XCTUnwrap(manager.test?.job_id)
        XCTAssertNotEqual(regenerationID, initialID)
        let submitted: Job = try await studio.api.getWithoutRetry("/jobs/\(regenerationID)")
        XCTAssertEqual(submitted.compute?.device, device.id)
        try await capture("live-r4-blind-regenerating", size: CGSize(width: 1280, height: 820), studio: studio, view: BlindView())
        let start = Date()
        while manager.test?.status == "generating" && Date().timeIntervalSince(start) < 600 {
            try await Task.sleep(for: .seconds(2)); try await manager.refresh()
        }
        let finished = try XCTUnwrap(manager.test)
        XCTAssertEqual(finished.status, "ready")
        XCTAssertEqual(finished.n_ready, finished.n_questions)
        XCTAssertNil(finished.a); XCTAssertNil(finished.b)
        let job: Job = try await studio.api.getWithoutRetry("/jobs/\(regenerationID)")
        XCTAssertEqual(job.status, "succeeded", job.error ?? "")
        XCTAssertEqual(job.compute?.device, device.id)
        let first = try XCTUnwrap(finished.questions.first)
        let left = try XCTUnwrap(first.left_url), right = try XCTUnwrap(first.right_url)
        let leftBytes = try await studio.api.data(studio.api.request(left), retry503: false)
        let rightBytes = try await studio.api.data(studio.api.request(right), retry503: false)
        XCTAssertFalse(leftBytes.isEmpty); XCTAssertFalse(rightBytes.isEmpty)
        try await capture("live-r4-blind-regenerated", size: CGSize(width: 1280, height: 820), studio: studio, view: BlindView())
        let evidence: [String: Any] = ["controller_port":stageAcceptancePort, "blind_id":id, "initial_job":initialID, "regeneration_job":regenerationID, "device_id":device.id, "device_name":device.name, "status":job.status, "questions_ready":finished.n_ready, "questions_total":finished.n_questions, "left_bytes":leftBytes.count, "right_bytes":rightBytes.count, "hidden_combos":finished.a == nil && finished.b == nil, "regeneration_seconds":Date().timeIntervalSince(start)]
        try JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys]).write(to: output.deletingLastPathComponent().appending(path: "stage3-round4-evidence.json"))
        print("STAGE4_LIVE_SUCCESS:blind=\(id),job=\(regenerationID),device=\(device.id),ready=\(finished.n_ready)")
    }
}
