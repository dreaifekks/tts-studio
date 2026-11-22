import SwiftUI

struct SortControls: View {
    let options: [SortOption]
    @Binding var field: String
    @Binding var order: String
    var body: some View {
        Menu {
            Picker("排序字段", selection: $field) { ForEach(options) { Text($0.label).tag($0.id) } }
            Divider()
            Picker("顺序", selection: $order) { Text("升序").tag("asc"); Text("降序").tag("desc") }
        } label: {
            Label("\(options.first { $0.id == field }?.label ?? "排序") · \(order == "asc" ? "升序" : "降序")", systemImage: "arrow.up.arrow.down")
        }.fixedSize().accessibilityLabel("列表排序")
    }
}
struct EngineChoice: View {
    let engine: EngineInfo
    var installed: Bool = true
    var body: some View {
        Text(installed ? engine.label : "\(displayEngine(engine.id))（当前算力未安装）")
            .tag(engine.id)
    }
}
