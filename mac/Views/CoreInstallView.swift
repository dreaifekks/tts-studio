import SwiftUI
import AppKit

struct CoreInstallView: View {
    @Environment(StudioStore.self) private var studio
    @Environment(\.dismiss) private var dismiss

    var mode: CoreInstallMode

    @State private var selectedEngines: Set<String> = ["v2proplus", "v2pro"]
    @State private var initializedEngines = false
    @State private var showDetails = false
    @State private var copied = false
    @State private var copiedDiagnostics = false

    private var installer: CoreInstaller { studio.coreInstaller }

    private var orderedEngines: [String] {
        CoreInstaller.availableEngines.filter { selectedEngines.contains($0) }
    }

    private var targetPort: Int? {
        if let installed = installer.detectInstalledPort() {
            return installed
        }
        return mode == .install ? studio.connection.localPort : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            switch installer.state {
            case .idle, .cancelled:
                readyContent
            case .running(let step, let total, let progress, let stage, let startedAt, let canCancel):
                runningContent(
                    step: step,
                    total: total,
                    progress: progress,
                    stage: stage,
                    startedAt: startedAt,
                    canCancel: canCancel
                )
            case .succeeded(let version, _):
                succeededContent(version: version)
            case .failed(let message):
                failedContent(message: message)
            }
        }
        .padding(24)
        .frame(width: 540)
        .onChange(of: installer.state) { _, newState in
            if Self.shouldCollapseDetails(for: newState) {
                showDetails = false
            }
        }
        .onAppear {
            if !initializedEngines {
                initializedEngines = true
                let preferred = studio.preferredEngine
                if CoreInstaller.availableEngines.contains(preferred) {
                    selectedEngines.insert(preferred)
                }
            }
            if Self.shouldCollapseDetails(for: installer.state) {
                showDetails = false
            }
        }
    }

    nonisolated static func shouldCollapseDetails(for state: CoreInstallState) -> Bool {
        if case .failed = state { return true }
        return false
    }

    @ViewBuilder
    private var readyContent: some View {
        HStack(spacing: 12) {
            Image(systemName: mode == .update ? "arrow.triangle.2.circlepath.circle.fill" : "desktopcomputer.and.arrow.down")
                .font(.title)
                .foregroundStyle(Color.accentColor)
            Text(mode == .update ? CoreInstallCopy.updateTitle : CoreInstallCopy.installTitle)
                .font(.title2.bold())
        }

        Text(mode == .update ? CoreInstallCopy.updateDescription : CoreInstallCopy.installDescription)
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

        if case .cancelled = installer.state {
            Label(CoreInstallCopy.cancelledNotice, systemImage: "info.circle")
                .font(.caption)
                .foregroundStyle(.orange)
        }

        VStack(alignment: .leading, spacing: 10) {
            Text(CoreInstallCopy.engineSectionTitle)
                .font(.headline)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 105), alignment: .leading)], alignment: .leading, spacing: 8) {
                ForEach(CoreInstaller.availableEngines, id: \.self) { engine in
                    Toggle(
                        displayEngine(engine),
                        isOn: Binding(
                            get: { selectedEngines.contains(engine) },
                            set: { enabled in
                                if enabled {
                                    selectedEngines.insert(engine)
                                } else {
                                    selectedEngines.remove(engine)
                                }
                            }
                        )
                    )
                    .toggleStyle(.checkbox)
                }
            }
            Text(CoreInstallCopy.engineSelectionHint)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(.quaternary))

        HStack {
            copyCommandButton
            Spacer()
            Button("取消") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button(mode == .update ? CoreInstallCopy.startUpdateButton : CoreInstallCopy.startInstallButton) {
                startInstall()
            }
            .buttonStyle(.borderedProminent)
            .disabled(orderedEngines.isEmpty)
            .keyboardShortcut(.defaultAction)
        }
    }

    @ViewBuilder
    private func runningContent(
        step: Int,
        total: Int,
        progress: Double,
        stage: String,
        startedAt: Date,
        canCancel: Bool
    ) -> some View {
        HStack(spacing: 12) {
            ProgressView()
                .controlSize(.small)
            Text(mode == .update ? CoreInstallCopy.runningUpdateTitle : CoreInstallCopy.runningInstallTitle)
                .font(.title2.bold())
        }

        VStack(alignment: .leading, spacing: 8) {
            ProgressView(value: progress, total: 1.0)
                .progressViewStyle(.linear)
            HStack {
                Text("第 \(step)/\(total) 步 · \(stage)")
                    .font(.callout)
                Spacer()
                TimelineView(.periodic(from: startedAt, by: 1)) { context in
                    Text("已用时 \(Self.formatElapsed(from: startedAt, to: context.date))")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            Text(canCancel ? CoreInstallCopy.backgroundHint : CoreInstallCopy.switchingHint)
                .font(.caption)
                .foregroundStyle(.secondary)
        }

        logDetailsDisclosure

        HStack {
            Button("取消", role: .destructive) {
                installer.cancel()
            }
            .disabled(!canCancel)
            Spacer()
            Button("收起") { dismiss() }
        }
    }

    @ViewBuilder
    private func succeededContent(version: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill")
                .font(.title)
                .foregroundStyle(.green)
            Text(CoreInstallCopy.succeededTitle)
                .font(.title2.bold())
        }

        let displayVersion = version.isEmpty ? studio.versions.appVersion : version
        Text(CoreInstallCopy.succeededSubtitle(version: displayVersion))
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

        if !installer.logs.isEmpty {
            logDetailsDisclosure
        }

        HStack {
            Spacer()
            Button("完成") {
                installer.reset()
                dismiss()
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
        }
    }

    @ViewBuilder
    private func failedContent(message: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title)
                .foregroundStyle(.orange)
            Text(CoreInstallCopy.failedTitle)
                .font(.title2.bold())
        }

        Text(message)
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

        logDetailsDisclosure

        HStack {
            Button(copiedDiagnostics ? CoreInstallCopy.copiedDiagnosticsButton : CoreInstallCopy.copyDiagnosticsButton) {
                NSPasteboard.general.clearContents()
                copiedDiagnostics = NSPasteboard.general.setString(installer.diagnosticsText(), forType: .string)
            }
            copyCommandButton
            Spacer()
            Button("关闭") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("重试") {
                startInstall()
            }
            .buttonStyle(.borderedProminent)
            .disabled(orderedEngines.isEmpty)
            .keyboardShortcut(.defaultAction)
        }
    }

    private var copyCommandButton: some View {
        Button(copied ? CoreInstallCopy.copiedCommandButton : CoreInstallCopy.copyCommandButton) {
            let cmd = installer.shellCommand(port: targetPort, engines: orderedEngines)
            NSPasteboard.general.clearContents()
            copied = NSPasteboard.general.setString(cmd, forType: .string)
        }
    }

    private var logDetailsDisclosure: some View {
        DisclosureGroup(CoreInstallCopy.detailsLabel, isExpanded: $showDetails) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        if installer.logs.isEmpty {
                            Text("等待输出…")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(Array(installer.logs.enumerated()), id: \.offset) { idx, line in
                                Text(line)
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .id(idx)
                            }
                        }
                    }
                    .padding(10)
                }
                .frame(height: 180)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                .onChange(of: installer.logs.count) { _, newCount in
                    if newCount > 0 {
                        proxy.scrollTo(newCount - 1, anchor: .bottom)
                    }
                }
            }
            .padding(.top, 6)
        }
    }

    private func startInstall() {
        copied = false
        copiedDiagnostics = false
        showDetails = false
        installer.start(port: targetPort, engines: orderedEngines)
    }

    static func formatElapsed(from start: Date, to now: Date) -> String {
        let totalSeconds = max(0, Int(now.timeIntervalSince(start)))
        let minutes = totalSeconds / 60
        let seconds = totalSeconds % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }
}
