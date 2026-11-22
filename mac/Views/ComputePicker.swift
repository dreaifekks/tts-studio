import SwiftUI

struct SpeedIcon: View {
    var engine: String
    var speed: EngineSpeed?
    var body: some View {
        let grade = speed?.grade
        Image(systemName: grade == nil ? "circle" : "bolt.fill")
            .foregroundStyle(grade == "快" ? Color.green : grade == "中" ? .yellow : grade == "慢" ? .red : .secondary)
            .help(tooltip).accessibilityLabel(tooltip)
    }
    private var tooltip: String {
        guard let speed, let grade = speed.grade, let seconds = speed.sec_per_item else { return "\(displayEngine(engine)) · 未测" }
        return "\(displayEngine(engine)) · \(grade) · 每句约 \(displaySpeedSeconds(seconds)) 秒 · \(speed.sourceLabel)"
    }
}
struct ComputePicker<Input: Encodable>: View {
    @Environment(StudioStore.self) private var studio
    @Bindable var planner: TaskPlanner
    var input: Input
    var action: String
    var sweep = false
    var contextKey = ""
    var taskTitle = ""
    var preview = true
    var ready = true
    @State var expanded = false
    private var key: String { studio.api.baseURL + TaskPlanner.previewID(input) + (planner.selected ?? "auto") + contextKey + taskTitle + String(ready) + String(preview) }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("在哪跑").font(.caption).fixedSize()
                    pickerSummaryView.lineLimit(1)
                    Spacer(minLength: 4)
                    pickerActionButtons
                }
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("在哪跑").font(.caption).fixedSize()
                        Spacer(minLength: 4)
                        pickerActionButtons
                    }
                    pickerSummaryView
                        .fixedSize(horizontal: false, vertical: true)
                }
            }.font(.callout)
            if ready, let error = planner.error { Text(error).font(.caption).foregroundStyle(.orange) }
            if ready, expanded {
                VStack(spacing: 0) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text(computeTaskHeading(input, action: action, title: taskTitle))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        SpeedLegend()
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
                    Divider()
                    Button { planner.selected = nil; expanded = false } label: {
                        HStack { Image(systemName: planner.selected == nil ? "largecircle.fill.circle" : "circle"); Text("自动选择"); Spacer() }
                    }.buttonStyle(.plain).padding(10)
                    if preview, let plan = planner.plan {
                        ForEach(plan.candidates) { candidate in
                            Divider()
                            Button { planner.selected = candidate.device; expanded = false } label: {
                                CandidateChoice(candidate: candidate, selected: planner.selected == candidate.device, recommended: candidate.device == plan.recommended, engine: planner.previewEngine, kind: planner.previewKind)
                            }.buttonStyle(.plain).disabled(!candidate.available || planner.submitting).padding(10)
                        }
                    } else if !preview {
                        ForEach(studio.compute.devices.filter { $0.type != "cloud" }) { device in
                            Divider()
                            Button { planner.selected = device.id; expanded = false } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: planner.selected == device.id ? "largecircle.fill.circle" : "circle")
                                    VStack(alignment: .leading, spacing: 3) { Text(device.name).fontWeight(.medium); Text("\(device.typeLabel) · \(device.statusLabel)").font(.caption).foregroundStyle(.secondary) }
                                    Spacer()
                                    Image(systemName: "circle").foregroundStyle(.secondary).accessibilityLabel("时间待测")
                                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            }.buttonStyle(.plain).disabled(planner.submitting || device.status == "paused" || device.status == "training" || device.status == "offline" && device.wakeable != true).padding(10)
                        }
                        if studio.compute.devices.filter({ $0.type != "cloud" }).isEmpty { Text("暂无可用设备").foregroundStyle(.secondary).padding(10) }
                    }
                }.background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                Text("只对这一次生效").font(.caption).foregroundStyle(.secondary)
            }
        }
        .task(id: key) {
            guard ready else { planner.clearPreview(); expanded = false; return }
            planner.taskSummary = "\(action)（\(computeTaskContent(input, title: taskTitle))）"
            if !preview { planner.clearPreview(); planner.previewKind = "gen"; planner.previewEngine = nil; return }
            do { try await Task.sleep(for: .milliseconds(250)); try Task.checkCancellation() } catch { return }
            await planner.refresh(api: studio.api, input: input, sweep: sweep)
        }
        .sheet(isPresented: Binding(get: { planner.confirmation != nil }, set: { if !$0 { planner.resolve(false) } })) {
            ComputeConfirmation(planner: planner, action: action, showSpeed: preview).environment(studio)
        }
        .onDisappear { planner.resolve(false) }
    }
    @ViewBuilder
    private var pickerSummaryView: some View {
        if !ready { Text("选择模型版本后估算").foregroundStyle(.secondary) }
        else if !preview { Text(anonymousSummary).foregroundStyle(.secondary) }
        else if let request = input as? SweepRequest, !request.ready { Text("选择角色与模型").foregroundStyle(.secondary) }
        else if planner.allCompleted { Text("都已完成").foregroundStyle(.secondary) }
        else if let plan = planner.plan, let candidate = plan.selected {
            Text(summary(plan, candidate)).foregroundStyle(.secondary).help(exactEstimate(candidate))
        } else if planner.loading {
            HStack(spacing: 4) {
                ProgressView().controlSize(.mini)
                Text("正在选择…").foregroundStyle(.secondary)
            }
        }
        else { Text("暂无可用设备").foregroundStyle(.secondary) }
    }
    @ViewBuilder
    private var pickerActionButtons: some View {
        if planner.selected != nil {
            Button("恢复自动") { planner.selected = nil }
                .buttonStyle(.link)
                .fixedSize()
                .disabled(planner.submitting || !ready)
        }
        Button(expanded ? "收起" : "更改") { expanded.toggle() }
            .buttonStyle(.link)
            .fixedSize()
            .disabled(planner.submitting || !ready)
    }
    private var anonymousSummary: String {
        guard let selected = planner.selected else { return "自动：按任务推荐" }
        return "这次：在 \(studio.compute.devices.first { $0.id == selected }?.name ?? "所选设备") 上\(action)"
    }
    private func summary(_ plan: ComputePlan, _ candidate: PlanCandidate) -> String {
        let suffix = planner.selected == nil ? computeReason(plan.reason_code) : candidate.cost
        let prepNote: String
        if candidate.type == "cloud", let prep = candidate.prep_sec, prep > 0 {
            prepNote = "（含准备约 \(max(1, Int((prep / 60).rounded()))) 分钟）"
        } else {
            prepNote = ""
        }
        return "\(planner.selected == nil ? "自动：" : "这次：")在 \(candidate.name) 上\(action) · \(displayDuration(candidate.seconds))\(prepNote)" + (suffix.isEmpty ? "" : " · \(suffix)")
    }
}
struct CandidateChoice: View {
    @Environment(StudioStore.self) private var studio
    var candidate: PlanCandidate
    var selected: Bool
    var recommended: Bool
    var engine: String? = nil
    var kind = "gen"
    var showSpeed = true
    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                radioIcon
                infoBlock
                Spacer(minLength: 6)
                metricsGroup
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    radioIcon
                    titleRow
                    Spacer(minLength: 4)
                    speedView
                }
                HStack(spacing: 8) {
                    subtitleLines.padding(.leading, 22)
                    Spacer(minLength: 4)
                    Text(displayDuration(candidate.seconds)).monospacedDigit().fixedSize()
                    Text(candidate.cost).monospacedDigit().fixedSize()
                }
            }
        }
        .font(.callout)
        .opacity(candidate.available ? 1 : 0.5)
        .contentShape(Rectangle())
        .help(exactEstimate(candidate))
    }

    private var radioIcon: some View {
        Image(systemName: selected ? "largecircle.fill.circle" : "circle")
            .foregroundStyle(selected ? Color.accentColor : .secondary)
    }

    private var titleRow: some View {
        HStack(spacing: 6) {
            Text(candidate.name).fontWeight(.medium).lineLimit(1)
            if recommended { Text("推荐").font(.caption).foregroundStyle(Color.accentColor).fixedSize() }
        }
    }

    private var subtitleLines: some View {
        VStack(alignment: .leading, spacing: 2) {
            if !candidate.available { Text(candidate.unavailableReason).font(.caption).foregroundStyle(.secondary) }
            else if candidate.wake == true { Text("睡眠中，开始前先唤醒").font(.caption).foregroundStyle(.secondary) }
            else { Text(computeQueueLabel(candidate, device: studio.compute.devices.first { $0.id == candidate.device })).font(.caption).foregroundStyle(.secondary) }
            if candidate.available, kind == "train", candidate.type == "local", studio.compute.devices.first(where: { $0.id == candidate.device })?.hw?.accel != "cuda" {
                Text("只能用 CPU 训练").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var infoBlock: some View {
        VStack(alignment: .leading, spacing: 3) {
            titleRow
            subtitleLines
        }
    }

    @ViewBuilder
    private var speedView: some View {
        if showSpeed {
            let resolvedEngine = engine ?? studio.preferredEngine
            SpeedIcon(engine: resolvedEngine, speed: studio.compute.devices.first { $0.id == candidate.device }?.speeds[resolvedEngine])
        } else {
            Image(systemName: "circle").foregroundStyle(.secondary).accessibilityLabel("速度待测")
        }
    }

    private var metricsGroup: some View {
        HStack(spacing: 10) {
            speedView
            Text(displayDuration(candidate.seconds)).monospacedDigit().fixedSize()
            Text(candidate.cost).monospacedDigit().frame(minWidth: 70, alignment: .trailing).fixedSize()
        }
    }
}
struct ComputeConfirmation: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.openSettings) private var openSettings
    @Bindable var planner: TaskPlanner
    var action: String
    var showSpeed = true
    var body: some View {
        if let plan = planner.confirmation {
            let chosen = plan.candidates.first { $0.device == planner.confirmationDevice }
            VStack(alignment: .leading, spacing: 18) {
                Text(chosen?.type == "cloud" ? "这次约花 \(displayUnits(chosen?.units ?? 0)) 单元，开始吗？" : "这次免费，开始吗？").font(.title2.bold())
                Text(description(plan, chosen)).foregroundStyle(.secondary)
                VStack(spacing: 0) {
                    HStack { Text("在哪跑"); Spacer(); Text("速度"); Text("大约多久").frame(width: 92); Text("花费").frame(width: 80) }.font(.caption).foregroundStyle(.secondary).padding(12)
                    SpeedLegend().frame(maxWidth: .infinity, alignment: .trailing).padding(.horizontal, 12).padding(.bottom, 8)
                    Divider()
                    ForEach(plan.candidates) { candidate in
                        Button { planner.confirmationDevice = candidate.device } label: {
                            CandidateChoice(candidate: candidate, selected: candidate.device == planner.confirmationDevice, recommended: candidate.device == plan.recommended, engine: planner.previewEngine, kind: planner.previewKind, showSpeed: showSpeed)
                        }.buttonStyle(.plain).padding(12).background(candidate.device == planner.confirmationDevice ? Color.accentColor.opacity(0.1) : .clear).disabled(!candidate.available)
                        if candidate.id != plan.candidates.last?.id { Divider() }
                    }
                }.background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
                HStack {
                    Text("本月已用 \(displayUnits(plan.month_used)) 单元" + (studio.compute.cloud.map { " · 账号池还剩 \(displayUnits($0.total_balance))" } ?? ""))
                    Spacer()
                    Button("调整提醒线") { planner.resolve(false); studio.settingsTab = .compute; openSettings() }
                }.font(.caption).foregroundStyle(.secondary)
                HStack {
                    Spacer()
                    Button("取消", role: .cancel) { planner.resolve(false) }.keyboardShortcut(.cancelAction)
                    Button("用 \(chosen?.gpu.map(displayGPU) ?? chosen?.name ?? "所选设备") 开始") { planner.resolve(true) }
                        .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(chosen?.available != true)
                }
            }.padding(26).frame(minWidth: 560, idealWidth: 680, maxWidth: 740)
        }
    }
    private func description(_ plan: ComputePlan, _ chosen: PlanCandidate?) -> String {
        let task = planner.taskSummary.isEmpty ? action : planner.taskSummary
        let preparation = chosen.flatMap { cloudPreparation($0) }.map { "（\($0)）" } ?? ""
        return "\(task)。用 \(chosen?.name ?? "所选设备") \(displayDuration(chosen?.seconds))\(preparation)。\(plan.reminder(for: chosen))"
    }
}
func exactEstimate(_ candidate: PlanCandidate) -> String {
    [candidate.seconds.map { "预计 \($0) 秒" }, candidate.units.map { "预计 \($0) 单元" }].compactMap { $0 }.joined(separator: " · ")
}

func computeTaskContent<B>(_ input: B, title: String = "") -> String {
    let detail: String
    if let value = input as? PlanInput {
        let models = (value.parts?.map(\.engine) ?? value.engines ?? value.engine.map { [$0] } ?? []).map(displayEngine).joined(separator: " / ")
        let amount = value.kind == "train" ? "\(value.lines) 句" : "\(value.items) \(value.kind == "gen" ? "句" : "条")"
        detail = [amount, models].filter { !$0.isEmpty }.joined(separator: " ")
    } else if let value = input as? SweepRequest {
        detail = "\(value.characters.count) 个角色 · " + value.engines.map(displayEngine).joined(separator: " / ") + " · \(value.trains.count) 组训练"
    } else { detail = "" }
    return title.isEmpty ? detail : [title, detail].filter { !$0.isEmpty }.joined(separator: " · ")
}
func computeTaskHeading<B>(_ input: B, action: String, title: String = "") -> String {
    "这一次在哪\(action)（\(computeTaskContent(input, title: title))）"
}
func cloudPreparation(_ candidate: PlanCandidate) -> String? {
    guard candidate.type == "cloud" else { return nil }
    guard let seconds = candidate.prep_sec else { return "要先准备环境" }
    guard seconds > 0 else { return nil }
    return "要先准备环境约 \(max(1, Int((seconds / 60).rounded()))) 分钟"
}
func computeQueueLabel(_ candidate: PlanCandidate, device: ComputeDevice?) -> String {
    if candidate.type == "cloud" { return candidate.idle ? (cloudPreparation(candidate) ?? "云端 GPU · 已就绪") : "云端 GPU · 使用中，要排队" }
    let prefix = device?.typeLabel ?? (candidate.type == "lan" ? "局域网设备" : candidate.type == "local" ? "本机" : "设备")
    if candidate.idle { return "\(prefix) · 空闲" }
    return "\(prefix) · \(device?.statusLabel ?? "忙")，要排队"
}
