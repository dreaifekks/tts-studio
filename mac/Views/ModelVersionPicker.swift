import SwiftUI

struct ModelVersionPicker: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.openSettings) private var openSettings
    var rows: [ModelVersion]
    @Binding var selection: String?
    var loading = false
    var manual = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("模型版本", selection: $selection) {
                Text(manual ? "更多设置中的自定组合" : "选择模型版本").tag(String?.none)
                ModelVersionOptions(rows: rows)
            }
            if let version = rows.first(where: { $0.id == selection }), let score = version.scoreLabel { Text(score).font(.caption).foregroundStyle(.secondary).help(scoreHelp(version.score)) }
            if loading { ProgressView("正在读取模型版本…").controlSize(.small) }
            else if rows.isEmpty {
                Text("这个角色还没有能用的模型版本。零样本不需要训练，但也需要底模。").font(.caption).foregroundStyle(.secondary)
                Button("查看算力") { studio.settingsTab = .compute; openSettings() }.buttonStyle(.link)
            } else if rows.first(where: { $0.id == selection })?.zeroShot == true {
                Text("底模（零样本）：使用参考音频的声音生成。").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct ModelVersionOptions: View {
    var rows: [ModelVersion]
    var body: some View {
        ForEach(["hard", "mini", "none"], id: \.self) { set in
            let values = rows.filter { ($0.score_set ?? "none") == set }
            if !values.isEmpty {
                Section(set == "none" ? "尚未评测" : displayEvalSet(set)) {
                    ForEach(values) { version in
                        Text(version.optionLabel).help(scoreHelp(version.score)).tag(Optional(version.id))
                    }
                }
            }
        }
    }
}
