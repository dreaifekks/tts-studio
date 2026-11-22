import SwiftUI

struct ComputeSettingsView: View {
    @Environment(StudioStore.self) private var studio
    @State private var adding = false
    @State private var editing: ComputeDevice?
    @State private var rePairing = false
    @State private var automatic = AutoSettings()
    var body: some View {
        SettingsPage {
            SettingsSection(title: "设备") {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        Text("名称").frame(minWidth: 120, idealWidth: 150, maxWidth: 165, alignment: .leading)
                        Text("状态").frame(minWidth: 85, idealWidth: 105, maxWidth: 115, alignment: .leading)
                        Text("常用模型的速度")
                        Spacer(minLength: 8)
                        SpeedLegend()
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 12) {
                            Text("名称").frame(minWidth: 120, maxWidth: 150, alignment: .leading)
                            Text("状态").frame(minWidth: 85, maxWidth: 105, alignment: .leading)
                            Text("常用模型的速度")
                            Spacer(minLength: 0)
                        }
                        SpeedLegend()
                    }
                }.font(.caption).foregroundStyle(.secondary)
                if studio.compute.devices.isEmpty {
                    ContentUnavailableView("还没有设备", systemImage: "desktopcomputer", description: Text("添加一台电脑，或连接云端 GPU。"))
                }
                ForEach(studio.compute.devices.filter { $0.type != "cloud" }) { device in
                    Divider()
                    DeviceRow(device: device, onEdit: { rePairing = false; editing = device }, onRePair: { rePairing = true; editing = device })
                }
                let clouds = studio.compute.devices.filter { $0.type == "cloud" }
                if !clouds.isEmpty {
                    let cloudState = Self.cloudRowState(for: clouds)
                    Divider()
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("云端 GPU").fontWeight(.medium)
                            Text("\(studio.compute.cloud?.accounts.filter { !$0.disabled }.count ?? 0) 个账号").font(.caption).foregroundStyle(.secondary)
                        }.frame(minWidth: 120, idealWidth: 150, maxWidth: 165, alignment: .leading)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 5) {
                                Circle().fill(cloudState.dotColor).frame(width: 6, height: 6)
                                Text(cloudState.label).lineLimit(1)
                            }
                            Text("共 \(studio.compute.cloud?.gpus.filter(\.available).count ?? 0) 张卡能开")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                        Button("管理") { studio.settingsTab = .cloud }.fixedSize()
                    }.font(.callout)
                }
                Divider()
                HStack { Button("添加算力", systemImage: "plus") { adding = true }; Text("家里或远程的电脑，或者云端 GPU").font(.caption).foregroundStyle(.secondary) }
            }
            Text("点名称左边的 ▸ 展开，看每个模型的生成速度与训练/打分速度。「完整测速」测全部模型，约 8–15 分钟。").font(.caption).foregroundStyle(.secondary)
            SettingsSection(title: "自动选择") {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        Picker("在哪跑", selection: $automatic.mode) { Text("自动选择").tag("auto"); Text("固定一台").tag("fixed") }
                            .frame(maxWidth: 260)
                        if automatic.mode == "fixed" {
                            Picker("设备", selection: Binding(get: { automatic.device ?? "" }, set: { automatic.device = $0 })) {
                                Text("选择设备").tag("")
                                ForEach(studio.compute.devices) { Text($0.name).tag($0.id) }
                            }.frame(maxWidth: 220)
                        }
                        Spacer(minLength: 8)
                        autoModeHelpIcon
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Picker("在哪跑", selection: $automatic.mode) { Text("自动选择").tag("auto"); Text("固定一台").tag("fixed") }
                            Spacer(minLength: 8)
                            autoModeHelpIcon
                        }
                        if automatic.mode == "fixed" {
                            Picker("设备", selection: Binding(get: { automatic.device ?? "" }, set: { automatic.device = $0 })) {
                                Text("选择设备").tag("")
                                ForEach(studio.compute.devices) { Text($0.name).tag($0.id) }
                            }
                        }
                    }
                }
                WrappingHStack(horizontalSpacing: 8, verticalSpacing: 8, alignLastTrailingOnSingleLine: true) {
                    askThresholdFields
                    saveAutomaticButton
                }.textFieldStyle(.roundedBorder)
            }
        }
        .onAppear { automatic = studio.compute.automatic }
        .onChange(of: studio.compute.automatic.mode + (studio.compute.automatic.device ?? "") + String(studio.compute.automatic.ask_over) + String(studio.compute.automatic.ask_month)) { automatic = studio.compute.automatic }
        .sheet(isPresented: $adding) { AddComputeView() }
        .sheet(item: $editing) { device in DeviceEditor(device: device, rePair: rePairing) }
        .task(id: studio.jobs.map { $0.id + $0.status }.joined()) {
            let token = studio.api.generation
            do { try await studio.compute.refreshDevices() }
            catch is CancellationError { }
            catch { studio.api.ifCurrent(token) { studio.compute.error = error.localizedDescription } }
        }
    }

    private var autoModeHelpIcon: some View {
        Image(systemName: "info.circle").help("本机很快能跑完就用本机。\n局域网里有明显更快、又空着的设备就换那台。\n任务很大、云端 GPU 能省一半以上时间才用它；训练默认用云端 GPU。\n花费超过提醒线时先问你。")
    }

    private var askThresholdFields: some View {
        HStack(spacing: 6) {
            Text("先问我：一次花费超过").fixedSize()
            TextField("2", value: $automatic.ask_over, format: .number).frame(width: 60)
            Text("单元，或本月超过").fixedSize()
            TextField("500", value: $automatic.ask_month, format: .number).frame(width: 70)
            Text("单元时").fixedSize()
        }
    }

    private var saveAutomaticButton: some View {
        Button("保存") { studio.perform { try await studio.compute.saveAutomatic(automatic) } }
            .disabled(automatic.mode == "fixed" && (automatic.device ?? "").isEmpty)
            .fixedSize()
    }

    enum CloudRowState: Equatable, Sendable {
        case active(String)
        case warm(String)
        case idle(String)

        var label: String {
            switch self {
            case .active(let text), .warm(let text), .idle(let text): text
            }
        }

        var dotColor: Color {
            switch self {
            case .active: .orange
            case .warm: .green
            case .idle: .secondary
            }
        }
    }

    nonisolated static func cloudRowState(for clouds: [ComputeDevice]) -> CloudRowState {
        let active = clouds.filter(\.isCloudActive)
        if !active.isEmpty {
            let detail = active.map { "\($0.name) \($0.statusLabel)" }.joined(separator: "、")
            return .active("使用中 · \(detail)")
        }
        let warm = clouds.filter(\.isCloudWarm)
        if !warm.isEmpty {
            let detail = warm.map { device in
                let count = device.warm ?? 0
                return count > 1 ? "\(device.name) ×\(count)" : device.name
            }.joined(separator: "、")
            return .warm("保留中 · \(detail)")
        }
        return .idle("未开")
    }
}
struct SpeedLegend: View {
    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "bolt.fill").foregroundStyle(.green); Text("快")
            Image(systemName: "bolt.fill").foregroundStyle(.yellow); Text("中")
            Image(systemName: "bolt.fill").foregroundStyle(.red); Text("慢")
            Image(systemName: "circle").foregroundStyle(.secondary); Text("未测")
        }.font(.caption).fixedSize().help("悬停或展开查看具体耗时与数据来源")
    }
}
struct DeviceRow: View {
    @Environment(StudioStore.self) private var studio
    var device: ComputeDevice
    var onEdit: () -> Void
    var onRePair: () -> Void = {}
    @State var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Button { expanded.toggle() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: expanded ? "chevron.down" : "chevron.right").font(.caption)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(device.name).fontWeight(.medium).lineLimit(1)
                            Text(device.typeLabel).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }.frame(minWidth: 120, idealWidth: 150, maxWidth: 165, alignment: .leading).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("\(expanded ? "收起" : "展开")\(device.name)")
                HStack(spacing: 5) {
                    Circle().fill(benchmarking ? Color.orange : device.status == "idle" ? Color.green : device.status == "busy" || device.status == "training" ? .orange : .secondary).frame(width: 6, height: 6)
                    Text(benchmarking ? "测速中…" : device.statusLabel).lineLimit(1)
                }.frame(minWidth: 85, idealWidth: 105, maxWidth: 115, alignment: .leading)
                VStack(alignment: .leading, spacing: 5) {
                    CommonSpeedIcons(engines: device.engines, speeds: device.speeds)
                    Text("已测 \(device.engines.filter { device.speeds[$0]?.effectiveSource() == "measured" }.count) / \(device.engines.count)").font(.caption).foregroundStyle(.secondary)
                }.frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                Button("完整测速") { benchmark() }.disabled(benchmarking).fixedSize()
                Menu { DeviceMenuActions(device: device, onEdit: onEdit, onRePair: onRePair)
                } label: { Text("⋯") }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("\(device.name)的操作")
            }.font(.callout)
            if expanded {
                VStack(alignment: .leading, spacing: 8) {
                    ModelSpeedTable(
                        engines: engineKeysForSpeedTable(engines: studio.compute.engines + device.engines, speeds: device.speeds),
                        speeds: device.speeds,
                        device: device.id
                    )
                    if let diskLine = device.diskSummary {
                        HStack(spacing: 6) {
                            Image(systemName: "internaldrive")
                                .font(.caption)
                                .foregroundStyle(device.isDiskLow ? .orange : .secondary)
                            Text(diskLine)
                                .font(.caption)
                                .monospacedDigit()
                                .foregroundStyle(device.isDiskLow ? .orange : .secondary)
                        }
                    }
                }
                .padding(.leading, 22)
            }
        }.opacity(device.status == "paused" ? 0.6 : 1)
    }
    private var benchmarking: Bool { studio.activeJobs.contains { $0.kind == "bench" && $0.target == device.id } }
    private func benchmark() { studio.perform { let job = try await studio.compute.benchmark(device: device.id); studio.recordLocalJob(job.id); studio.updateJob(job); studio.monitor(job) } }
}
struct DeviceMenuActions: View {
    @Environment(StudioStore.self) private var studio
    var device: ComputeDevice
    var onEdit: () -> Void
    var onRePair: () -> Void = {}
    var body: some View {
        if device.type != "cloud" { Button(device.type == "local" ? "改名…" : "改名 / 编辑…", action: onEdit) }
        if device.type == "lan" { Button("重新配对…", action: onRePair) }
        Button(device.status == "paused" ? "恢复使用" : "暂停使用") { studio.perform { try await studio.compute.patch(device.id, fields: ["paused":.bool(device.status != "paused")]) } }
        Button("完整测速") { studio.perform { let job = try await studio.compute.benchmark(device: device.id); studio.recordLocalJob(job.id); studio.updateJob(job); studio.monitor(job) } }
        if device.type == "lan" {
            Divider()
            Button("移除", role: .destructive) { studio.perform { try await studio.compute.remove(device.id) } }
        }
    }
}
struct CommonSpeedIcons: View {
    @Environment(StudioStore.self) private var studio
    var engines: [String] = []
    var speeds: [String: EngineSpeed]
    var body: some View {
        ViewThatFits(in: .horizontal) {
            icons
            ScrollView(.horizontal) { icons }.scrollIndicators(.hidden)
        }
    }
    nonisolated static func displayedEngines(
        commonModels: Set<String>,
        deviceEngines: [String],
        speeds: [String: EngineSpeed],
        fallbackEngines: [String]
    ) -> [String] {
        let speedEngines = speeds.keys.filter { $0 != "train" && $0 != "scorer" }
        let ownEngines = sortedEngines(deviceEngines + speedEngines)
        let base = ownEngines.isEmpty ? fallbackEngines : ownEngines
        return sortedEngines(base.filter { commonModels.contains($0) })
    }
    private var displayedEngines: [String] {
        let fallback = studio.compute.engines.isEmpty ? studio.allKnownEngines : studio.compute.engines
        return Self.displayedEngines(
            commonModels: studio.compute.commonModels,
            deviceEngines: engines,
            speeds: speeds,
            fallbackEngines: fallback
        )
    }
    private var icons: some View {
        HStack(spacing: 12) {
            ForEach(displayedEngines, id: \.self) { engine in
                HStack(spacing: 4) { Text(displayEngine(engine)); SpeedIcon(engine: engine, speed: speeds[engine]) }
            }
        }.font(.caption)
    }
}
struct ModelSpeedTable: View {
    @Environment(StudioStore.self) private var studio
    var engines: [String]
    var speeds: [String: EngineSpeed]
    var device: String? = nil

    private var orderedEngines: [String] {
        engineKeysForSpeedTable(engines: engines, speeds: speeds)
    }

    private var trainSpeed: EngineSpeed? { speeds["train"] }
    private var scorerSpeed: EngineSpeed? { speeds["scorer"] }

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 20, verticalSpacing: 10) {
            GridRow { Text("模型"); Text("生成速度"); Text("来源"); Text("") }.font(.caption).foregroundStyle(.secondary)
            ForEach(orderedEngines, id: \.self) { engine in
                let speed = speeds[engine]
                let valid = speed?.hasValidPositiveSpeed == true
                GridRow {
                    HStack(spacing: 6) {
                        Text(displayEngine(engine))
                        if studio.compute.commonModels.contains(engine) {
                            Text("常用")
                                .font(.caption2)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(Color.accentColor.opacity(0.12), in: Capsule())
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    HStack(spacing: 6) {
                        SpeedIcon(engine: engine, speed: speed)
                        Text(displayModelSpeed(speed))
                            .monospacedDigit()
                            .foregroundStyle(valid ? .primary : .secondary)
                    }
                    sourceBadge(for: speed, allowZero: false)
                    if let device, speed?.effectiveSource(allowZero: false) != "measured" {
                        Button("测这一个") {
                            studio.perform {
                                let job = try await studio.compute.benchmark(device: device, engine: engine)
                                studio.recordLocalJob(job.id); studio.updateJob(job); studio.monitor(job)
                            }
                        }
                        .controlSize(.small)
                    } else {
                        Text("")
                    }
                }
            }
            Divider().gridCellColumns(4)
            GridRow {
                let trainValid = trainSpeed?.hasValidPositiveSpeed == true
                Text("训练速度").foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    Image(systemName: "cpu")
                        .font(.caption)
                        .foregroundStyle(trainValid ? Color.accentColor : .secondary)
                    Text(displayTrainSpeed(trainSpeed))
                        .monospacedDigit()
                        .foregroundStyle(trainValid ? .primary : .secondary)
                }
                sourceBadge(for: trainSpeed, allowZero: false)
                Text("")
            }
            GridRow {
                let scorerPositive = scorerSpeed?.hasValidPositiveSpeed == true
                Text("评测打分").foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.seal")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(displayScorerSpeed(scorerSpeed))
                        .monospacedDigit()
                        .foregroundStyle(scorerPositive ? .primary : .secondary)
                }
                sourceBadge(for: scorerSpeed, allowZero: true)
                Text("")
            }
        }
        .font(.callout)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
    }

    private func sourceBadge(for speed: EngineSpeed?, allowZero: Bool) -> some View {
        let resolved = speed?.effectiveSource(allowZero: allowZero) ?? "untested"
        let label = speed?.effectiveSourceLabel(allowZero: allowZero) ?? "未测"
        let color: Color = resolved == "measured" ? .green : resolved == "reference" ? .orange : .secondary
        return Text(label)
            .font(.caption)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: Capsule())
            .foregroundStyle(color)
            .fixedSize()
    }
}
struct AddComputeView: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss
    @State private var kind = ""
    init(initialKind: String = "") { _kind = State(initialValue: initialKind) }
    var body: some View {
        if kind == "lan" { AddComputerView(flow: DeviceInviteFlow(compute: studio.compute), back: { kind = "" }) }
        else if kind == "cloud" { CloudLoginView(back: { kind = "" }) }
        else {
            VStack(alignment: .leading, spacing: 22) {
                Text("添加算力").font(.title2.bold())
                Button { kind = "lan" } label: { VStack(alignment: .leading, spacing: 8) { Label("局域网或远程电脑", systemImage: "desktopcomputer").font(.headline); Text("家里的另一台电脑，或者通过 Tailscale 连着的电脑。免费，用它自己的 CPU / 显卡。").font(.callout).foregroundStyle(.secondary).lineLimit(nil).fixedSize(horizontal: false, vertical: true) }.frame(maxWidth: .infinity, alignment: .leading).padding(16).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10)) }.buttonStyle(.plain)
                Button { kind = "cloud" } label: { VStack(alignment: .leading, spacing: 8) { Label("云端 GPU（Colab）", systemImage: "cloud").font(.headline); Text("把一个 Google 账号加进账号池。开机时只选卡，账号自动挑。").font(.callout).foregroundStyle(.secondary).lineLimit(nil).fixedSize(horizontal: false, vertical: true) }.frame(maxWidth: .infinity, alignment: .leading).padding(16).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10)) }.buttonStyle(.plain)
                HStack { Spacer(); Button("取消", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction) }
            }.padding(28).frame(width: 510)
        }
    }
}
struct DeviceEditor: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss
    var device: ComputeDevice?
    var rePair = false
    init(device: ComputeDevice?, rePair: Bool = false, failure: String? = nil) { self.device = device; self.rePair = rePair; _error = State(initialValue: failure) }
    @State private var name = ""
    @State private var address = ""
    @State private var pairCode = ""
    @State private var wake = ""
    @State private var broadcast = ""
    @State private var error: String?
    @State private var saving = false
    private var wakePayload: JSONValue { wake.isEmpty ? .null : DeviceWake(mac: wake, broadcast: broadcast.isEmpty ? nil : broadcast).jsonValue }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(device == nil ? "添加局域网设备" : rePair ? "重新配对" : device?.type == "local" ? "设备改名" : "编辑设备").font(.title2.bold())
            if device == nil || rePair { Text("在那台电脑上启动执行程序后，终端会显示连接地址和配对码。").font(.callout).foregroundStyle(.secondary) }
            Form {
                TextField("设备名", text: $name).disabled(rePair)
                if device?.type != "local" {
                    TextField("连接地址", text: $address)
                    TextField(device == nil || rePair ? "配对码（XXX-XXX）" : "配对码（重新配对时填写）", text: $pairCode)
                    DisclosureGroup("唤醒设置") {
                        Text("设备睡眠时，派任务前先唤醒。").font(.caption).foregroundStyle(.secondary)
                        TextField("唤醒 MAC（可选）", text: $wake)
                        TextField("广播地址（可选）", text: $broadcast)
                    }.disabled(rePair)
                }
            }.textFieldStyle(.roundedBorder)
            if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Button("取消", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(device == nil ? "连接并添加" : "保存") {
                    saving = true
                    Task {
                        defer { saving = false }
                        do {
                            if let device {
                                var fields: [String: JSONValue] = ["name":.string(name)]
                                if device.type == "lan" {
                                    fields["address"] = .string(address)
                                    fields["wake"] = wakePayload
                                    if !pairCode.isEmpty { fields["pair_code"] = .string(pairCode) }
                                }
                                if !pairCode.isEmpty { try await studio.compute.repairDevice(device.id, address: address, pairCode: pairCode, fields: fields) }
                                else { try await studio.compute.patch(device.id, fields: fields) }
                            } else {
                                _ = try await studio.compute.addDevice(name: name, address: address, pairCode: pairCode, wake: wake.isEmpty ? nil : DeviceWake(mac: wake, broadcast: broadcast.isEmpty ? nil : broadcast))
                            }
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(saving || (name.isEmpty && device?.type != "local") || ((device == nil || rePair) && (address.isEmpty || pairCode.isEmpty)))
            }
        }.padding(28).frame(width: 540)
        .onAppear { if let device { name = device.name; address = device.address ?? ""; wake = device.wake?.mac ?? ""; broadcast = device.wake?.broadcast ?? "" } }
    }
}

struct AddComputerView: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss
    @State var flow: DeviceInviteFlow
    var back: () -> Void = {}
    var polling = true
    init(flow: DeviceInviteFlow, back: @escaping () -> Void = {}, polling: Bool = true, manual: Bool = false, os: String = "unix", copyError: String? = nil) {
        _flow = State(initialValue: flow); self.back = back; self.polling = polling
        _manual = State(initialValue: manual); _os = State(initialValue: os); _copyError = State(initialValue: copyError)
    }
    @State private var os = "unix"
    @State private var manual = false
    @State private var copied = false
    @State private var copyError: String?
    @State private var operation: Task<Void, Never>?
    var body: some View {
        @Bindable var flow = flow
        VStack(alignment: .leading, spacing: 20) {
            HStack { Text("添加算力").font(.title2.bold()); Spacer(); Text("局域网 / 远程 · 第 \(flow.step) 步，共 4 步").font(.caption).foregroundStyle(.secondary) }
            Divider()
            if flow.step == 1 {
                Text("那台电脑和这台 Studio 在哪个网络里？").font(.headline)
                Picker("网络", selection: $flow.network) {
                    Text("家里的局域网").tag("lan")
                    Text("Tailscale").tag("tailscale")
                }.pickerStyle(.radioGroup)
                Text("不在家里的电脑选 Tailscale。").foregroundStyle(.secondary)
            } else if flow.step == 2, let invite = flow.invite {
                if os != "windows" || invite.commands.windows != nil {
                    Text("在要添加的那台电脑上打开终端，粘贴下面这一行运行。")
                }
                Picker("系统", selection: $os) {
                    Text("macOS / Linux").tag("unix")
                    Text(invite.commands.windows == nil ? "Windows · 暂不支持" : "Windows").tag("windows")
                }.pickerStyle(.segmented)
                if os == "windows", invite.commands.windows == nil { Text("Windows 暂不支持").foregroundStyle(.secondary) }
                else {
                    HStack(alignment: .top) {
                        Text(os == "windows" ? invite.commands.windows ?? "" : invite.commands.unix).font(.system(.callout, design: .monospaced)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        Button(copied ? "已复制" : "复制") {
                            NSPasteboard.general.clearContents()
                            copied = NSPasteboard.general.setString(os == "windows" ? invite.commands.windows ?? "" : invite.commands.unix, forType: .string)
                            copyError = copied ? nil : "复制失败，请选中命令后手动复制。"
                        }
                    }.padding(14).background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                }
                if os != "windows" || invite.commands.windows != nil {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("装好需要的程序（约 7GB，5–10 分钟）")
                        Text("装完自动连回这台 Studio，这里会自己跳到下一步")
                        Text("配对码 \(invite.code) 只能用一次，10 分钟内有效")
                    }.font(.callout).foregroundStyle(.secondary)
                }
                expiration
            } else if flow.step == 3 {
                if !flow.expired && flow.device == nil { ProgressView(flow.installing ? "已连上主控，正在安装…" : "正在等待设备连接…").controlSize(.small) }
                if !flow.expired {
                    Text(flow.installing ? "安装完成后会自动显示设备信息。安装没有继续时，可以重新生成配对码。" : flow.device == nil ? "那台电脑装完会自动连上。" : "设备已连上，可以返回查看识别信息。").foregroundStyle(.secondary)
                }
                expiration
                DisclosureGroup("没有自动连上？手动填", isExpanded: $manual) {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("安装结束时，那台电脑会显示连接地址，填到这里。").font(.caption).foregroundStyle(.secondary)
                        TextField("连接地址", text: $flow.manualAddress)
                        Button("连接") { run { await flow.manual() } }.disabled(flow.busy || flow.manualAddress.isEmpty || flow.expired || flow.device != nil || flow.installing)
                        if let error = flow.manualError { Text(error).foregroundStyle(.orange).textSelection(.enabled) }
                    }.textFieldStyle(.roundedBorder).padding(.top, 8)
                }
            } else if let device = flow.device {
                Label("已连上", systemImage: "checkmark.circle.fill").font(.headline).foregroundStyle(.green)
                Text("自动识别出这些信息，名称可以改").font(.callout).foregroundStyle(.secondary)
                if device.status == "offline" { Label("设备暂时离线", systemImage: "wifi.slash").foregroundStyle(.orange) }
                Form {
                    TextField("名称", text: $flow.name)
                    LabeledContent("系统", value: device.hw?.osLabel ?? "暂未识别")
                    LabeledContent("可用加速", value: device.hw?.accelerationLabel ?? "暂未识别")
                    LabeledContent("能跑的引擎", value: device.engines.map(displayEngine).joined(separator: " · "))
                }.textFieldStyle(.roundedBorder)
                Picker("测速", selection: $flow.quick) {
                    Text("现在做快速测速（约 2 分钟）").tag(true)
                    Text("以后再测，先用参考值").tag(false)
                }.pickerStyle(.radioGroup)
                Text("测完自动选最快的设置，估时更准。以后可以在设备列表里做约 8 分钟的完整测速。").font(.caption).foregroundStyle(.secondary)
            }
            if let error = flow.error { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            if let copyError { Text(copyError).font(.callout).foregroundStyle(.orange) }
            Divider()
            HStack {
                Button("上一步") {
                    if flow.step == 1 { back() }
                    else { flow.step -= 1 }
                }.disabled(flow.busy)
                Spacer()
                if flow.busy { ProgressView().controlSize(.small) }
                Button("取消", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                if flow.step == 1 { Button("下一步") { run { await flow.nextFromNetwork() } }.buttonStyle(.borderedProminent).disabled(flow.busy) }
                else if flow.step == 2 { Button(flow.device == nil ? "我已经运行了" : "查看设备") { flow.step = flow.device == nil ? 3 : 4 }.buttonStyle(.borderedProminent).disabled(flow.expired || os == "windows" && flow.invite?.commands.windows == nil) }
                else if flow.step == 3, flow.device != nil { Button("查看设备") { flow.step = 4 }.buttonStyle(.borderedProminent) }
                else if flow.step == 4 {
                    Button("完成") {
                        run {
                            flow.busy = true; flow.error = nil
                            defer { flow.busy = false }
                            do {
                                if let job = try await flow.finish() { studio.recordLocalJob(job.id); studio.updateJob(job); studio.monitor(job) }
                                dismiss()
                            } catch { flow.error = "保存或测速没有完成，请重试。" }
                        }
                    }.buttonStyle(.borderedProminent).disabled(flow.busy || flow.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }.padding(28).frame(width: 620)
        .task(id: flow.pollingKey) { if polling, let id = flow.invite?.id { await flow.poll(id) } }
        .onChange(of: os) { copied = false; copyError = nil }
        .onChange(of: flow.invite?.id) { copied = false; copyError = nil }
        .onDisappear { operation?.cancel(); Task { await flow.close() } }
    }
    @ViewBuilder private var expiration: some View {
        if flow.device == nil {
        if flow.expired { Text("配对码已过期，请重新生成。").foregroundStyle(.orange) }
        else if let end = flow.deadline {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(end <= context.date ? "配对码已过期，请重新生成。" : "配对码还剩约 \(max(1, Int(ceil(end.timeIntervalSince(context.date) / 60)))) 分钟").font(.caption).foregroundStyle(.secondary)
            }
        }
        if flow.expired || flow.step == 3 { Button("重新生成配对码") { run { await flow.generate() } }.disabled(flow.busy) }
        }
    }
    private func run(_ action: @escaping @MainActor () async -> Void) { operation = Task { await action() } }
}
