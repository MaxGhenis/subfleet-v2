// Subfleet: the status.json model the menu bar panel reads.
// Reads only <SUBFLEET_HOME or ~/.subfleet>/status.json. The daemon owns probes.
// Foundation only, so SUBFLEET_MODEL_TEST probes compile it without a GUI.

import Foundation

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
    var lanes: ClaudeLaneCounts?
}

/// How many Claude lanes admission could place a job on now.
struct ClaudeLaneCounts: Decodable {
    var dispatchable_now: Int?
}

/// Lanes each provider could take work on now, from `status.json`: what the
/// new-conversation sheet's Auto choice weighs (Max: "calling from various
/// lanes of Claude or Codex, depending on where I have the capacity").
func dispatchableLanes(_ snapshot: Snapshot) -> [String: Int] {
    var counts: [String: Int] = ["codex": snapshot.codex.fleet.dispatchable_now]
    if let claude = snapshot.claude.lanes?.dispatchable_now { counts["claude"] = claude }
    return counts
}

/// Claude is the default, including unknown readiness and a tie at zero.
/// Codex needs a ready lane and a known absence of ready Claude lanes.
func autoProvider(_ counts: [String: Int]) -> String {
    guard let claude = counts["claude"] else { return "claude" }
    return claude == 0 && (counts["codex"] ?? 0) > 0 ? "codex" : "claude"
}

// C-17.7, C-18.2: jobs submitted together by `run --batch` carry one label.
struct JobBatch: Decodable, Equatable {
    var id: String
    var label: String
    var index: Int
    var size: Int
}

struct JobRow: Decodable, Identifiable {
    var job_id: String
    var name: String?
    var state: String
    var wait_reason: String?
    var next_check_at: String?
    var sandbox: String?
    var workdir: String?
    var model: String?
    var lane_id: String?
    var attempts: Int?
    var created_at: String?
    var started_at: String?
    var finished_at: String?
    var rc: Int?
    var batch: JobBatch?
    var id: String { job_id }
}

struct JobsSection: Decodable {
    var live: [JobRow]?
    var recent: [JobRow]?
}

// C-18.4: an alert in force, as `status.json` lists it, most severe first.
struct AlertRow: Decodable, Identifiable {
    var key: String
    var severity: String?
    var subject: String?
    var body: String?
    var since: String?
    var last_sent: String?
    var id: String { key }
}

struct Snapshot: Decodable {
    var generated_at: String
    var offline: Bool?
    // Optional: a snapshot written by a daemon that predates C-18.2 has no jobs.
    var jobs: JobsSection?
    // Optional: one that predates C-18.4 has no alerts.
    var alerts: [AlertRow]?
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

struct JobGroup {
    var id: String
    var title: String?
    var jobs: [JobRow]
}

/// C-18.2: a batch's jobs stay together, placed where the first of them appears;
/// a job that belongs to no batch is a group of one with no title.
func jobGroups(_ rows: [JobRow]) -> [JobGroup] {
    var groups: [JobGroup] = []
    var position: [String: Int] = [:]
    for row in rows {
        let key = row.batch.map { "batch:" + $0.id } ?? "job:" + row.job_id
        if let index = position[key] {
            groups[index].jobs.append(row)
        } else {
            position[key] = groups.count
            let title = row.batch.map { "\($0.label) · \($0.size) jobs" }
            groups.append(JobGroup(id: key, title: title, jobs: [row]))
        }
    }
    return groups
}

struct JobDisplay {
    var title: String
    var detail: String
    var status: String
    var tone: LaneTone
}

struct AlertDisplay {
    var title: String
    var detail: String
    var since: String?
    var tone: LaneTone
}

/// C-18.4: an alert's subject, then its body, which names what to run (C-23.52).
/// Critical is an error, info is neutral, and anything else is a warning.
func alertDisplay(_ alert: AlertRow) -> AlertDisplay {
    let subject = alert.subject ?? ""
    let tone: LaneTone = alert.severity == "critical" ? .error : alert.severity == "info" ? .neutral : .warning
    return AlertDisplay(title: subject.isEmpty ? alert.key : subject, detail: alert.body ?? "",
                        since: alert.since, tone: tone)
}

/// C-18.4: an alert other than `info` is a problem the menu bar icon shows.
func alertsNeedAttention(_ snapshot: Snapshot) -> Bool {
    (snapshot.alerts ?? []).contains { alertDisplay($0).tone != .neutral }
}

/// C-18.2: a waiting job says why; a failed one says its exit code.
func jobDisplay(_ job: JobRow) -> JobDisplay {
    let title = (job.name ?? "").isEmpty ? job.job_id : (job.name ?? job.job_id)
    let place = job.workdir.map { URL(fileURLWithPath: $0).lastPathComponent }
    let detail = [job.model, job.lane_id, place].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    switch job.state {
    case "running":
        return JobDisplay(title: title, detail: detail, status: "running", tone: .good)
    case "waiting":
        let reason = job.wait_reason ?? "unknown"
        return JobDisplay(title: title, detail: detail, status: "waiting: \(reason)",
                          tone: reason == "capacity" ? .neutral : .warning)
    case "queued":
        return JobDisplay(title: title, detail: detail, status: "queued", tone: .neutral)
    case "succeeded":
        return JobDisplay(title: title, detail: detail, status: "succeeded", tone: .good)
    case "cancelled":
        return JobDisplay(title: title, detail: detail, status: "cancelled", tone: .neutral)
    default:
        let code = job.rc.map { " rc \($0)" } ?? ""
        return JobDisplay(title: title, detail: detail, status: job.state + code, tone: .error)
    }
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
