import SwiftUI
import Charts

struct StatsView: View {
    @Environment(StudioStore.self) private var store
    var embeddedCharacter: String? = nil

    @State private var dimension = "engine"
    @State private var standaloneCharacter = ""
    @State private var scopeCurrentCharacter = true

    private var activeCharacterFilter: String {
        if let embeddedCharacter {
            return scopeCurrentCharacter ? embeddedCharacter : ""
        }
        return standaloneCharacter
    }
    private var groups: [StatsGroup] { store.stats?.dimensions.first { $0.dimension == dimension }?.groups ?? [] }
    private var labels: [String: String] { ["engine":"引擎", "character":"角色", "ref_id":"参考", "weights":"权重组合", "temperature":"温度", "top_k":"Top K"] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if embeddedCharacter == nil {
                    PageHeading(title: "评价统计", subtitle: "从已评价的声音里，找出更容易得到好结果的设置。")
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        scopeControl
                        dimensionPicker.frame(minWidth: 150, maxWidth: 220)
                        Spacer(minLength: 4)
                        refreshButton
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(spacing: 8) {
                            scopeControl
                            Spacer(minLength: 4)
                            refreshButton
                        }
                        dimensionPicker.frame(maxWidth: .infinity)
                    }
                }
                if let overall = store.stats?.overall {
                    HStack {
                        Metric(title: "已评价", value: String(overall.n))
                        Metric(title: "平均评分", value: number(overall.avg_rating))
                        Metric(title: "好评占比", value: "\(number(overall.good_ratio * 100, digits: 1))%")
                        Metric(title: "自动评分均值", value: number(overall.avg_score, digits: 1)).help(scoreHelp(overall.avg_score))
                    }.padding(16).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
                }
                Chart(groups) { group in
                    BarMark(x: .value("设置", group.key), y: .value("好评占比", group.good_ratio))
                        .foregroundStyle(Color.accentColor.gradient)
                }
                .chartYScale(domain: 0...1)
                .chartYAxis { AxisMarks(format: Decimal.FormatStyle.Percent.percent.scale(100)) }
                .chartYAxisLabel("好评占比")
                .frame(height: 200)

                ScrollView {
                    Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                        GridRow { Text("设置"); Text("条数"); Text("平均评分"); Text("好评占比"); Text("自动均分") }.font(.caption).foregroundStyle(.secondary)
                        Divider().gridCellColumns(5)
                        ForEach(groups) { group in
                            GridRow {
                                Text(group.key).frame(maxWidth: .infinity, alignment: .leading)
                                Text(String(group.n)); Text(number(group.avg_rating))
                                Text(group.good_ratio, format: .percent); Text(number(group.avg_score)).help(scoreHelp(group.avg_score))
                            }
                        }
                    }.padding(12)
                }.frame(height: CGFloat(min(8, max(1, groups.count)) * 30 + 52))

                if let tags = store.stats?.overall.tag_ratios, !tags.isEmpty {
                    Text("问题标签占比").font(.headline)
                    Chart(tags.keys.sorted(), id: \.self) { key in
                        BarMark(x: .value("占比", tags[key] ?? 0), y: .value("问题", key)).foregroundStyle(.orange.gradient)
                    }.chartXScale(domain: 0...1).frame(height: 140)
                } else {
                    Text("还没有问题标签；给候选添加标签后会显示占比。").foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, embeddedCharacter == nil ? 24 : 0)
            .padding(.vertical, embeddedCharacter == nil ? 12 : 0)
        }
        .task(id: activeCharacterFilter + "|" + String(store.reviewRevision)) {
            do { try await load() }
            catch is CancellationError { }
            catch { store.errorMessage = error.localizedDescription }
        }
    }

    private func load() async throws {
        let targetFilter = activeCharacterFilter
        let token = store.api.generation
        let result: ReviewStats = try await store.api.get(
            "/reviews/stats",
            query: targetFilter.isEmpty ? [:] : ["character": targetFilter]
        )
        try Task.checkCancellation()
        let latestFilter = embeddedCharacter != nil ? (scopeCurrentCharacter ? store.activeCharacter : "") : standaloneCharacter
        guard store.api.isCurrent(token), latestFilter == targetFilter else { return }
        store.stats = result
    }

    @ViewBuilder
    private var scopeControl: some View {
        if embeddedCharacter != nil {
            Picker("范围", selection: $scopeCurrentCharacter) {
                Text("仅看当前角色").tag(true)
                Text("全部角色").tag(false)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(minWidth: 160, maxWidth: 200)
        } else {
            Picker("角色", selection: $standaloneCharacter) {
                Text("全部").tag("")
                ForEach(store.models, id: \.slug) { Text(characterName($0.slug)).tag($0.slug) }
            }
            .frame(minWidth: 130, maxWidth: 180)
        }
    }

    private var dimensionPicker: some View {
        Picker("比较维度", selection: $dimension) {
            ForEach(store.stats?.dimensions ?? [], id: \.dimension) { Text(labels[$0.dimension] ?? $0.dimension).tag($0.dimension) }
        }
    }

    private var refreshButton: some View {
        Button("刷新", systemImage: "arrow.clockwise") { store.perform { try await load() } }
            .fixedSize()
    }
}
