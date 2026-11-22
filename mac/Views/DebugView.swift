import SwiftUI
import Charts

struct DebugView: View {
    @Environment(StudioStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let item: AudioItem
    @State private var segmentIndex = 0
    @State private var selectedStep: Int?
    @State private var image: NSImage?
    @State private var error: String?
    private var segments: [Segment] { item.segments ?? [] }
    private var segment: Segment? { segments.indices.contains(segmentIndex) ? segments[segmentIndex] : nil }
    private var step: Step? { segment?.steps?.min { abs($0.step - (selectedStep ?? 0)) < abs($1.step - (selectedStep ?? 0)) } }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Text("Debug · 采样与频谱").font(.title2.bold()); Spacer(); Button("完成") { dismiss() }.keyboardShortcut(.cancelAction) }
            Text("\(item.engine) · \(item.id)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            if segments.isEmpty { ContentUnavailableView("没有采样轨迹", systemImage: "chart.xyaxis.line", description: Text("这条音频没有可显示的采样记录。")) }
            else {
                Picker("文本分段", selection: $segmentIndex) { ForEach(segments.indices, id: \.self) { Text("第 \($0 + 1) 段").tag($0) } }.pickerStyle(.segmented)
                if let segment {
                    HStack {
                        Metric(title: "EOS 位置", value: segment.eos_step.map(String.init) ?? "未出现")
                        Metric(title: "每音素 token", value: number(segment.tokens_per_phone))
                        Metric(title: "相对参考", value: "\(number(segment.ratio_vs_ref))×")
                        Metric(title: "终止方式", value: segment.hit_max_len ? "达到上限" : (segment.eos_by == "sampled" ? "采样 EOS" : "最高概率 EOS"))
                    }
                    Chart {
                        ForEach(segment.steps ?? [], id: \.step) { step in LineMark(x: .value("步数", step.step), y: .value("熵（bit）", step.entropy)).interpolationMethod(.linear) }
                        if let eos = segment.eos_step { RuleMark(x: .value("EOS", eos)).foregroundStyle(.orange).annotation(position: .top) { Text("EOS").font(.caption) } }
                        if let selectedStep { RuleMark(x: .value("步数", selectedStep)).foregroundStyle(.secondary) }
                    }.chartXAxisLabel("步数").chartYAxisLabel("熵（bit）").chartXSelection(value: $selectedStep)
                    .chartOverlay { proxy in
                        GeometryReader { geometry in
                            Rectangle().fill(.clear).contentShape(Rectangle()).onContinuousHover { phase in
                                switch phase {
                                case .active(let location):
                                    if let frame = proxy.plotFrame { selectedStep = proxy.value(atX: location.x - geometry[frame].origin.x, as: Int.self) }
                                case .ended: selectedStep = nil
                                }
                            }
                        }
                    }.frame(height: 210)
                    if selectedStep != nil, let step {
                        Text("第 \(step.step) 步 · token \(step.token) · 采样概率 \(number(step.p, digits: 4)) · 原始概率 \(number(step.p_raw, digits: 4))")
                            .font(.callout.monospacedDigit())
                        Text("Top-5：" + step.top5.map { "\($0.token) (\(number($0.p, digits: 3)))" }.joined(separator: "   "))
                            .font(.caption.monospaced())
                    } else { Text("将鼠标移到曲线上查看 token、采样概率及 Top-5。 ").font(.callout).foregroundStyle(.secondary) }
                }
            }
            Divider()
            if let image { ScrollView { Image(nsImage: image).resizable().scaledToFit() }.frame(minHeight: 180) }
            else if let error { Text(error).foregroundStyle(.red) }
            else if item.mel_url != nil { ProgressView("加载频谱…") }
            else { Text("本条没有 mel 频谱。 ").foregroundStyle(.secondary) }
        }.padding(24).frame(width: 900, height: 760)
        .task {
            guard let path = item.mel_url else { return }
            do { image = NSImage(data: try await store.api.data(store.api.request(path))) }
            catch { self.error = error.localizedDescription }
        }
    }
}
