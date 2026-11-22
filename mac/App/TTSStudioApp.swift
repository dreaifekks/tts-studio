import SwiftUI

@main
struct TTSStudioApp: App {
    @State private var store = StudioStore.shared
    var body: some Scene {
        WindowGroup("TTS Studio") { ContentView().environment(store) }
            .defaultSize(width: 1260, height: 820)
            .commands {
                CommandGroup(after: .newItem) {
                    Button(store.generateMode == .compare ? "开始对比" : "出候选") { store.startPrimaryGeneration() }
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(
                            store.generationBusy
                            || !store.composerReady
                            || !store.hasValidEmotionReference
                            || store.params.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                            || (store.generateMode == .compare && store.comparisonEnginesList.count < 2)
                        )
                }
                CommandMenu("导航") {
                    ForEach(Array(StudioPage.allCases.enumerated()), id: \.element.id) { index, page in
                        Button(page.rawValue) { store.page = page }.keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: [.command, .option])
                    }
                }
            }
        Settings { SettingsView().environment(store) }
    }
}
