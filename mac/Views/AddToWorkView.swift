import SwiftUI

struct AddToWorkView: View {
    @Environment(StudioStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let item: AudioItem
    @State private var workID = ""
    @State private var mode = "新建一行"
    @State private var target: Work?
    @State private var lineID = ""
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("加入作品").font(.title2.bold())
            Text(item.text).lineLimit(3).foregroundStyle(.secondary)
            Picker("作品", selection: $workID) {
                Text("新建作品").tag("")
                ForEach(store.works) { Text($0.title).tag($0.id) }
            }
            Picker("加入方式", selection: $mode) {
                Text("新建一行").tag("新建一行")
                Text("挂到已有行").tag("挂到已有行")
            }.pickerStyle(.segmented).disabled(workID.isEmpty)
            if mode == "挂到已有行" && !workID.isEmpty {
                Picker("目标行", selection: $lineID) {
                    Text("请选择").tag("")
                    ForEach(Array((target?.lines ?? []).enumerated()), id: \.element.id) { index, line in
                        Text("\(index + 1). \(characterName(line.character)) · \(line.text.prefix(40))").tag(line.id)
                    }
                }
                Text("这条音频会加入该行候选并立即选用，行的台词与参数保持原样，字幕按行台词生成。").font(.caption).foregroundStyle(.secondary)
            } else { Text("直接沿用这条候选的台词、角色和参数，并选用此音频，无需重新生成。").font(.caption).foregroundStyle(.secondary) }
            if let error { Text(error).foregroundStyle(.red) }
            HStack {
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button(busy ? "加入中…" : "加入并选用") {
                    busy = true
                    Task {
                        defer { busy = false }
                        do {
                            try await store.addCandidateToWork(item, workID: workID.isEmpty ? nil : workID,
                                                              lineID: mode == "挂到已有行" ? lineID : nil)
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.buttonStyle(.borderedProminent)
                .disabled(busy || store.workBusy || (mode == "挂到已有行" && lineID.isEmpty))
            }
        }.padding(24).frame(width: 570)
        .task {
            do { try await store.loadWorks(); workID = store.work?.id ?? "" }
            catch { self.error = error.localizedDescription }
        }
        .task(id: workID) {
            lineID = ""
            target = nil
            guard !workID.isEmpty else { mode = "新建一行"; return }
            do {
                if store.work?.id == workID { target = store.work }
                else { target = try await store.api.get("/works/\(workID)") }
                try Task.checkCancellation()
                lineID = target?.lines.first?.id ?? ""
            } catch is CancellationError { }
            catch { self.error = error.localizedDescription }
        }
    }
}
