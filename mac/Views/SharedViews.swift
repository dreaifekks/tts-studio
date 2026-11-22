import SwiftUI

/// Single-instance wrapping row layout so editable controls and popovers never
/// duplicate across ViewThatFits branches or lose focus when resizing across thresholds.
struct WrappingHStack: Layout {
    var horizontalSpacing: CGFloat = 8
    var verticalSpacing: CGFloat = 8
    var alignLastTrailingOnSingleLine: Bool = false

    private struct Row {
        var indices: [Int]
        var sizes: [CGSize]
        var height: CGFloat
        var width: CGFloat
    }

    private func computeRows(maxWidth: CGFloat, subviews: Subviews) -> [Row] {
        guard !subviews.isEmpty else { return [] }
        let limit = maxWidth.isFinite && maxWidth > 0 ? maxWidth : .infinity
        var rows: [Row] = []
        var currentIndices: [Int] = []
        var currentSizes: [CGSize] = []
        var currentWidth: CGFloat = 0
        var currentHeight: CGFloat = 0

        for index in subviews.indices {
            let ideal = subviews[index].sizeThatFits(.unspecified)
            let itemWidth = min(ideal.width, limit)
            let itemSize = itemWidth < ideal.width
                ? subviews[index].sizeThatFits(ProposedViewSize(width: itemWidth, height: nil))
                : ideal
            let needed = currentIndices.isEmpty ? itemSize.width : currentWidth + horizontalSpacing + itemSize.width
            if !currentIndices.isEmpty && needed > limit {
                rows.append(Row(indices: currentIndices, sizes: currentSizes, height: currentHeight, width: currentWidth))
                currentIndices = [index]
                currentSizes = [itemSize]
                currentWidth = itemSize.width
                currentHeight = itemSize.height
            } else {
                currentIndices.append(index)
                currentSizes.append(itemSize)
                currentWidth = needed
                currentHeight = max(currentHeight, itemSize.height)
            }
        }
        if !currentIndices.isEmpty {
            rows.append(Row(indices: currentIndices, sizes: currentSizes, height: currentHeight, width: currentWidth))
        }
        return rows
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        let rows = computeRows(maxWidth: maxWidth, subviews: subviews)
        let totalHeight = rows.reduce(CGFloat(0)) { $0 + $1.height } + CGFloat(max(0, rows.count - 1)) * verticalSpacing
        let maxRowWidth = rows.map(\.width).max() ?? 0
        let resolvedWidth: CGFloat
        if let proposedWidth = proposal.width, proposedWidth.isFinite {
            resolvedWidth = alignLastTrailingOnSingleLine ? max(proposedWidth, maxRowWidth) : min(proposedWidth, maxRowWidth)
        } else {
            resolvedWidth = maxRowWidth
        }
        return CGSize(width: resolvedWidth, height: totalHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let rows = computeRows(maxWidth: bounds.width, subviews: subviews)
        var y = bounds.minY
        for (rowIndex, row) in rows.enumerated() {
            var x = bounds.minX
            let pinLastTrailing = alignLastTrailingOnSingleLine && rows.count == 1 && row.indices.count >= 2
            for (pos, subviewIndex) in row.indices.enumerated() {
                let size = row.sizes[pos]
                let itemY = y + (row.height - size.height) / 2
                if pinLastTrailing && pos == row.indices.count - 1 {
                    let trailingX = max(x, bounds.maxX - size.width)
                    subviews[subviewIndex].place(
                        at: CGPoint(x: trailingX, y: itemY),
                        proposal: ProposedViewSize(width: size.width, height: size.height)
                    )
                } else {
                    subviews[subviewIndex].place(
                        at: CGPoint(x: x, y: itemY),
                        proposal: ProposedViewSize(width: size.width, height: size.height)
                    )
                    x += size.width + horizontalSpacing
                }
            }
            if rowIndex < rows.count - 1 {
                y += row.height + verticalSpacing
            }
        }
    }
}

struct PageHeading: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(subtitle).foregroundStyle(.secondary).accessibilityLabel("\(title)：\(subtitle)")
        }.frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, 8)
    }
}
struct Metric: View {
    let title: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.monospacedDigit().weight(.semibold))
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
struct JobProgressView: View {
    let job: Job
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(job.compute.map { "\(job.kindLabel) · \($0.name)" } ?? job.kindLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(job.stage.isEmpty ? job.statusLabel : job.stage)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Text(job.progress, format: .percent.precision(.fractionLength(0)))
                    .monospacedDigit()
            }
            ProgressView(value: job.progress)
        }.font(.callout)
    }
}
struct CandidateCard: View {
    @Environment(StudioStore.self) private var store
    let item: AudioItem
    var best = false
    var selected = false
    var onSelect: () -> Void = {}
    var onDebug: (() -> Void)?
    var extra: (() -> AnyView)?
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) {
                    headerPrimary
                    Spacer(minLength: 4)
                    headerMetaAndMenu
                    if let extra { extra().fixedSize() }
                }
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        headerPrimary
                        Spacer(minLength: 4)
                        headerMetaAndMenu
                    }
                    if let extra {
                        HStack {
                            Spacer(minLength: 0)
                            extra()
                        }
                    }
                }
            }
            Text(item.asr_text ?? item.text).textSelection(.enabled).lineLimit(3)
            HStack {
                Metric(title: "评分", value: displayScore(item.score)).help(scoreHelp(item.score))
                Metric(title: "读音准确", value: displayAccuracy(item.cer_kana ?? item.cer))
                Metric(title: "音色相似", value: displaySimilarity(item.spk_sim))
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 5) {
                    ratingStars
                    Spacer(minLength: 4)
                    reviewActions
                }
                VStack(alignment: .leading, spacing: 6) {
                    ratingStars
                    HStack(spacing: 8) {
                        Spacer(minLength: 0)
                        reviewActions
                    }
                }
            }
            if let tags = item.review?.tags, !tags.isEmpty { Text(tags.joined(separator: " · ")).font(.caption).foregroundStyle(.orange) }
        }
        .padding(14)
        .background(selected ? Color.accentColor.opacity(0.09) : Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(best ? Color.accentColor.opacity(0.5) : .clear))
        .contentShape(Rectangle()).onTapGesture(perform: onSelect)
    }

    @ViewBuilder
    private var headerPrimary: some View {
        Button { store.perform { try await store.play(item) } } label: {
            Image(systemName: store.audio.currentID == item.id && store.audio.playing ? "pause.fill" : "play.fill")
                .frame(width: 22, height: 22)
        }.help("播放 / 暂停").accessibilityLabel("播放候选 \(item.id)")
        if best {
            Label("最佳", systemImage: "sparkles")
                .font(.caption.bold())
                .foregroundStyle(.tint)
                .fixedSize()
        }
        Text("\(characterName(item.character)) · \(displayEngine(item.engine))")
            .font(.callout)
            .lineLimit(1)
    }

    @ViewBuilder
    private var headerMetaAndMenu: some View {
        Text("\(displaySeconds(item.duration)) 秒")
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
            .fixedSize()
        Menu {
            Text("生成编号：\(item.seed)")
            Divider()
            Button("复用参数与种子", systemImage: "arrow.uturn.backward") {
                store.reuseHistoryItem(item)
            }
            Button("导出 \(store.defaultExportFormat.label)…", systemImage: "square.and.arrow.up") {
                store.perform { try await store.exportAudio(item, format: store.defaultExportFormat.rawValue) }
            }
            Menu("导出其他格式") {
                ForEach(ExportFormat.allCases.filter { $0 != store.defaultExportFormat }) { format in
                    Button("\(format.label)…") {
                        store.perform { try await store.exportAudio(item, format: format.rawValue) }
                    }
                }
            }
            if let onDebug {
                Divider()
                Button("生成详情", systemImage: "waveform.path.ecg", action: onDebug)
                    .accessibilityIdentifier("debug-\(item.id)")
            }
        } label: { Text("⋯") }.menuStyle(.borderlessButton).fixedSize()
    }

    private var ratingStars: some View {
        HStack(spacing: 5) {
            Text("评价").font(.caption).foregroundStyle(.secondary).fixedSize()
            ForEach(1...5, id: \.self) { rating in
                Button {
                    store.perform { try await store.setRating(item, rating: rating) }
                } label: {
                    Image(systemName: rating <= (item.review?.rating ?? 0) ? "star.fill" : "star")
                        .foregroundStyle(rating <= (item.review?.rating ?? 0) ? Color.orange : Color.secondary)
                }.buttonStyle(.plain).accessibilityLabel("给 \(item.id) 打 \(rating) 分")
            }
        }
    }

    @ViewBuilder
    private var reviewActions: some View {
        if item.review != nil {
            Button("清除评价") { store.perform { try await store.clearReview(item) } }
                .buttonStyle(.borderless)
                .fixedSize()
        }
        Menu {
            ForEach(problemTags, id: \.self) { tag in
                Button {
                    store.perform { try await store.toggleReviewTag(item, tag: tag) }
                } label: {
                    Label(tag, systemImage: item.review?.tags.contains(tag) == true ? "checkmark.circle.fill" : "circle")
                }
            }
        } label: { Label("问题标签", systemImage: "tag") }
        .menuStyle(.borderlessButton).fixedSize()
    }
}
