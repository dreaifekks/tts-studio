import SwiftUI
import Charts

struct CloudSettingsView: View {
    @Environment(StudioStore.self) private var studio
    @State private var perspective = "gpu"
    init(initialPerspective: String = "gpu") { _perspective = State(initialValue: initialPerspective) }
    @State private var settings = CloudSettings()
    @State private var adding = false
    var body: some View {
        SettingsPage {
            if let cloud = studio.compute.cloud {
                ViewThatFits(in: .horizontal) {
                    HStack {
                        perspectivePicker
                        Spacer(minLength: 12)
                        poolSummaryText(cloud)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        perspectivePicker
                        poolSummaryText(cloud)
                    }
                }
                if perspective == "gpu" {
                    SettingsSection(title: "账号池能开的卡") {
                        SpeedLegend().frame(maxWidth: .infinity, alignment: .trailing)
                        HStack(spacing: 10) {
                            Text("GPU").frame(minWidth: 110, idealWidth: 140, maxWidth: 150, alignment: .leading)
                            Text("常用模型").frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                            Text("能开的账号").frame(minWidth: 84, idealWidth: 96, maxWidth: 105)
                            Text("账号池够用").frame(minWidth: 80, idealWidth: 92, maxWidth: 100)
                        }.font(.caption).foregroundStyle(.secondary)
                        ForEach(cloud.gpus.filter(\.available) + cloud.gpus.filter { !$0.available }) { gpu in
                            Divider()
                            CloudGPURow(gpu: gpu)
                        }
                        if cloud.accounts.filter({ !$0.disabled }).isEmpty {
                            Text("还没有登录账号，添加一个 Google 账号来使用云端 GPU。").font(.callout).foregroundStyle(.secondary)
                            Button("添加 Google 账号", systemImage: "plus") { adding = true }
                        }
                    }
                    Text("开机时只选卡，账号自动挑。点卡名左边的 ▸ 展开查看各模型生成速度与训练/打分速度。").font(.caption).foregroundStyle(.secondary)
                    SettingsSection(title: "使用偏好") {
                        Picker("开哪张卡", selection: $settings.gpu_pref) {
                            Text("按任务推荐").tag("auto")
                            ForEach(cloud.gpus) { Text("总用 \($0.label)").tag($0.gpu) }
                        }.frame(maxWidth: 340)
                        WrappingHStack(horizontalSpacing: 10, verticalSpacing: 8) {
                            maxMinutesPicker
                            maxMinutesField
                        }
                        Text(settings.keep_minutes == 0 ? "到时间自动关掉；任务做完也马上关" : "到时间自动关掉").font(.caption).foregroundStyle(.secondary)
                        WrappingHStack(horizontalSpacing: 8, verticalSpacing: 8) {
                            Toggle("做完先别关", isOn: Binding(get: { settings.keep_minutes > 0 }, set: { settings.keep_minutes = $0 ? 10 : 0 }))
                            if settings.keep_minutes > 0 {
                                HStack(spacing: 6) {
                                    Text("再留").fixedSize()
                                    TextField("10", value: $settings.keep_minutes, format: .number).frame(width: 60)
                                    Text("分钟").fixedSize()
                                }
                            }
                        }
                        HStack { Text("接着跑下一批就不用再准备环境。").font(.caption).foregroundStyle(.secondary); Spacer(); Button("保存") { studio.perform { try await studio.compute.saveCloud(settings) } } }
                    }.textFieldStyle(.roundedBorder).id("cloud-preferences")
                } else {
                    SettingsSection(title: "账号") {
                        ForEach(cloud.accounts) { account in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack {
                                    VStack(alignment: .leading, spacing: 5) { Text(account.displayName).fontWeight(.medium); Text(account.disabled ? "已停用" : account.gpus.map(displayGPU).joined(separator: " · ")).font(.caption).foregroundStyle(.secondary) }
                                    Spacer()
                                    VStack(alignment: .trailing, spacing: 5) { Text("剩余 \(account.balance.map(displayUnits) ?? "—")"); Text("本月已用 \(displayUnits(account.month_used_estimate))").font(.caption).foregroundStyle(.secondary).help("本机记录估计值：\(account.month_used_estimate) 单元") }
                                    Menu { CloudAccountActions(account: account) } label: { Text("⋯") }.menuStyle(.borderlessButton).fixedSize()
                                    if account.id == "default" {
                                        Button(account.disabled ? "启用" : "停用") { studio.perform { if account.disabled { try await studio.compute.enableAccount(account.id) } else { try await studio.compute.disableAccount(account.id) } } }
                                    } else {
                                        Button("移除") { studio.perform { try await studio.compute.removeAccount(account.id) } }
                                    }
                                }
                                if studio.compute.accountErrorDevice == account.id, let error = studio.compute.accountError { Text(error).font(.caption).foregroundStyle(.orange) }
                                if let error = account.error { Text(error).font(.caption).foregroundStyle(.orange) }
                            }.padding(.vertical, 4)
                            Divider()
                        }
                        if cloud.accounts.isEmpty { Text("还没有登录账号").foregroundStyle(.secondary) }
                        Button("添加 Google 账号", systemImage: "plus") { adding = true }
                    }
                    SettingsSection(title: "每月用量（单元）") {
                        CloudUsageChart(info: cloud)
                    }
                }
            } else if studio.compute.loading { ProgressView("正在读取云端 GPU…") }
            else {
                ContentUnavailableView { Label("云端 GPU 信息暂时取不到", systemImage: "cloud") } actions: {
                    Button("重试") { Task { await studio.compute.refreshCloud() } }
                }
            }
            if let error = studio.compute.cloudError, studio.compute.cloud != nil { HStack { Text(error).foregroundStyle(.orange); Button("重试") { Task { await studio.compute.refreshCloud() } } } }
        }
        .onAppear { if let value = studio.compute.cloud?.settings { settings = value } }
        .onChange(of: studio.compute.cloud?.settings) { if let value = studio.compute.cloud?.settings { settings = value } }
        .sheet(isPresented: $adding) { AddComputeView(initialKind: "cloud") }
    }

    private var perspectivePicker: some View {
        Picker("查看方式", selection: $perspective) { Text("按 GPU").tag("gpu"); Text("按账号").tag("account") }
            .pickerStyle(.segmented)
            .frame(maxWidth: 220)
    }

    private func poolSummaryText(_ cloud: CloudInfo) -> some View {
        Text("\(cloud.accounts.filter { !$0.disabled }.count) 个启用账号 · 可用余额 \(displayUnits(cloud.total_balance)) 单元 · 本月合计已用 \(displayUnits(cloud.month_used_estimate))")
            .font(.caption)
            .help("本月用量来自主控的本机记录，是估计值。精确合计：\(cloud.month_used_estimate) 单元")
    }

    private var maxMinutesPicker: some View {
        Picker("最长用多久", selection: Binding(get: { settings.max_minutes == nil ? "auto" : "custom" }, set: { settings.max_minutes = $0 == "auto" ? nil : 120 })) {
            Text("按任务安排").tag("auto"); Text("自定时间").tag("custom")
        }.frame(maxWidth: 280)
    }

    @ViewBuilder
    private var maxMinutesField: some View {
        if settings.max_minutes != nil {
            HStack(spacing: 6) {
                TextField("2", value: Binding(get: { Double(settings.max_minutes ?? 120) / 60 }, set: { settings.max_minutes = Int(($0 * 60).rounded()) }), format: .number).frame(width: 72)
                Text("小时").fixedSize()
            }
        }
    }
}
struct CloudGPURow: View {
    @Environment(StudioStore.self) private var studio
    var gpu: CloudGPU
    @State var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Button { expanded.toggle() } label: {
                    HStack(spacing: 6) {
                        Image(systemName: expanded ? "chevron.down" : "chevron.right").font(.caption)
                        Text(gpu.label).fontWeight(.medium).lineLimit(1)
                    }.frame(minWidth: 110, idealWidth: 140, maxWidth: 150, alignment: .leading).contentShape(Rectangle())
                }.buttonStyle(.plain)
                CommonSpeedIcons(speeds: gpu.speeds)
                    .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                Image(systemName: "info.circle").foregroundStyle(.secondary).help(gpu.units_per_hour.map { "每小时 \($0) 单元" } ?? "暂无报价").accessibilityLabel("查看使用费率")
                Text(gpu.available ? "\(gpu.account_count) 个账号能开" : "没有账号能开")
                    .lineLimit(1)
                    .frame(minWidth: 84, idealWidth: 96, maxWidth: 105)
                Text(gpu.available ? gpu.hours_left.map { "约 \(displayHours($0)) 小时" } ?? "—" : "—")
                    .lineLimit(1)
                    .frame(minWidth: 80, idealWidth: 92, maxWidth: 100)
                    .monospacedDigit()
            }.font(.callout).opacity(gpu.available ? 1 : 0.5)
            if !gpu.available, let reason = gpu.reason { Text(reason).font(.caption).foregroundStyle(.secondary).padding(.leading, 22) }
            if expanded {
                ModelSpeedTable(
                    engines: engineKeysForSpeedTable(engines: studio.compute.engines, speeds: gpu.speeds),
                    speeds: gpu.speeds
                ).padding(.leading, 22)
                Text("云端 GPU 第一次跑这个模型后，会自动记下速度。").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
struct CloudUsageChart: View {
    var info: CloudInfo
    @State private var selectedMonth: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if info.usage_monthly.isEmpty { Text("还没有用量记录").foregroundStyle(.secondary).frame(height: 150) }
            else {
                Chart {
                    ForEach(info.usage_monthly) { month in
                        ForEach(month.by_account.keys.sorted(), id: \.self) { id in
                            BarMark(x: .value("月份", month.month), y: .value("单元", month.by_account[id] ?? 0))
                                .foregroundStyle(by: .value("账号", info.account_labels[id] ?? id))
                        }
                    }
                    ForEach(info.usage_monthly) { month in
                        let total = month.by_account.values.reduce(0, +)
                        PointMark(x: .value("月份", month.month), y: .value("单元", total)).symbolSize(0).foregroundStyle(.clear)
                            .annotation(position: .top) { Text(displayUnits(total)).font(.caption).foregroundStyle(.secondary) }
                    }
                }.frame(height: 180).chartXSelection(value: $selectedMonth)
                if let month = info.usage_monthly.first(where: { $0.month == selectedMonth }) {
                    Text("\(month.month) · " + month.by_account.keys.sorted().map { id in "\(info.account_labels[id] ?? id)：\(displayUnits(month.by_account[id] ?? 0))" }.joined(separator: " · ")).font(.caption)
                }
            }
        }.help("用量来自主控的本机记录，是估计值。")
    }
}
struct CloudLoginView: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss
    @State private var step = 1
    @State private var login: CloudLogin?
    @State private var result: CloudLoginStatus?
    @State private var code = ""
    @State private var error: String?
    @State private var working = false
    @State private var submission = CloudCodeSubmission()
    @State private var addition = CloudLoginAddition()
    @State private var loginTask: Task<Void, Never>?
    var back: (() -> Void)?
    var polling = true
    init(account: CloudAccount? = nil, step: Int = 1, back: (() -> Void)? = nil, checking: Bool = false) {
        self.back = back
        _step = State(initialValue: step)
        _result = State(initialValue: account.map { CloudLoginStatus(state: "done", account: $0) })
        let submission = CloudCodeSubmission(); submission.submitted = checking
        _submission = State(initialValue: submission)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack { Text("添加云端 GPU").font(.title2.bold()); Spacer(); Text("云端 GPU · 第 \(step) 步，共 3 步").font(.caption).foregroundStyle(.secondary) }
            Divider()
            if step == 1 {
                if let account = result?.account, result?.state == "done" {
                    HStack {
                        Label("已登录：" + account.displayName, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                        Spacer(); Button("换一个") { discardAdded(); reset() }
                    }
                } else {
                    Text("把 Google 账号加进账号池，开机时会自动选用。").foregroundStyle(.secondary)
                    Text("会打开浏览器让你同意，回来就好。").font(.callout).foregroundStyle(.secondary)
                    if let login {
                        if !submission.submitted { Link("在浏览器中继续登录", destination: login.auth_url) }
                        if login.needs_code && !submission.submitted {
                            TextField("粘贴授权码", text: $code).textFieldStyle(.roundedBorder)
                            Button("继续") {
                                let authorizationCode = code
                                loginTask = Task {
                                    await submission.submit(api: studio.api, id: login.login_id, code: authorizationCode)
                                    if submission.submitted { code = "" }
                                }
                            }.disabled(code.isEmpty || submission.working)
                        }
                        if result?.state != "failed" { ProgressView(submission.submitted ? "正在核对…" : "等待浏览器授权…").controlSize(.small) }
                    } else if submission.submitted {
                        ProgressView("正在核对…").controlSize(.small)
                    } else {
                        Button("用 Google 账号登录") {
                            working = true
                            loginTask = Task {
                                defer { working = false }
                                do {
                                    let value: CloudLogin = try await studio.api.post("/cloud/accounts/login")
                                    if Task.isCancelled {
                                        let compute = studio.compute
                                        studio.perform { try await compute.cancelLogin(value.login_id) }
                                        return
                                    }
                                    addition.begin(value.login_id)
                                    login = value
                                    if !NSWorkspace.shared.open(value.auth_url) { error = "浏览器没有打开，可以点链接继续登录。" }
                                } catch is CancellationError { }
                                catch { self.error = error.localizedDescription }
                            }
                        }.buttonStyle(.borderedProminent).disabled(working)
                        if working { CloudLoginProgress(preparing: studio.compute.cloud?.installed == false) }
                    }
                    if let error { Text(error).foregroundStyle(.orange).textSelection(.enabled) }
                    if let error = submission.error { Text(error).foregroundStyle(.orange) }
                    if result?.state == "failed" { Button("重新登录") { reset() } }
                }
            } else if step == 2, let account = result?.account {
                CloudAccountRecognition(account: studio.compute.cloud?.accounts.first { $0.id == account.id } ?? account)
                if let failure = studio.compute.cloudError {
                    HStack { Text(failure).foregroundStyle(.orange); Button("重试") { Task { await studio.compute.refreshCloud() } } }
                }
            } else if step == 3 {
                Label("已加入账号池", systemImage: "checkmark.circle.fill").font(.headline).foregroundStyle(.green)
                if let cloud = studio.compute.cloud {
                    Text("加入后账号池共 \(cloud.enabledAccountCount) 个启用账号，合计剩余 \(displayUnits(cloud.total_balance)) 单元。")
                } else { Text("账号已加入，账号池余额暂时取不到。").foregroundStyle(.secondary) }
                Text("开机时只选卡，账号自动挑。").foregroundStyle(.secondary)
            }
            Divider()
            HStack {
                Button("上一步") { if step > 1 { step -= 1 } else { back?() } }.disabled(working || submission.working)
                Spacer()
                if step != 3 { Button("取消", role: .cancel) { code = ""; dismiss() }.keyboardShortcut(.cancelAction) }
                if step == 3 { Button("完成") { dismiss() }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction) }
                else { Button("下一步") { step += 1 }.buttonStyle(.borderedProminent).disabled(result?.state != "done") }
            }
        }.padding(28).frame(width: 540)
        // 没到第 3 步就离开（取消、Esc、上一步回到选类型）：这次新加的账号删掉
        .onDisappear { loginTask?.cancel(); code = ""; if step < 3 { discardAdded() } }
        .task(id: login?.login_id) {
            guard polling, let login else { return }
            await studio.api.pollCloudLogin(login.login_id) { value in
                result = value
                addition.finish(value)
                error = value.state == "failed" ? value.error ?? "登录没有完成。" : nil
                if value.state == "done" {
                    code = ""; step = 2; await studio.compute.refreshCloud()
                }
            } report: { _ in error = "登录状态暂时取不到，正在重试…" }
        }
    }
    private func reset() { loginTask?.cancel(); login = nil; result = nil; code = ""; error = nil; submission = CloudCodeSubmission(); step = 1 }
    private func discardAdded() {
        let addition = addition, compute = studio.compute
        studio.perform { try await addition.discard(compute) }  // 删不掉（还在跑任务、试的卡没释放完）时提示，账号留在列表里
    }
}

struct CloudLoginProgress: View {
    var preparing: Bool
    var body: some View { ProgressView(preparing ? "正在准备云端 GPU 组件…" : "正在打开登录…").controlSize(.small) }
}

struct CloudAccountActions: View {
    @Environment(StudioStore.self) private var studio
    var account: CloudAccount
    var body: some View {
        Button("刷新") { studio.perform { try await studio.compute.refreshAccount(account.id) } }.disabled(account.disabled)
        if account.id != "default" {
            Button(account.disabled ? "启用" : "停用") { studio.perform { if account.disabled { try await studio.compute.enableAccount(account.id) } else { try await studio.compute.disableAccount(account.id) } } }
        }
    }
}

struct CloudAccountRecognition: View {
    @Environment(StudioStore.self) private var studio
    var account: CloudAccount
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(account.displayName, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            Text("剩余 \(account.balance.map(displayUnits) ?? "暂时取不到") 单元 · 本月已用 \(displayUnits(account.month_used_estimate))")
            if let error = account.error { Text(computeFailure(error)).foregroundStyle(.orange) }
            if let cloud = studio.compute.cloud {
                HStack { Text("卡型与速度").font(.caption).foregroundStyle(.secondary); Spacer(); SpeedLegend() }
                ForEach(cloud.gpus) { gpu in
                    HStack {
                        Text(gpu.label).frame(width: 140, alignment: .leading)
                        CommonSpeedIcons(speeds: gpu.speeds)
                        Spacer()
                        Text(account.gpus.contains(gpu.gpu) ? "能开" : "暂时开不了").foregroundStyle(account.gpus.contains(gpu.gpu) ? Color.primary : .secondary)
                    }.font(.callout)
                }
                if cloud.gpus.isEmpty { Text("暂时没有可用的卡型信息").foregroundStyle(.secondary) }
            } else { Text("卡型信息暂时取不到").foregroundStyle(.secondary) }
        }
    }
}
