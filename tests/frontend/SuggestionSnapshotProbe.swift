// Suggested conversations (C-29.14), rendered at 1440 x 900 @2x without a window, as
// SnapshotProbe.swift renders the visual pass: the card in the conversation that
// suggested it, the sidebar's Suggested section, and a suggestion opened from it.
import AppKit
import SwiftUI
import QuartzCore

final class SuggestionSnapshotDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

/// Snapshots never reach a daemon; the views' own reads answer empty.
final class SuggestionSnapshotClient: DaemonCalling, @unchecked Sendable {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("Snapshots never contact a daemon")
    }
}

let suggestionBrief = """
Port the session importer to the version 3 manifest.

The exporter fix in this conversation depends on it, but it is its own piece of work:

- Read `docs/migration.md` and the manifest schema in `subfleet/importer.py`
- Map the five renamed fields; keep the version 2 reader for old exports
- Run `uv run pytest tests/unit/test_importer.py` and report what changed

Leave the exporter alone; this conversation is fixing it.
"""

func suggestionFixture(_ id: String, title: String, tldr: String, origin: String?, message: String?, state: String,
                       created: String, workspace: String?, worktree: Bool, provider: String = "claude",
                       started: String? = nil, kind: String = "in-place", repository: String? = nil) -> Suggestion {
    Suggestion(suggestion_id: id, state: state, title: title, tldr: tldr, brief: suggestionBrief, brief_truncated: false,
               brief_chars: suggestionBrief.count, origin_conversation_id: origin, origin_message_id: message,
               creator_session: origin == nil ? "desktop-session" : nil, workspace: workspace, worktree: worktree,
               workspace_kind: kind, repository: repository, provider: provider, model: provider == "codex" ? "gpt-6-astra" : nil,
               decided_by: state == "open" ? nil : "the Subfleet app", decided_at: state == "open" ? nil : created,
               start_error: nil, started_conversation_id: started, started_message_id: started == nil ? nil : "m-started",
               created_at: created, updated_at: created)
}

@MainActor
func suggestionModel(_ root: URL, scene: String) throws -> UIModel {
    var state = ConversationStoreState()
    state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
        capabilities: ["conversations.v1", "diff.v1", "steer.v1", "workspace.check.v1", suggestionsCapability],
        codex_writable: false, steer_providers: ["claude"]))
    state.models["claude"] = [ModelEntry(short: "Opus 5.5", id: "claude-opus-5-5", provider: "claude",
        value: "claude-opus-5-5", values: ["claude-opus-5-5"], efforts: ["medium", "high", "ultracode"],
        default_effort: "medium", fast: ModelFast(supported: true, billing: "subscription"))]
    let stamp = ISO8601DateFormatter()
    let now = Date()
    func ago(_ minutes: Double) -> String { stamp.string(from: now.addingTimeInterval(-minutes * 60)) }
    let names = ["Fix the exporter's date handling", "Review the recovery flow", "Polish conversation titles",
                 "Check pending approvals", "Update Markdown tables", "Plan the next release", "Measure text contrast",
                 "Review account capacity", "Build the Mac app", "Read the timeline"]
    for (i, title) in names.enumerated() {
        state.conversations.append(Conversation(conversation_id: "c\(i)", provider: "claude", title: title,
            workspace: NSHomeDirectory() + (i % 2 == 0 ? "/subfleet" : "/policyengine"), workspace_kind: "in-place",
            allow_main: false, lane_id: "claude-2", settings: ConversationSettings(model: "claude-opus-5-5", effort: "medium"),
            origin: "new", created_at: ago(Double(i) * 50 + 5), updated_at: ago(Double(i) * 50 + 5),
            pending_approvals: 0, active: false, live_elsewhere: false))
    }
    let started = scene == "card-started"
    let card = suggestionFixture("sg-1", title: "Port the importer to manifest v3",
        tldr: "The exporter fix depends on it, but it is a separate piece of work worth its own session.",
        origin: "c0", message: "m1", state: started ? "started" : "open", created: ago(2),
        workspace: NSHomeDirectory() + "/subfleet", worktree: true, started: started ? "c9" : nil, kind: "worktree",
        repository: NSHomeDirectory() + "/subfleet")
    let others = [
        suggestionFixture("sg-2", title: "Add a regression test for DST", tldr: "Found while fixing the exporter.",
                          origin: "c1", message: nil, state: "open", created: ago(30),
                          workspace: NSHomeDirectory() + "/subfleet", worktree: false),
        suggestionFixture("sg-3", title: "Review the release notes", tldr: "From a desktop session; no conversation.",
                          origin: nil, message: nil, state: "open", created: ago(90), workspace: nil, worktree: false,
                          provider: "codex"),
    ]
    for suggestion in [card] + others { state.upsert(suggestion) }
    let receipt = Receipt(message_id: "m1", conversation_id: "c0", seq: 1, origin: "person", state: "complete",
        settings: state.conversations[0].settings,
        served: Served(fields: ["account": .string("max@example.com"), "model": .string("claude-opus-5-5"),
                                "effort": .string("medium"), "fast_mode_state": .string("off")]),
        text: "The exporter writes dates in local time. Fix it, and tell me about anything out of scope.")
    state.apply(open: ConversationOpenResult(conversation: state.conversations[0], messages: [receipt], events_cursor: 0,
                                            pending_approvals: []))
    let start = now.addingTimeInterval(-240)
    func at(_ seconds: Double) -> String { stamp.string(from: start.addingTimeInterval(seconds)) }
    let data = try JSONValue.parse(JSONEncoder().encode(card))
    let events = [
        ConversationEvent(seq: 1, message_id: "m1", kind: "status", ts: at(0), data: .object(["phase": .string("starting-provider")])),
        ConversationEvent(seq: 2, message_id: "m1", kind: "accepted", ts: at(2), data: .object([:])),
        ConversationEvent(seq: 3, message_id: "m1", kind: "tool.started", ts: at(10), data: .object([
            "id": .string("t1"), "name": .string("Bash"), "summary": .string("uv run pytest tests/unit/test_exporter.py")])),
        ConversationEvent(seq: 4, message_id: "m1", kind: "tool.completed", ts: at(40), data: .object([
            "id": .string("t1"), "is_error": .bool(false)])),
        ConversationEvent(seq: 5, message_id: "m1", kind: "text", ts: at(200), data: .object([
            "block": .string("0"), "final": .bool(true),
            "text": .string("The exporter now writes every date in UTC, and its tests pass. The importer still reads "
                            + "the version 2 manifest, which is out of scope here, so I suggested it as its own conversation.")])),
        ConversationEvent(seq: 6, message_id: "m1", kind: "suggestion", ts: at(210), data: data),
        ConversationEvent(seq: 7, message_id: "m1", kind: "turn.completed", ts: at(215), data: .object(["state": .string("succeeded")])),
    ]
    _ = state.apply(events: EventsPage(events: events, next: events.count, reset: false), conversationID: "c0")
    state.focus("c0")
    state.laneLabels = ["claude-2": "max@example.com"]
    let model = UIModel(paths: .rooted(at: root), client: SuggestionSnapshotClient(), defaults: SuggestionSnapshotDefaults(),
                        state: state)
    model.refreshAccountUsage()
    if scene == "detail" { model.selectedSuggestionID = "sg-3" }
    return model
}

struct SuggestionCanvas: View {
    @ObservedObject var model: UIModel
    let scene: String
    var body: some View {
        HStack(spacing: 0) {
            SidebarView(model: model, selection: .constant(scene == "detail" ? "sg:sg-3" : "cv:c0")).frame(width: 300)
            Divider()
            if let suggestion = model.selectedSuggestion {
                SuggestionDetailView(model: model, suggestion: suggestion)
            } else if let conversation = model.state.focusedConversation {
                ConversationView(model: model, conversation: conversation)
            }
        }
    }
}

@MainActor func renderSuggestion<V: View>(_ view: V, to url: URL, dark: Bool) async throws {
    let host = NSHostingView(rootView: view.environment(\.textScale, 1)
        .environment(\.colorScheme, dark ? .dark : .light).frame(width: 1440, height: 900)
        .scaleEffect(2).frame(width: 2880, height: 1800))
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    host.appearance = NSApp.appearance
    // A backing context is required by SwiftUI Lists; this window is never ordered on screen.
    let backing = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 2880, height: 1800),
                           styleMask: [.borderless], backing: .buffered, defer: false)
    backing.isReleasedWhenClosed = false
    backing.appearance = NSApp.appearance
    backing.contentView = host
    defer { backing.close() }
    host.frame = NSRect(x: 0, y: 0, width: 2880, height: 1800)
    host.wantsLayer = true
    host.layoutSubtreeIfNeeded()
    host.displayIfNeeded()
    CATransaction.flush()
    let warmup = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
    host.cacheDisplay(in: host.bounds, to: warmup)
    try await Task.sleep(nanoseconds: 400_000_000)
    host.layoutSubtreeIfNeeded()
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2880, pixelsHigh: 1800,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = host.bounds.size
    host.cacheDisplay(in: host.bounds, to: rep)
    precondition(rep.pixelsWide == 2880 && rep.pixelsHigh == 1800)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

@main struct SuggestionSnapshotProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let out = URL(fileURLWithPath: CommandLine.arguments[1])
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let home = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SUBFLEET_HOME"]!)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let status: [String: Any] = ["generated_at": ISO8601DateFormatter().string(from: Date()),
            "codex": ["homes": [], "fleet": ["total_homes": 0, "dispatchable_now": 0]],
            "claude": ["accounts": [["lane_id": "claude-2", "email": "max@example.com", "active": false,
                "enrolled": true, "owner": "v2", "enabled": true, "dispatchable": true, "identity_status": "verified",
                "verdict": "ready", "probe": ["status": "ok", "five_hour": ["used_percent": 37.0, "status": "provider"],
                                              "seven_day": ["used_percent": 62.0, "status": "provider"]]]]]]
        try JSONSerialization.data(withJSONObject: status).write(to: home.appendingPathComponent("status.json"))
        defer { try? FileManager.default.removeItem(at: home) }
        let root = out.appendingPathComponent(".fixture-state")
        defer { try? FileManager.default.removeItem(at: root) }
        let scenes = ProcessInfo.processInfo.environment["SF_SUGGESTION_SCENES"]?.split(separator: ",").map(String.init)
            ?? ["card", "card-started", "detail"]
        var rendered = 0
        for scene in scenes {
            for dark in [true, false] {
                let model = try suggestionModel(root.appendingPathComponent(UUID().uuidString), scene: scene)
                precondition(model.state.openSuggestionCount == (scene == "card-started" ? 2 : 3))
                try await renderSuggestion(SuggestionCanvas(model: model, scene: scene),
                                           to: out.appendingPathComponent("suggestion-\(scene)-\(dark ? "dark" : "light").png"),
                                           dark: dark)
                rendered += 1
            }
        }
        precondition(NSApp.windows.allSatisfy { !$0.isVisible })
        print("Rendered \(rendered) snapshots at 2880×1800 pixels; visible windows: 0")
    }
}
