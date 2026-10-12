// Sidebar triage (C-29.15) and the Worktree toggle (C-29.16), rendered at 1440 x 900 @2x
// without a window, as SuggestionSnapshotProbe.swift renders: the sidebar's Pinned and
// Unread sections with a worktree conversation open, the Archived view with an archived
// conversation open, and New conversation in a git checkout with Worktree on.
import AppKit
import SwiftUI
import QuartzCore

final class TriageSnapshotDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func dictionary(forKey key: String) -> [String: Any]? { values[key] as? [String: Any] }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

/// Snapshots never reach a daemon: the folder check passes, the runs strip is empty,
/// and everything else is unavailable.
final class TriageSnapshotClient: DaemonCalling, @unchecked Sendable {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        switch op.name {
        case "conversation.runs": return try JSONValue.object(["runs": .array([])]).decode(R.self)
        case "workspace.check": return try JSONValue.object(["ok": .bool(true)]).decode(R.self)
        default: throw DaemonClientError.unavailable("Snapshots never contact a daemon")
        }
    }
}

@MainActor
func triageModel(_ root: URL, scene: String, repo: URL) throws -> UIModel {
    var state = ConversationStoreState()
    state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
        capabilities: ["conversations.v1", "diff.v1", "steer.v1", "workspace.check.v1", archiveCapability],
        codex_writable: false, steer_providers: ["claude"]))
    state.models["claude"] = [ModelEntry(short: "Opus 5.5", id: "claude-opus-5-5", provider: "claude",
        value: "claude-opus-5-5", values: ["claude-opus-5-5"], efforts: ["medium", "high", "ultracode"],
        default_effort: "medium", fast: ModelFast(supported: true, billing: "subscription"))]
    let stamp = ISO8601DateFormatter()
    let now = Date()
    func ago(_ minutes: Double) -> String { stamp.string(from: now.addingTimeInterval(-minutes * 60)) }
    let names = ["Fix the exporter's date handling", "Review the recovery flow", "Polish conversation titles",
                 "Check pending approvals", "Update Markdown tables", "Plan the next release", "Measure text contrast",
                 "Review account capacity", "Build the Mac app", "Read the timeline", "Retire the v1 cockpit",
                 "Old benchmark notes"]
    let home = NSHomeDirectory()
    for (i, title) in names.enumerated() {
        let worktree: JSONValue? = i == 0 ? .object([
            "path": .string(home + "/.subfleet/worktrees/conversation-c0"), "branch": .string("subfleet/cv-20261011-c0"),
            "source": .string(home + "/subfleet"), "repository": .string(home + "/subfleet")]) : nil
        state.conversations.append(Conversation(conversation_id: "c\(i)", provider: i == 6 ? "codex" : "claude", title: title,
            workspace: i == 0 ? home + "/.subfleet/worktrees/conversation-c0" : home + (i % 2 == 0 ? "/subfleet" : "/policyengine"),
            workspace_kind: i == 0 ? "worktree" : "in-place", worktree: worktree,
            allow_main: false, lane_id: "claude-2", settings: ConversationSettings(model: "claude-opus-5-5", effort: "medium"),
            origin: "new", created_at: ago(Double(i) * 50 + 5), updated_at: ago(Double(i) * 50 + 5),
            pending_approvals: 0, active: i == 3, live_elsewhere: false,
            archived_at: i >= 10 ? ago(20) : nil))
    }
    // Seen up to the floor; c2, c4 and c7 changed since the person last looked.
    state.marks.caughtUp(at: 100)
    state.messageSeqs = ["c0": 90, "c1": 95, "c2": 120, "c3": 99, "c4": 130, "c5": 80, "c7": 140, "c8": 60]
    state.marks.markRead("c1", through: 95)
    state.marks.setPinned("c8", true)
    state.marks.setPinned("c7", true)
    state.marks.unreadFirst = true
    if scene == "archived" { state.showArchived = true }
    let open = scene == "archived" ? "c10" : "c0"
    let index = state.conversations.firstIndex { $0.conversation_id == open }!
    let receipt = Receipt(message_id: "m1", conversation_id: open, seq: 1, origin: "person", state: "complete",
        settings: state.conversations[index].settings,
        served: Served(fields: ["account": .string("max@example.com"), "model": .string("claude-opus-5-5"),
                                "effort": .string("medium"), "fast_mode_state": .string("off")]),
        text: scene == "archived" ? "Retire the v1 cockpit's outbox import." : "The exporter writes dates in local time. Fix it.")
    state.apply(open: ConversationOpenResult(conversation: state.conversations[index], messages: [receipt],
                                            events_cursor: 0, pending_approvals: []))
    let events = [
        ConversationEvent(seq: 1, message_id: "m1", kind: "accepted", ts: ago(4), data: .object([:])),
        ConversationEvent(seq: 2, message_id: "m1", kind: "text", ts: ago(1), data: .object([
            "block": .string("0"), "final": .bool(true),
            "text": .string(scene == "archived" ? "Done: the import is retired and its tests are gone."
                            : "The exporter now writes every date in UTC, on this conversation's own branch; its tests pass.")])),
        ConversationEvent(seq: 3, message_id: "m1", kind: "turn.completed", ts: ago(1), data: .object(["state": .string("succeeded")])),
    ]
    _ = state.apply(events: EventsPage(events: events, next: events.count, reset: false), conversationID: open)
    if scene != "new-worktree" { state.focus(open) }
    state.laneLabels = ["claude-2": "max@example.com"]
    if scene == "new-worktree" {
        // A saved draft in a git checkout with Worktree on, as a relaunch finds it.
        var draft = NewConversationDraft(workspace: repo.path)
        draft.text = "Make the importer read the version 3 manifest."
        draft.settings = ConversationSettings(model: "claude-opus-5-5")
        draft.worktree = true
        let support = root.appendingPathComponent("support", isDirectory: true)
        try ensurePrivateDirectory(support)
        try JSONEncoder().encode(draft).write(to: support.appendingPathComponent("new-conversation-draft.json"))
    }
    let model = UIModel(paths: .rooted(at: root), client: TriageSnapshotClient(), defaults: TriageSnapshotDefaults(),
                        state: state)
    model.refreshAccountUsage()
    if scene == "new-worktree" { model.openNewDraft() }
    return model
}

struct TriageCanvas: View {
    @ObservedObject var model: UIModel
    let scene: String
    var body: some View {
        HStack(spacing: 0) {
            SidebarView(model: model, selection: .constant(scene == "archived" ? "cv:c10" : scene == "new-worktree" ? nil : "cv:c0"))
                .frame(width: 300)
            Divider()
            if model.newDraft.isPresented {
                NewConversationDraftView(model: model)
            } else if let conversation = model.state.focusedConversation {
                ConversationView(model: model, conversation: conversation)
            }
        }
    }
}

@MainActor func renderTriage<V: View>(_ view: V, to url: URL, dark: Bool) async throws {
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
    // The draft's folder check answers on the read queue; let it land.
    try await Task.sleep(nanoseconds: 800_000_000)
    host.layoutSubtreeIfNeeded()
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2880, pixelsHigh: 1800,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = host.bounds.size
    host.cacheDisplay(in: host.bounds, to: rep)
    precondition(rep.pixelsWide == 2880 && rep.pixelsHigh == 1800)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

@main struct TriageSnapshotProbe {
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
        // A git checkout as the toggle finds one: a `.git` directory holding HEAD.
        let repo = root.appendingPathComponent("subfleet", isDirectory: true)
        try FileManager.default.createDirectory(at: repo.appendingPathComponent(".git"), withIntermediateDirectories: true)
        try Data("ref: refs/heads/main\n".utf8).write(to: repo.appendingPathComponent(".git/HEAD"))
        let scenes = ProcessInfo.processInfo.environment["SF_TRIAGE_SCENES"]?.split(separator: ",").map(String.init)
            ?? ["sidebar", "archived", "new-worktree"]
        var rendered = 0
        for scene in scenes {
            for dark in [true, false] {
                let model = try triageModel(root.appendingPathComponent(UUID().uuidString), scene: scene, repo: repo)
                try await renderTriage(TriageCanvas(model: model, scene: scene),
                                       to: out.appendingPathComponent("triage-\(scene)-\(dark ? "dark" : "light").png"),
                                       dark: dark)
                if scene == "new-worktree" {
                    precondition(model.newDraft.showsWorktreeToggle && model.newDraft.workspaceKind == "worktree",
                                 "the toggle should show, on")
                }
                if scene == "sidebar" {
                    let sections = model.state.sidebar().map(\.id)
                    precondition(Array(sections.prefix(2)) == ["pinned", "unread"], "\(sections)")
                }
                rendered += 1
            }
        }
        precondition(NSApp.windows.allSatisfy { !$0.isVisible })
        print("Rendered \(rendered) snapshots at 2880×1800 pixels; visible windows: 0")
    }
}
