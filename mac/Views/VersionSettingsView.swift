import SwiftUI

struct VersionSettingsView: View {
    @Environment(StudioStore.self) private var studio
    @State private var confirmMac = false
    @State private var showCoreInstaller = false
    @State private var coreInstallMode: CoreInstallMode = .install
    var body: some View {
        let versions = studio.versions
        let localInstalled = studio.connection.isLocalCoreInstalled || (studio.connected && !studio.connection.isClient)
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("TTS Studio")
                Text(versions.appVersion).foregroundStyle(.secondary)
                if let latest = versions.latest?.studio.latest {
                    Text(versions.macAvailable ? "新版本 \(latest)" : "已是最新").font(.caption).foregroundStyle(versions.macAvailable ? Color.accentColor : .green)
                }
                Spacer()
                Button(versions.checking ? "正在检查…" : "检查更新") { Task { await versions.checkLatest(api: studio.api, refresh: true) } }.disabled(versions.checking)
            }
            if versions.macAvailable, let release {
                HStack {
                    Button("更新到 \(release.version)…") { confirmMac = true }
                    Button("复制更新命令") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(TerminalUpdater.shellCommand(tag: release.tag), forType: .string) }
                    Link("更新说明", destination: release.url)
                }
            }
            if !studio.connection.isClient {
                Divider()
                HStack(alignment: .center, spacing: 10) {
                    Text("本机主控服务")
                    if studio.coreInstaller.isRunning {
                        Text("正在处理…").font(.caption).foregroundStyle(Color.accentColor)
                    } else if !localInstalled {
                        Text("未安装").font(.caption).foregroundStyle(.orange)
                    } else if let serverVersion = versions.serverVersion {
                        Text(serverVersion).foregroundStyle(.secondary)
                        Text(versions.localCoreVersionMismatch ? "版本不一致" : "已同步")
                            .font(.caption)
                            .foregroundStyle(versions.localCoreVersionMismatch ? .orange : .green)
                    } else {
                        Text("已安装").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if studio.coreInstaller.isRunning {
                        Button(CoreInstallCopy.viewProgressButton) {
                            coreInstallMode = localInstalled ? .update : .install
                            showCoreInstaller = true
                        }
                    } else if !localInstalled {
                        Button(CoreInstallCopy.installServiceButton) {
                            coreInstallMode = .install
                            showCoreInstaller = true
                        }
                        .buttonStyle(.borderedProminent)
                    } else {
                        if versions.localCoreVersionMismatch {
                            Button(CoreInstallCopy.syncUpdateButton(appVersion: versions.appVersion)) {
                                coreInstallMode = .update
                                showCoreInstaller = true
                            }
                            .buttonStyle(.borderedProminent)
                        }
                        Button(CoreInstallCopy.reinstallOrModelsButton) {
                            coreInstallMode = .update
                            showCoreInstaller = true
                        }
                    }
                }
            }
            if let error = versions.error { Text(error).font(.caption).foregroundStyle(.orange) }
            if !versions.macProgress.isEmpty { Text(versions.macProgress).font(.callout) }
        }
        .alert("在终端更新 TTS Studio？", isPresented: $confirmMac) {
            Button("取消", role: .cancel) { }
            if let release { Button("打开终端") { Task { await versions.updateMac(release: release) } } }
        } message: { Text("终端会获取新版本、构建并安装。完成后退出旧版再打开新版。") }
        .sheet(isPresented: $showCoreInstaller) {
            CoreInstallView(mode: coreInstallMode)
        }
        .task { await versions.checkLatest(api: studio.api) }
    }
    private var release: ReleaseInfo? {
        guard let version = studio.versions.latest?.studio.latest else { return nil }
        let tag = version.hasPrefix("v") ? version : "v" + version
        return ReleaseInfo(latest: version, url: studio.versions.latest?.studio.notes_url ?? URL(string: "https://github.com/dreaifekks/tts-studio/releases/tag/\(tag)")!, published_at: "", notes: "")
    }
}
