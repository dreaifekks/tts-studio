import SwiftUI

struct ContentView: View {
    @Environment(StudioStore.self) private var store
    @Environment(\.openSettings) private var openSettings
    var body: some View {
        @Bindable var store = store
        NavigationSplitView {
            VStack(alignment: .leading, spacing: 0) {
                Label("TTS Studio", systemImage: "waveform.circle.fill").font(.title3.bold()).padding(18)
                List(StudioPage.allCases, selection: $store.page) { page in
                    Label(page.rawValue, systemImage: page.symbol).tag(page)
                }.listStyle(.sidebar)
                if !store.params.character.isEmpty, store.models.contains(where: { $0.slug == store.params.character }) {
                    Divider().padding(.horizontal, 12)
                    Button {
                        store.openCharacter(store.params.character, tab: .versions)
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "person.crop.circle")
                                .foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 1) {
                                Text("当前角色").font(.caption2).foregroundStyle(.secondary)
                                Text(characterName(store.params.character)).font(.callout).fontWeight(.medium).lineLimit(1)
                            }
                            Spacer(minLength: 0)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 16)
                    .padding(.top, 10)
                    .help("打开当前角色管理")
                }
                SettingsLink { HStack { Label("设置", systemImage: "gearshape"); if store.versions.updateAvailable { Circle().fill(.red).frame(width: 6, height: 6).accessibilityLabel("有新版本") } } }.buttonStyle(.plain).padding(18)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(store.versions.updateAvailable ? "设置，有新版本" : "设置")
            }.navigationSplitViewColumnWidth(min: 155, ideal: 180, max: 230)
        } detail: {
            VStack(spacing: 0) {
                if let error = store.errorMessage {
                    HStack(alignment: .top) {
                        Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                        Text(error).textSelection(.enabled).font(.callout)
                        Spacer()
                        Button { store.errorMessage = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).help("收起错误").accessibilityLabel("收起错误")
                    }.padding(12).background(.orange.opacity(0.1))
                }
                Group {
                    if store.connection.configured {
                        switch store.page {
                        case .generate: GenerateView()
                        case .works: WorksView()
                        case .characters: CharactersView()
                        case .training: TrainingView()
                        }
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                    .overlay {
                        if !store.connected && store.connection.configured { ConnectionUnavailableView() }
                    }
                Divider()
                HStack(spacing: 10) {
                    Circle().fill(store.connected ? .green : .orange).frame(width: 7, height: 7)
                    Text(store.connected ? "已连接" : "未连接").fixedSize()
                    Text(store.computeSummary).lineLimit(1).truncationMode(.tail)
                    if let id = store.trainingJobID { Label("训练中", systemImage: "cpu").foregroundStyle(.orange).fixedSize().help(id) }
                    Divider().frame(height: 12)
                    Text(store.waitingStatus ?? store.status).lineLimit(1).truncationMode(.tail).textSelection(.enabled)
                    Spacer(minLength: 4)
                    if store.audio.playing { Button("暂停", systemImage: "pause.fill") { store.audio.toggle() }.buttonStyle(.plain).fixedSize() }
                    Menu {
                        if store.activeJobs.isEmpty { Text("无运行中任务") }
                        ForEach(Array(Set(store.activeJobs.map { $0.compute?.name ?? "等待分配" })).sorted(), id: \.self) { name in
                            Section(name) { ForEach(store.activeJobs.filter { ($0.compute?.name ?? "等待分配") == name }) { job in jobStatus(job) } }
                        }
                    } label: {
                        HStack {
                            Text("\(store.activeJobs.count) 个任务运行中")
                        }
                    }.menuStyle(.borderlessButton).fixedSize()
                }.font(.caption).foregroundStyle(.secondary).padding(.horizontal, 16).padding(.vertical, 10)
            }
            .navigationTitle(store.page.rawValue)
            .toolbar { ToolbarItem { Button { store.perform { await store.refreshConnection() } } label: { Image(systemName: "arrow.clockwise") }.help("刷新连接").accessibilityLabel("刷新连接") } }
        }
        .frame(minWidth: 960, minHeight: 660)
        .sheet(isPresented: Binding(get: { !store.connection.configured }, set: { _ in })) { IdentitySetupView() }
        .onChange(of: store.needsLibrarySelection) { if store.needsLibrarySelection { if store.shouldPromptLocalLibrarySelection { openSettings() }; store.needsLibrarySelection = false } }
        .task(id: store.connection.configured) { if store.connection.configured { await store.loadInitial() } }
        .task {
            while !Task.isCancelled {
                if store.connection.configured { await store.refreshConnection() }
                if store.connected && !store.versions.serverUpdating { await store.versions.readCurrent(api: store.api) }
                do { try await Task.sleep(for: .seconds(15)) } catch { break }
            }
        }
    }
    private func jobStatus(_ job: Job) -> some View {
        Text("\(job.kindLabel) · \(job.stage) · \(Int(job.progress * 100))%")
    }

}

struct ConnectionUnavailableView: View {
    @Environment(StudioStore.self) private var store
    @State private var showCoreInstaller = false
    var body: some View {
        ZStack {
            Rectangle().fill(.regularMaterial)
            VStack(spacing: 18) {
                ContentUnavailableView(store.unavailableTitle, systemImage: "desktopcomputer", description: Text(store.unavailableDescription))
                HStack(spacing: 12) {
                    if store.connection.localCoreMissing {
                        Button(store.coreInstaller.isRunning ? CoreInstallCopy.viewProgressButton : CoreInstallCopy.oneClickInstallButton) {
                            showCoreInstaller = true
                        }
                        .buttonStyle(.borderedProminent)
                        SettingsLink { Text("打开设置") }
                    }
                    Button("重新连接") { store.connection.retryWake(); store.perform { await store.refreshConnection() } }
                }
            }.padding(24)
        }
        .sheet(isPresented: $showCoreInstaller) {
            CoreInstallView(mode: .install)
        }
    }
}
