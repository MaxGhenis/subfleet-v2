// Subfleet: the menu bar quota panel (width 430) over the status.json model.

#if !SUBFLEET_MODEL_TEST
import AppKit
import ServiceManagement
import SwiftUI

// MARK: - Read-only store

@MainActor
final class QuotaStore: ObservableObject {
    @Published var snap: Snapshot?
    @Published var loadedAt = Date()
    @Published var readError: String?
    @Published var reloadMessage: String?
    @Published var loginItem = SMAppService.mainApp.status == .enabled
    @Published var loginError: String?
    let url: URL
    private var timer: Timer?

    /// Set when this is the development build pointed at ~/.subfleet (D-21):
    /// the snapshot there is not read.
    let refusal: String?

    /// The snapshot of the resolved endpoint (C-29.1), or an explicit file.
    init(url: URL? = nil, automaticallyReload: Bool = true) {
        if let url {
            self.url = url
            refusal = nil
        } else {
            switch resolveDaemonEndpoint() {
            case .ready(let endpoint):
                self.url = endpoint.statusURL
                refusal = nil
            case .refused(let root, let reason):
                self.url = root.appendingPathComponent("status.json")
                refusal = reason
            }
        }
        load()
        if automaticallyReload {
            timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.load() }
            }
        }
    }

    func reload() {
        load()
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mm:ssa"
        let checked = formatter.string(from: loadedAt).lowercased()
        reloadMessage = readError == nil ? "Snapshot reloaded at \(checked)." : "Reload failed at \(checked)."
    }

    func load() {
        loadedAt = Date()
        if let refusal {
            snap = nil
            readError = refusal
            return
        }
        do {
            snap = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: url))
            readError = nil
        } catch {
            snap = nil
            if (error as NSError).domain == NSCocoaErrorDomain,
               (error as NSError).code == NSFileReadNoSuchFileError {
                readError = "The daemon has not written a status snapshot yet."
            } else {
                readError = "The daemon snapshot could not be read."
            }
        }
    }

    func setStartAtLogin(_ enabled: Bool) {
        do {
            try enabled ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
            loginItem = SMAppService.mainApp.status == .enabled
            loginError = nil
        } catch {
            loginError = "Could not change the login setting. Check System Settings."
            loginItem = SMAppService.mainApp.status == .enabled
        }
    }

    var barLabel: String {
        guard let snap else { return "–/–" }
        let count = snap.offline == true || snap.isStale(now: loadedAt)
            ? "–" : String(snap.codex.fleet.dispatchable_now)
        return "\(count)/\(snap.codex.fleet.total_homes)"
    }

    var hasProblem: Bool {
        guard let snap else { return true }
        if snap.offline == true || snap.isStale(now: loadedAt) || snap.codex.fleet.dispatchable_now == 0 { return true }
        let rows = snap.codex.homes.map { codexDisplay($0, snapshot: snap, now: loadedAt) }
            + (snap.claude.accounts ?? []).map { claudeDisplay($0, snapshot: snap, now: loadedAt) }
        return rows.contains { $0.tone == .error || $0.tone == .warning }
    }
}

func clock(_ iso: String?) -> String {
    guard let date = snapshotDate(iso) else { return "unknown" }
    return clock(date)
}

func clock(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.dateFormat = Calendar.current.isDateInToday(date) ? "h:mma" : "h:mma EEE"
    return formatter.string(from: date).lowercased()
}

func shortHome(_ home: String) -> String {
    let prefix = NSHomeDirectory() + "/"
    return home.hasPrefix(prefix) ? "~/" + home.dropFirst(prefix.count) : home
}

func toneColor(_ tone: LaneTone) -> Color {
    switch tone {
    case .good: return .green
    case .warning: return .orange
    case .error: return .red
    case .neutral: return .gray
    }
}

// MARK: - Native menu bar views

struct UsageBar: View {
    let pct: Double
    let stale: Bool
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule().fill(stale ? Color.gray : pct >= 95 ? .red : pct >= 75 ? .orange : .green)
                    .frame(width: max(3, geo.size.width * pct / 100))
            }
        }.frame(width: 48, height: 5)
    }
}

struct LaneRow: View {
    let name: String
    let subtitle: String
    let display: LaneDisplay

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle().fill(toneColor(display.tone)).frame(width: 7, height: 7).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.system(.body, design: .rounded).weight(.medium))
                if !subtitle.isEmpty { Text(subtitle).font(.caption2).foregroundStyle(.secondary) }
                if !display.detail.isEmpty { Text(display.detail).font(.caption2).foregroundStyle(.secondary) }
                HStack(spacing: 6) {
                    if let pct = display.percentage {
                        Text("5h \(Int(pct.rounded()))% used").monospacedDigit()
                        UsageBar(pct: pct, stale: display.stale)
                    } else { Text("5h unknown") }
                    Text(display.weeklyPercentage.map { "week \(Int($0.rounded()))% used" } ?? "week unknown")
                        .monospacedDigit()
                }.font(.caption2).foregroundStyle(.secondary)
                let resets = [display.fiveHourReset.map { "5h \(clock($0))" },
                              display.weeklyReset.map { "week \(clock($0))" }].compactMap { $0 }
                if !resets.isEmpty {
                    Text("Resets: " + resets.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 4)
            Text(display.status).font(.caption).foregroundStyle(toneColor(display.tone))
                .multilineTextAlignment(.trailing).frame(width: 102, alignment: .trailing)
        }
    }
}

struct JobRowView: View {
    let display: JobDisplay

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Circle().fill(toneColor(display.tone)).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 1) {
                Text(display.title).font(.callout).lineLimit(1).truncationMode(.middle)
                if !display.detail.isEmpty {
                    Text(display.detail).font(.caption2).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            Spacer()
            Text(display.status).font(.caption).foregroundStyle(toneColor(display.tone))
        }
    }
}

struct JobsView: View {
    let jobs: JobsSection?

    // Select recent jobs before grouping so batch headings don't reduce the
    // C-18.2 limit of eight results or pull older batch members into the menu.
    var recentGroups: [JobGroup] {
        jobGroups(Array((jobs?.recent ?? []).prefix(8)))
    }

    var body: some View {
        let live = jobs?.live ?? []
        VStack(alignment: .leading, spacing: 6) {
            Text("JOBS").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if live.isEmpty {
                Text("Nothing running or waiting.").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(jobGroups(live), id: \.id) { group in
                if let title = group.title {
                    Text(title).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                }
                ForEach(group.jobs) { job in JobRowView(display: jobDisplay(job)) }
            }
            if !recentGroups.isEmpty {
                Text("RECENT").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(recentGroups, id: \.id) { group in
                    if let title = group.title {
                        Text(title).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    }
                    ForEach(group.jobs) { job in JobRowView(display: jobDisplay(job)) }
                }
            }
        }
    }
}

struct ContentView: View {
    @ObservedObject var store: QuotaStore

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("AI quota").font(.headline)
                Spacer()
                if let snap = store.snap {
                    Text("as of \(clock(snap.generated_at))").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let snap = store.snap {
                // MenuBarExtra also asks for the minimum size. A maximum alone
                // lets its window collapse this entire viewport to zero height.
                ScrollView { snapshotContent(snap) }.frame(height: 520)
            } else {
                Label("Snapshot unavailable", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                Text(store.readError ?? "Waiting for the daemon snapshot.").font(.callout).foregroundStyle(.secondary)
                Text(store.url.path).font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
            }
            Divider()
            footer
            if let message = store.reloadMessage {
                Text(message).font(.caption2).foregroundStyle(.secondary)
                    .accessibilityIdentifier("snapshot-reload-result")
            }
            if let loginError = store.loginError { Text(loginError).font(.caption2).foregroundStyle(.red) }
        }
        .padding(12).frame(width: 430)
        .onAppear { store.load() }
    }

    func snapshotContent(_ snap: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if snap.offline == true {
                Label("Offline — showing cached usage", systemImage: "wifi.slash")
                    .font(.caption).foregroundStyle(.orange)
            }
            if snap.isStale(now: store.loadedAt) {
                Label("Snapshot is stale. Check that the daemon is running.", systemImage: "clock.badge.exclamationmark")
                    .font(.caption).foregroundStyle(.orange)
            }
            Divider()
            JobsView(jobs: snap.jobs).accessibilityIdentifier("jobs-section")
            Divider()
            Text("CODEX").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(snap.codex.homes, id: \.id) { lane in
                LaneRow(name: lane.lane_id ?? shortHome(lane.home), subtitle: lane.email ?? "",
                        display: codexDisplay(lane, snapshot: snap, now: store.loadedAt))
            }
            if snap.codex.homes.isEmpty { Text("No Codex lanes.").font(.caption).foregroundStyle(.secondary) }
            Divider()
            Text("CLAUDE").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(snap.claude.accounts ?? [], id: \.id) { lane in
                LaneRow(name: lane.email, subtitle: lane.lane_id ?? "",
                        display: claudeDisplay(lane, snapshot: snap, now: store.loadedAt))
            }
            if (snap.claude.accounts ?? []).isEmpty { Text("No Claude lanes.").font(.caption).foregroundStyle(.secondary) }
        }
    }

    var footer: some View {
        HStack {
            Button { store.reload() } label: {
                Label("Reload snapshot", systemImage: "arrow.clockwise")
            }.help("Read the daemon's latest snapshot. The daemon schedules provider probes.")
            Spacer()
            Toggle("Start at login", isOn: Binding(get: { store.loginItem }, set: { store.setStartAtLogin($0) }))
                .toggleStyle(.checkbox).font(.caption)
            Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }
                .buttonStyle(.plain).help("Quit Subfleet")
        }.font(.callout)
    }
}
#endif
