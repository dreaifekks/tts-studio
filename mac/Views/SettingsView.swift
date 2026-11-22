import SwiftUI

// The main window and Settings share the destination used by “调整提醒线”.
enum SettingsTab: String, CaseIterable { case general = "通用", library = "资料库", compute = "算力", cloud = "云端 GPU" }
struct SettingsView: View {
    @Environment(StudioStore.self) private var studio
    var body: some View {
        @Bindable var studio = studio
        TabView(selection: $studio.settingsTab) {
            GeneralSettingsView().tabItem { Label("通用", systemImage: "gearshape") }.tag(SettingsTab.general)
            LibrarySettingsView().tabItem { Label("资料库", systemImage: "books.vertical") }.tag(SettingsTab.library)
            ComputeSettingsView().tabItem { Label("算力", systemImage: "cpu") }.tag(SettingsTab.compute)
            CloudSettingsView().tabItem { Label("云端 GPU", systemImage: "cloud") }.tag(SettingsTab.cloud)
        }.padding(.top, 8).frame(minWidth: 760, idealWidth: 920, minHeight: 620, idealHeight: 740)
            .task { await studio.compute.refresh(refreshAccounts: true) }
    }
}
struct SettingsPage<Content: View>: View {
    @Environment(StudioStore.self) private var studio
    var managed = true
    @ViewBuilder var content: () -> Content
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                content()
                if managed, let error = studio.compute.error { Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled) }
            }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
struct SettingsSection<Content: View>: View {
    var title: String
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            VStack(alignment: .leading, spacing: 14, content: content)
                .padding(16).frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(.quaternary))
        }
    }
}
struct GeneralSettingsView: View {
    @Environment(StudioStore.self) private var studio
    @State private var copied = false
    @AppStorage("defaultCandidateCount") private var defaultCandidateCount = 6
    @AppStorage("defaultExportFormat") private var defaultExportFormat = "wav"
    @AppStorage("tagKeys") private var tagKeys = "q,w,e,r,t"
    @AppStorage("tagNames") private var tagNames = "开心,生气,害羞,低落,平静"

    var body: some View {
        @Bindable var compute = studio.compute
        SettingsPage(managed: false) {
            SettingsSection(title: "这台 Studio") {
                ConnectionEditor(firstLaunch: false)
                if let info = studio.compute.systemInfo, info.web_available {
                    Divider()
                    HStack {
                        VStack(alignment: .leading, spacing: 4) { Text("在浏览器里用"); Text("手机、平板或其他电脑都可以用浏览器打开").font(.caption).foregroundStyle(.secondary) }
                        Spacer()
                        Button(copied ? "已复制" : "复制链接") {
                            NSPasteboard.general.clearContents()
                            copied = NSPasteboard.general.setString(info.web_url.absoluteString, forType: .string)
                            if !copied { studio.errorMessage = "链接没有复制成功。" }
                        }
                    }
                }
            }
            SettingsSection(title: "合成与打标偏好") {
                HStack {
                    Text("默认候选数")
                    Spacer()
                    Stepper("\(defaultCandidateCount) 个", value: $defaultCandidateCount, in: 1...16)
                        .onChange(of: defaultCandidateCount) {
                            if !studio.generationBusy { studio.params.n = defaultCandidateCount }
                        }
                }
                HStack {
                    Text("默认导出格式")
                    Spacer()
                    Picker("默认导出格式", selection: $defaultExportFormat) {
                        ForEach(ExportFormat.allCases) { format in
                            Text(format.label).tag(format.rawValue)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 210)
                }
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("常用情绪速标快捷键").font(.callout.weight(.medium))
                        Spacer()
                        Button("恢复默认") {
                            tagKeys = "q,w,e,r,t"
                            tagNames = "开心,生气,害羞,低落,平静"
                        }
                        .controlSize(.small)
                    }
                    HStack(spacing: 10) {
                        TextField("按键（逗号分隔）", text: $tagKeys)
                        TextField("对应情绪标签（逗号分隔）", text: $tagNames)
                    }
                    .textFieldStyle(.roundedBorder)
                    Text("在「角色 → 参考音频」中使用这些按键快速打标并自动试听下一条。").font(.caption).foregroundStyle(.secondary)
                }
            }
            SettingsSection(title: "常用模型") {
                let knownEngines = studio.allKnownEngines.isEmpty ? sortedEngines(compute.commonModels) : studio.allKnownEngines
                let installedSet = studio.installedComputeEngines
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)], alignment: .leading, spacing: 8) {
                    ForEach(knownEngines, id: \.self) { engine in
                        let isInstalled = installedSet.isEmpty || installedSet.contains(engine)
                        Toggle(isOn: Binding(get: { compute.commonModels.contains(engine) }, set: { enabled in
                            if enabled { compute.commonModels.insert(engine) } else { compute.commonModels.remove(engine) }
                        })) {
                            if isInstalled {
                                Text(displayEngine(engine))
                            } else {
                                HStack(spacing: 4) {
                                    Text(displayEngine(engine))
                                    Text("未安装").font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .toggleStyle(.checkbox)
                    }
                }
                Text("列表收起时只显示这些模型的速度，新建训练、作品行等默认引擎也会优先跟随此处偏好；展开一行可查看全部模型与训练/打分速度。").font(.caption).foregroundStyle(.secondary)
            }
            SettingsSection(title: "版本") { VersionSettingsView() }
            SettingsSection(title: "音频保留") { Text("没有加入作品、也没有评价的音频，7 天后自动删除。").font(.callout) }
        }
    }
}
struct ConnectionEditor: View {
    @Environment(StudioStore.self) private var studio
    var firstLaunch: Bool
    var onSave: () -> Void = {}
    @State private var role = "control"
    @State private var address = ""
    @State private var alias = "主控"
    @State private var wake = ""
    @State private var broadcast = "192.168.1.255"
    @State private var error: String?
    @State private var showCoreInstaller = false
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Picker("用法", selection: $role) { Text("作为主控").tag("control"); Text("连接到另一台").tag("client") }.pickerStyle(.segmented)
            Text(role == "control" ? "所有任务都由主控统一安排，生成也可以直接在这台上跑。其他电脑上的 Studio 选「连接到另一台」，看到的内容和这里一样。" : "看到的任务、资料库和设备与主控相同。").font(.caption).foregroundStyle(.secondary)
            if role == "control" && !studio.connection.isLocalCoreInstalled && !(studio.connected && !studio.connection.isClient) {
                HStack(alignment: .center, spacing: 10) {
                    Label(studio.connection.localCoreNotice, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                    Spacer()
                    Button(studio.coreInstaller.isRunning ? CoreInstallCopy.viewProgressButton : CoreInstallCopy.oneClickInstallButton) {
                        showCoreInstaller = true
                    }
                    .controlSize(.small)
                }
            }
            if role == "client" {
                TextField("主控别名", text: $alias)
                TextField("主控地址", text: $address)
                DisclosureGroup("唤醒设置") {
                    Text("主控睡眠时，连接前先唤醒。").font(.caption).foregroundStyle(.secondary)
                    TextField("唤醒 MAC（可选）", text: $wake)
                    TextField("广播地址", text: $broadcast)
                }
            }
            HStack {
                if !firstLaunch { Text(studio.connected ? "已连接" : studio.computeSummary).font(.caption).foregroundStyle(.secondary) }
                Spacer()
                Button(firstLaunch ? "开始使用" : "保存") {
                    do {
                        try studio.applyConnection(role: role, address: address, alias: alias, wakeMAC: wake, broadcast: broadcast)
                        error = nil; onSave()
                    } catch { self.error = error.localizedDescription }
                }.buttonStyle(.borderedProminent)
            }
            if let error { Text(error).font(.callout).foregroundStyle(.red) }
        }.textFieldStyle(.roundedBorder)
            .onAppear {
                let connection = studio.connection
                role = connection.role; address = connection.address
                alias = connection.alias; wake = connection.wakeMAC; broadcast = connection.broadcast
            }
            .sheet(isPresented: $showCoreInstaller) {
                CoreInstallView(mode: .install)
            }
    }
}
struct IdentitySetupView: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("这台 Studio 怎么用？").font(.title2.bold())
            ConnectionEditor(firstLaunch: true, onSave: { dismiss() })
        }.padding(28).frame(width: 510).interactiveDismissDisabled()
    }
}
struct LibrarySettingsView: View {
    @Environment(StudioStore.self) private var studio
    @State private var location = "drive"
    var body: some View {
        SettingsPage {
            SettingsSection(title: "位置") {
                if let library = studio.compute.library, library.selected {
                    LabeledContent("存放在", value: library.kind == "drive" ? "Google Drive" : "本机文件夹")
                    HStack {
                        Text("文件夹")
                        Text(displayPath(library.path)).foregroundStyle(.secondary).textSelection(.enabled)
                        Spacer()
                        if studio.connection.isClient {
                            Text(studio.connection.libraryFolderHint).font(.caption).foregroundStyle(.secondary)
                        } else {
                            Button("更改…") { chooseFolder() }
                        }
                    }
                    Text(library.kind == "drive" ? "资料库通过 Google Drive 电脑版同步。" : "资料库放角色、参考音频、模型、训练数据、评测和盲听记录。").font(.caption).foregroundStyle(.secondary)
                    if !library.owned {
                        Divider()
                        HStack { Text("由 \(library.owner?.name ?? "别的主控") 管理").foregroundStyle(.secondary); Spacer(); Button(studio.connection.isClient ? "改为 \(studio.connection.controllerName) 管理" : "改为本机管理") { studio.perform { try await studio.compute.claim() } } }
                    }
                } else {
                    Label("选一个文件夹，存放资料库", systemImage: "folder.badge.plus").font(.title3)
                    if !studio.connection.isClient {
                        Picker("存放在", selection: $location) { Text("Google Drive").tag("drive"); Text("本机文件夹").tag("local") }.pickerStyle(.segmented)
                    }
                    Text("角色、模型和训练数据都放在这里。").foregroundStyle(.secondary)
                    if studio.connection.isClient {
                        Text(studio.connection.libraryFolderHint).foregroundStyle(.secondary)
                    } else {
                        Button("选择文件夹…") { chooseFolder() }.buttonStyle(.borderedProminent)
                    }
                    if let error = studio.compute.library?.error { Text(error).foregroundStyle(.orange) }
                }
            }
            if let library = studio.compute.library, library.selected {
                SettingsSection(title: "内容") {
                    HStack {
                        Text("状态"); Label(studio.compute.libraryChecking ? "正在同步" : "已同步", systemImage: studio.compute.libraryChecking ? "arrow.triangle.2.circlepath" : "checkmark.circle.fill").foregroundStyle(.green)
                        if let date = library.scanned_at { Text(syncTime(date)).font(.caption).foregroundStyle(.secondary).help(date) }
                        Spacer(); Button("重新检查") { studio.perform { try await studio.compute.rescan() } }.disabled(studio.compute.libraryChecking)
                    }
                    if let counts = library.counts { LabeledContent("包含") { Text(counts.summary).multilineTextAlignment(.trailing) } }
                    if library.conflicts > 0 {
                        HStack {
                            Text("需要处理").foregroundStyle(.secondary)
                            Text("\(library.conflicts) 个文件有同步冲突").foregroundStyle(.orange)
                            Spacer()
                            if studio.connection.isClient {
                                Text(studio.connection.libraryConflictHint).font(.caption).foregroundStyle(.secondary)
                            } else {
                                Button("在 Finder 中显示") {
                                    if let path = library.path {
                                        NSWorkspace.shared.activateFileViewerSelecting(library.conflict_files.map { URL(filePath: path).appending(path: $0) })
                                    }
                                }
                            }
                        }
                    }
                    if !library.broken_files.isEmpty { Text("\(library.broken_files.count) 个文件暂时无法读取").foregroundStyle(.orange) }
                }
            }
            if studio.compute.storage != nil || studio.compute.storageLoading {
                SettingsSection(title: "空间占用") {
                    storageSectionContent
                }
            }
        }
        .task {
            if studio.compute.storage == nil && !studio.compute.storageLoading {
                await studio.compute.refreshStorage()
            }
        }
    }
    @ViewBuilder
    private var storageSectionContent: some View {
        WrappingHStack(horizontalSpacing: 12, verticalSpacing: 8, alignLastTrailingOnSingleLine: true) {
            if let measuredAt = studio.compute.storage?.measured_at, !measuredAt.isEmpty {
                Text("统计于 \(syncTime(measuredAt))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .help(measuredAt)
            } else {
                Text("资料库与本机磁盘的空间使用情况")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                if studio.compute.storageLoading {
                    ProgressView().controlSize(.small)
                }
                Button("重新统计") {
                    Task { await studio.compute.refreshStorage(refresh: true) }
                }
                .disabled(studio.compute.storageLoading)
                .fixedSize()
            }
        }
        if let storage = studio.compute.storage {
            if storage.library.selected {
                VStack(alignment: .leading, spacing: 10) {
                    WrappingHStack(horizontalSpacing: 12, verticalSpacing: 4, alignLastTrailingOnSingleLine: true) {
                        Text("资料库占用").font(.callout.weight(.semibold))
                        Text(storage.library.summaryLabel)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    StorageBreakdownBar(categories: storage.library.categories, totalBytes: storage.library.total_bytes)
                    StorageCategoryList(categories: storage.library.categories, isDrive: storage.library.isDrive)
                }
                Divider()
            }
            VStack(alignment: .leading, spacing: 10) {
                WrappingHStack(horizontalSpacing: 12, verticalSpacing: 4, alignLastTrailingOnSingleLine: true) {
                    Text(storage.local.sectionTitle).font(.callout.weight(.semibold))
                    Text("剩余 \(displayBytes(storage.disk.free_bytes)) / 共 \(displayBytes(storage.disk.total_bytes))")
                        .font(.callout)
                        .monospacedDigit()
                        .foregroundStyle(storage.disk.isLowSpace ? .orange : .secondary)
                }
                ProgressView(value: storage.disk.usedFraction)
                    .tint(storage.disk.isLowSpace ? .orange : .accentColor)
                    .accessibilityLabel("本机磁盘已用比例")
                if storage.disk.isLowSpace {
                    Label("磁盘剩余空间不足 \(displayBytes(StorageOverview.lowDiskThresholdBytes))，建议清理暂存或释放空间。", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                if storage.local.total_bytes > 0 || !storage.local.categories.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        WrappingHStack(horizontalSpacing: 12, verticalSpacing: 4, alignLastTrailingOnSingleLine: true) {
                            Text("TTS Studio 本机占用").font(.callout).foregroundStyle(.secondary)
                            Text("共 \(displayBytes(storage.local.total_bytes))")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        StorageBreakdownBar(categories: storage.local.categories, totalBytes: storage.local.total_bytes)
                        StorageCategoryList(categories: storage.local.categories, isDrive: false)
                    }
                    .padding(.top, 4)
                }
            }
        } else {
            ProgressView("正在统计空间占用…").controlSize(.small)
        }
    }
    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.allowsMultipleSelection = false; panel.canCreateDirectories = true
        panel.prompt = "选择资料库"
        if location == "drive" {
            panel.directoryURL = Self.googleDrivePickerDirectory(home: FileManager.default.homeDirectoryForCurrentUser)
        }
        if let window = NSApp.keyWindow {
            panel.beginSheetModal(for: window) { result in
                guard result == .OK, let url = panel.url else { return }
                studio.perform { try await studio.compute.selectLibrary(url.path); try await studio.reloadModels() }
            }
        }
    }
    static func googleDrivePickerDirectory(home: URL) -> URL {
        let files = FileManager.default
        let cloudStorage = home.appending(path: "Library/CloudStorage")
        func isDirectory(_ url: URL) -> Bool {
            var directory: ObjCBool = false
            return files.fileExists(atPath: url.path, isDirectory: &directory) && directory.boolValue
        }

        guard isDirectory(cloudStorage) else { return home }
        let entries = (try? files.contentsOfDirectory(at: cloudStorage, includingPropertiesForKeys: nil)) ?? []
        guard let drive = entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
            .first(where: { $0.lastPathComponent.hasPrefix("GoogleDrive-") && isDirectory($0) })
        else { return cloudStorage }

        let myDrive = drive.appending(path: "My Drive")
        return isDirectory(myDrive) ? myDrive : drive
    }

    private func syncTime(_ string: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = formatter.date(from: string) ?? ISO8601DateFormatter().date(from: string) else { return string }
        return date.formatted(.relative(presentation: .named))
    }
    private func displayPath(_ path: String?) -> String {
        guard let path else { return "未选择" }
        if let range = path.range(of: "/My Drive/") { return "我的云端硬盘 › " + path[range.upperBound...].replacingOccurrences(of: "/", with: " › ") }
        return path.replacingOccurrences(of: FileManager.default.homeDirectoryForCurrentUser.path, with: "~")
    }
}

private enum StorageCategoryPalette {
    static let colors: [Color] = [.blue, .purple, .teal, .orange, .green, .pink, .indigo, .gray]
    static func color(at index: Int) -> Color {
        colors[index % colors.count]
    }
}

private struct StorageBreakdownBar: View {
    let categories: [StorageCategory]
    let totalBytes: Int64

    private var positiveTotal: Int64 {
        let sum = categories.reduce(Int64(0)) { $0 + max(0, $1.bytes) }
        return max(totalBytes, sum)
    }

    var body: some View {
        GeometryReader { proxy in
            let total = positiveTotal
            let width = max(0, proxy.size.width)
            let activeCount = categories.filter { $0.bytes > 0 }.count
            let usableWidth = max(0, width - CGFloat(max(0, activeCount - 1)))
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 5)
                    .fill(.quaternary.opacity(0.5))
                if total > 0 && activeCount > 0 {
                    HStack(spacing: 1) {
                        ForEach(Array(categories.enumerated()), id: \.element.id) { index, category in
                            if category.bytes > 0 {
                                let fraction = Double(category.bytes) / Double(total)
                                Rectangle()
                                    .fill(StorageCategoryPalette.color(at: index))
                                    .frame(width: max(2, usableWidth * fraction))
                                    .help("\(category.label)：\(displayBytes(category.bytes))")
                            }
                        }
                    }
                    .frame(maxWidth: width, alignment: .leading)
                    .clipShape(RoundedRectangle(cornerRadius: 5))
                }
            }
        }
        .frame(height: 10)
    }
}

private struct StorageCategoryList: View {
    let categories: [StorageCategory]
    let isDrive: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(categories.enumerated()), id: \.element.id) { index, category in
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        categoryTitle(category, index: index)
                        Spacer(minLength: 8)
                        categoryMeta(category)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        categoryTitle(category, index: index)
                        HStack {
                            Spacer(minLength: 16)
                            categoryMeta(category)
                        }
                    }
                }
                .font(.callout)
            }
        }
    }

    private func categoryTitle(_ category: StorageCategory, index: Int) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(StorageCategoryPalette.color(at: index))
                .frame(width: 8, height: 8)
            Text(category.label)
                .lineLimit(1)
        }
    }

    private func categoryMeta(_ category: StorageCategory) -> some View {
        HStack(spacing: 10) {
            if let files = category.files {
                Text("\(files) 个文件")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .fixedSize()
            }
            Text(category.sizeLabel(isDrive: isDrive))
                .foregroundStyle(.primary)
                .monospacedDigit()
                .fixedSize()
        }
    }
}
