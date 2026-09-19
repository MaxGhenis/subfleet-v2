// Subfleet — the retained native menu bar app, backed by the v2 daemon.
// Reads only <SUBFLEET_HOME or ~/.subfleet>/status.json. The daemon owns probes.
// SUBFLEET_MODEL_TEST compiles the same decoding and display logic without a GUI.

import Foundation
#if !SUBFLEET_MODEL_TEST
import AppKit
import ServiceManagement
import SwiftUI
#endif

func statusFileURL(
    environment: [String: String] = ProcessInfo.processInfo.environment,
    home: URL = FileManager.default.homeDirectoryForCurrentUser
) -> URL {
    let override = environment["SUBFLEET_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines)
    let root: URL
    if let override, !override.isEmpty {
        let path = override == "~" ? home.path
            : override.hasPrefix("~/") ? home.appendingPathComponent(String(override.dropFirst(2))).path
            : override
        root = URL(fileURLWithPath: path, isDirectory: true)
    } else {
        root = home.appendingPathComponent(".subfleet", isDirectory: true)
    }
    return root.appendingPathComponent("status.json")
}

func snapshotDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let formatter = ISO8601DateFormatter()
    if let date = formatter.date(from: value) { return date }
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: value)
}

// MARK: - Daemon projection

struct Window_: Decodable {
    var used_percent: Double?
    var reset_at: String?
    var status: String?
    var stale: Bool?
}

struct Windows: Decodable {
    var primary: Window_?
    var secondary: Window_?
    var five_hour: Window_?
    var seven_day: Window_?
    var source: String?
    var stale: Bool?
    var as_of: String?
    var fiveHour: Window_? { five_hour ?? primary }
    var sevenDay: Window_? { seven_day ?? secondary }
}

struct CodexHome: Decodable {
    var lane_id: String?
    var home: String
    var email: String?
    var verdict: String
    var windows: Windows?
    var duplicate_of: String?
    var owner: String?
    var enabled: Bool?
    var dispatchable: Bool?
    var identity_status: String?
    var id: String { lane_id ?? home }
}

struct Fleet: Decodable {
    var total_homes: Int
    var dispatchable_now: Int
    var best_home: String?
}

struct CodexSection: Decodable {
    var homes: [CodexHome]
    var fleet: Fleet
}

struct OAuthWindow: Decodable {
    var used_percent: Double?
    var reset_at: Double?
    var status: String?
    var stale: Bool?
}

struct AccountProbe: Decodable {
    var status: String?
    var five_hour: OAuthWindow?
    var seven_day: OAuthWindow?
}

struct LiveUsage: Decodable {
    var five_hour_pct: Double?
    var seven_day_pct: Double?
    var source: String?
    var stale: Bool?
}

struct ClaudeAccount: Decodable {
    var lane_id: String?
    var email: String
    var active: Bool
    var enrolled: Bool
    var probe: AccountProbe?
    var live: LiveUsage?
    var oauth_status: String?
    var verdict: String?
    var owner: String?
    var enabled: Bool?
    var dispatchable: Bool?
    var identity_status: String?
    var id: String { lane_id ?? email }
}

struct ClaudeSection: Decodable {
    var accounts: [ClaudeAccount]?
}

struct Snapshot: Decodable {
    var generated_at: String
    var offline: Bool?
    var codex: CodexSection
    var claude: ClaudeSection

    func isStale(now: Date = Date(), maxAge: TimeInterval = 600) -> Bool {
        guard let generated = snapshotDate(generated_at) else { return true }
        let age = now.timeIntervalSince(generated)
        // More than two normal probe intervals, or an unreliable future clock.
        return age > maxAge || age < -60
    }
}

enum LaneTone: String { case good, warning, error, neutral }

struct LaneDisplay {
    var percentage: Double?
    var weeklyPercentage: Double?
    var status: String
    var detail: String
    var stale: Bool
    var tone: LaneTone
    var fiveHourReset: Date?
    var weeklyReset: Date?
}

private func providerPercent(_ value: Double?, label: String?) -> Double? {
    guard label == "provider" || label == "stale-provider",
          let value, value.isFinite, (0...100).contains(value) else { return nil }
    return value
}

private func laneDisplay(
    percentage: Double?, weekly: Double?, owner: String?, enabled: Bool?,
    dispatchable: Bool?, identity: String?, verdict: String,
    duplicate: Bool = false, desktop: Bool = false, evidenceStale: Bool,
    snapshot: Snapshot, now: Date
) -> LaneDisplay {
    let mismatch = identity == "mismatch"
    let unverified = identity != nil && identity != "verified" && !mismatch
    let offline = snapshot.offline == true
    let snapshotStale = snapshot.isStale(now: now)
    let stale = evidenceStale || snapshotStale || offline
    let authFailure = ["auth-dead", "auth-revoked", "no-auth", "auth-suspect"].contains(verdict)
    var notes: [String] = []
    if owner != "v2" { notes.append("Owner: \(owner ?? "unknown")") }
    if enabled == false { notes.append("Disabled") }
    if dispatchable != true { notes.append("Not dispatchable") }
    if mismatch { notes.append("Identity mismatch") }
    else if unverified { notes.append("Identity \(identity!)") }
    if duplicate { notes.append("Duplicate account") }
    if desktop { notes.append("Desktop login") }
    if offline { notes.append("Offline; cached data") }
    if snapshotStale { notes.append("Snapshot stale") }
    else if evidenceStale { notes.append("Usage stale") }

    let status: String
    let tone: LaneTone
    if mismatch { status = "Identity mismatch"; tone = .error }
    else if authFailure { status = "Sign-in required"; tone = .error }
    else if owner != "v2" { status = owner.map { "Owned by \($0)" } ?? "Owner unknown"; tone = .warning }
    else if enabled == false { status = "Disabled"; tone = .neutral }
    else if duplicate { status = "Duplicate lane"; tone = .warning }
    else if unverified { status = "Identity unverified"; tone = .warning }
    else if offline { status = "Offline · cached"; tone = .warning }
    else if snapshotStale { status = "Snapshot stale"; tone = .warning }
    else if evidenceStale { status = "Stale usage"; tone = .warning }
    else if verdict == "limited" { status = "Limited"; tone = .warning }
    else if dispatchable != true { status = "Unavailable"; tone = .neutral }
    else if ["ok", "ready", "provider", "admission-observed"].contains(verdict) {
        status = verdict == "admission-observed" ? "Admitted recently" : "Available"
        tone = .good
    } else { status = "Unknown"; tone = .neutral }
    return LaneDisplay(percentage: mismatch ? nil : percentage,
                       weeklyPercentage: mismatch ? nil : weekly,
                       status: status, detail: notes.joined(separator: " · "), stale: stale, tone: tone)
}

func codexDisplay(_ lane: CodexHome, snapshot: Snapshot, now: Date = Date()) -> LaneDisplay {
    let windows = lane.windows
    let five = windows?.fiveHour
    let seven = windows?.sevenDay
    let stale = windows?.stale == true || windows?.source == "stale-provider"
        || five?.stale == true || five?.status == "stale-provider"
        || seven?.stale == true || seven?.status == "stale-provider"
    var display = laneDisplay(
        percentage: providerPercent(five?.used_percent, label: five?.status ?? windows?.source),
        weekly: providerPercent(seven?.used_percent, label: seven?.status ?? windows?.source),
        owner: lane.owner, enabled: lane.enabled, dispatchable: lane.dispatchable,
        identity: lane.identity_status, verdict: lane.verdict, duplicate: lane.duplicate_of != nil,
        evidenceStale: stale, snapshot: snapshot, now: now)
    if display.percentage != nil { display.fiveHourReset = snapshotDate(five?.reset_at) }
    if display.weeklyPercentage != nil { display.weeklyReset = snapshotDate(seven?.reset_at) }
    return display
}

func claudeDisplay(_ lane: ClaudeAccount, snapshot: Snapshot, now: Date = Date()) -> LaneDisplay {
    let five = lane.probe?.five_hour
    let seven = lane.probe?.seven_day
    let stale = lane.live?.stale == true || lane.live?.source == "stale-provider"
        || five?.stale == true || five?.status == "stale-provider"
        || seven?.stale == true || seven?.status == "stale-provider"
    var display = laneDisplay(
        percentage: providerPercent(five?.used_percent, label: five?.status)
            ?? providerPercent(lane.live?.five_hour_pct, label: lane.live?.source),
        weekly: providerPercent(seven?.used_percent, label: seven?.status)
            ?? providerPercent(lane.live?.seven_day_pct, label: lane.live?.source),
        owner: lane.owner, enabled: lane.enabled ?? lane.enrolled, dispatchable: lane.dispatchable,
        identity: lane.identity_status, verdict: lane.verdict ?? lane.probe?.status ?? "unknown",
        desktop: lane.active, evidenceStale: stale, snapshot: snapshot, now: now)
    if display.percentage != nil, let reset = five?.reset_at, reset.isFinite {
        display.fiveHourReset = Date(timeIntervalSince1970: reset)
    }
    if display.weeklyPercentage != nil, let reset = seven?.reset_at, reset.isFinite {
        display.weeklyReset = Date(timeIntervalSince1970: reset)
    }
    return display
}

#if !SUBFLEET_MODEL_TEST
// MARK: - Read-only store

@MainActor
final class QuotaStore: ObservableObject {
    @Published var snap: Snapshot?
    @Published var loadedAt = Date()
    @Published var readError: String?
    @Published var loginItem = SMAppService.mainApp.status == .enabled
    @Published var loginError: String?
    let url = statusFileURL()
    private var timer: Timer?

    init() {
        load()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.load() }
        }
    }

    func load() {
        loadedAt = Date()
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
                ScrollView { snapshotContent(snap) }.frame(maxHeight: 520)
            } else {
                Label("Snapshot unavailable", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                Text(store.readError ?? "Waiting for the daemon snapshot.").font(.callout).foregroundStyle(.secondary)
                Text(store.url.path).font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
            }
            Divider()
            footer
            if let loginError = store.loginError { Text(loginError).font(.caption2).foregroundStyle(.red) }
        }
        .padding(12).frame(width: 430)
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
            Button { store.load() } label: {
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

@main
struct SubfleetApp: App {
    @StateObject private var store = QuotaStore()
    var body: some Scene {
        MenuBarExtra {
            ContentView(store: store)
        } label: {
            HStack(spacing: 2) {
                Image(systemName: store.hasProblem ? "bolt.trianglebadge.exclamationmark" : "bolt.fill")
                Text(store.barLabel).font(.system(.body, design: .monospaced))
            }
        }.menuBarExtraStyle(.window)
    }
}
#endif
