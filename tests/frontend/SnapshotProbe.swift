// Real SwiftUI/AppKit views, rendered at 1440 x 900 @2x without a window.
import AppKit
import SwiftUI
import QuartzCore

final class SnapshotDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class SnapshotClient: DaemonCalling, @unchecked Sendable {
    static let approval = ApprovalView(approval_id: "approval1", message_id: "m1", conversation_id: "c0",
        provider_request_id: "request1", kind: "command",
        display: ApprovalDisplay(fields: ["description": .string("Run the frontend tests in this checkout"), "tool": .string("Bash")]),
        options: ["allow", "deny"], created_at: "2026-10-04T10:03:11Z", state: "pending")
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "workspace.check" {
            let value = try JSONValue.parse(JSONEncoder().encode(args))
            let workspace = value["workspace"]?.string ?? ""
            return try JSONValue.object(["ok": .bool(workspace != "/Users/example"), "workspace": .string(workspace),
                "reason": .string("This folder is protected."),
                "fix": .string("Choose a project folder or use a new scratch folder.")]).decode(R.self)
        }
        if op.name == "approval.get" {
            let data = try JSONValue.parse(JSONEncoder().encode(Self.approval))
            return try JSONValue.object(["approval": data, "nonce": .string("fixture"), "request_sha256": .string("fixture"),
                "request": .object(["tool": .string("Bash"), "input": .object(["command": .string("python -m pytest tests/frontend")])]),
                "masked": .array([])]).decode(R.self)
        }
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("Snapshots never contact a daemon")
    }
}

@MainActor
func snapshotModel(_ root: URL, scenario: String, events: [ConversationEvent]) throws -> UIModel {
    let draftScene = ["new", "refused", "permission"].contains(scenario)
    var state = ConversationStoreState()
    state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
        capabilities: ["conversation.v1", "diff.v1", "steer.v1"], codex_writable: true, steer_providers: ["claude"]))
    state.models["claude"] = [ModelEntry(short: "Opus 5.5", id: "claude-opus-5-5", provider: "claude",
        value: "claude-opus-5-5", values: ["claude-opus-5-5"], efforts: ["medium", "high", "ultracode"],
        default_effort: "medium", fast: ModelFast(supported: true, billing: "subscription"))]
    let names = ["Subfleet visual pass", "Fix the search shortcut", "Review the recovery flow", "Compare the references",
                 "Polish conversation titles", "Check pending approvals", "Update Markdown tables", "Trace the composer",
                 "Plan the next release", "Measure text contrast", "Review account capacity", "Clean up notices",
                 "Improve the empty state", "Build the Mac app", "Read the timeline", "Review the Changes pane",
                 "Inspect workspaces", "Test steer and stop", "Check conversation routing", "Document the release"]
    let stamp = ISO8601DateFormatter()
    for (i, title) in names.enumerated() {
        let date = stamp.string(from: Calendar.current.date(byAdding: .day, value: i < 7 ? 0 : i < 14 ? -1 : -5, to: Date())!)
        state.conversations.append(Conversation(conversation_id: "c\(i)", provider: i % 3 == 0 ? "codex" : "claude",
            title: title, workspace: i % 2 == 0 ? "/Users/example/subfleet" : "/Users/example/policyengine",
            workspace_kind: "in-place", allow_main: false, lane_id: "claude-2",
            settings: ConversationSettings(model: "claude-opus-5-5", effort: "medium"), origin: "person",
            blocked_by: i == 2 ? "unfinished-turn" : nil, created_at: date, updated_at: date,
            pending_approvals: i == 1 ? 1 : 0, active: i == 0 && (["live", "live-expanded", "sidebar"].contains(scenario)), live_elsewhere: i == 4))
    }
    state.conversations[0].provider = "claude"
    let live = scenario == "live" || scenario == "live-expanded" || scenario == "sidebar"
    if scenario == "blocked" { state.conversations[0].blocked_by = "unfinished-turn" }
    let receipt = Receipt(message_id: "m1", conversation_id: "c0", seq: 1, origin: "person",
        state: live ? "running" : "complete", settings: state.conversations[0].settings,
        text: "Make the progress easier to read, and keep the existing actions reachable.")
    state.apply(open: ConversationOpenResult(conversation: state.conversations[0], messages: [receipt], events_cursor: 0,
                                            pending_approvals: []))
    var replay = events
    let replayStart = events.first?.ts.flatMap(parseTimestamp) ?? Date()
    func replayStamp(_ seconds: Double) -> String { ISO8601DateFormatter().string(from: replayStart.addingTimeInterval(seconds)) }
    if !live {
        replay.append(ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "tool.completed",
             ts: replayStamp(190), data: .object(["id": .string("tool14"), "is_error": .bool(false)])))
        replay.append(ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "turn.completed",
             ts: replayStamp(192), data: .object(["state": .string("succeeded")])))
        if let lastText = replay.lastIndex(where: { $0.kind == "text" }),
           var data = replay[lastText].data.object, let text = data["text"]?.string {
            data["text"] = .string(text + "\n\n```swift\nlet surface = Theme.surface.raised\n```\n\n| View | Result |\n| --- | --- |\n| Sidebar | One-line names |\n| Timeline | Grouped commands |")
            replay[lastText].data = .object(data)
        }
    }
    _ = state.apply(events: EventsPage(events: replay, next: replay.count, reset: false), conversationID: "c0")
    if live, ["live", "live-expanded"].contains(scenario) {
        let request = ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "approval.requested",
            ts: replayStamp(191), data: .object(["request_id": .string("request1"), "kind": .string("command"),
               "description": .string("Run the frontend tests in this checkout"), "tool": .string("Bash"),
               "options": .array([.string("allow"), .string("deny")])]))
        _ = state.apply(events: EventsPage(events: [request], next: request.seq, reset: false), conversationID: "c0")
    }
    if ["live", "live-expanded"].contains(scenario) { state.timelines["c0"]?.attach(approvals: [SnapshotClient.approval]) }
    state.focus("c0")
    state.laneLabels = ["claude-2": "max@example.com"]
    if draftScene {
        var draft = NewConversationDraft(workspace: scenario == "refused" ? "/Users/example" : "/Users/example/subfleet")
        draft.providerChoice = "claude"
        draft.settings = ConversationSettings(model: "claude-opus-5-5", effort: "medium")
        if scenario == "permission" { draft.settings.permission = "bypass"; draft.confirmWiden = true }
        draft.text = "Review the sidebar and timeline, then render snapshots."
        let support = AppPaths.rooted(at: root).support
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        try JSONEncoder().encode(draft).write(to: support.appendingPathComponent("new-conversation-draft.json"))
    }
    let model = UIModel(paths: .rooted(at: root), client: SnapshotClient(), defaults: SnapshotDefaults(), state: state)
    #if !SUBFLEET_VISUAL_BASELINE
    model.refreshAccountUsage()
    precondition(model.accountSnapshot != nil, "Fixture status must decode")
    #endif
    if draftScene {
        model.newDraft.workspace = scenario == "refused" ? "/Users/example" : "/Users/example/subfleet"
        model.newDraft.settings = ConversationSettings(model: "claude-opus-5-5", effort: "medium")
        if scenario == "permission" { model.newDraft.settings.permission = "bypass"; model.newDraft.confirmWiden = true }
        model.newDraft.text = "Review the sidebar and timeline, then render snapshots."
        model.newDraft.isPresented = true
        model.newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: scenario != "refused",
            reason: scenario == "refused" ? "This folder is protected." : nil,
            fix: scenario == "refused" ? "Choose a project folder or use a new scratch folder." : nil, workspace: model.newDraft.workspace),
            workspace: model.newDraft.workspace!, provider: "claude", permission: model.newDraft.settings.permission)
    }
    return model
}

struct SnapshotCanvas: View {
    @ObservedObject var model: UIModel
    let scenario: String
    var body: some View {
        #if SUBFLEET_VISUAL_BASELINE
        content.foregroundStyle(.primary).background(Color(nsColor: .windowBackgroundColor))
        #else
        content
        #endif
    }
    private var content: some View {
        HStack(spacing: 0) {
            SidebarView(model: model, selection: .constant("cv:c0")).frame(width: 280)
            Divider()
            if ["new", "refused", "permission"].contains(scenario) {
                NewConversationDraftView(model: model)
            } else if scenario == "empty" {
                #if SUBFLEET_VISUAL_BASELINE
                VStack(spacing: 12) {
                    Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle)
                    Text("Choose a conversation or start a new one")
                    Text("Press ⌘K to search conversations and messages").font(.caption)
                    Button("New conversation") {}
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                #else
                EmptyConversationView {}
                #endif
            } else if let conversation = model.state.focusedConversation {
                #if SUBFLEET_VISUAL_BASELINE
                ConversationView(model: model, conversation: conversation)
                #else
                ConversationView(model: model, conversation: conversation, initiallyExpandedWork: scenario == "live-expanded")
                #endif
            }
        }
    }
}

@MainActor func render<V: View>(_ view: V, to url: URL, dark: Bool, settled: () -> Bool = { true }) async throws {
    let host = NSHostingView(rootView: view.environment(\.textScale, 1)
        .environment(\.colorScheme, dark ? .dark : .light).frame(width: 1440, height: 900)
        .scaleEffect(2).frame(width: 2880, height: 1800))
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    host.appearance = NSApp.appearance
    // A backing context is required by SwiftUI Lists and native text editors.
    // This window is never ordered on screen or made key.
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
    try await Task.sleep(nanoseconds: 300_000_000)
    for _ in 0..<50 where !settled() {
        try await Task.sleep(nanoseconds: 100_000_000)
        host.layoutSubtreeIfNeeded()
    }
    host.layoutSubtreeIfNeeded()
    let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
    host.cacheDisplay(in: host.bounds, to: rep)
    precondition(rep.pixelsWide == 2880 && rep.pixelsHigh == 1800)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

@main struct SnapshotProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let out = URL(fileURLWithPath: CommandLine.arguments[1])
        let home = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SUBFLEET_HOME"]!)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let status: [String: Any] = ["generated_at": ISO8601DateFormatter().string(from: Date()),
            "codex": ["homes": [], "fleet": ["total_homes": 0, "dispatchable_now": 0]],
            "claude": ["accounts": [["lane_id": "claude-2", "email": "max@example.com", "active": false,
                "enrolled": true, "owner": "v2", "enabled": true, "dispatchable": true, "identity_status": "verified",
                "verdict": "ready", "probe": ["status": "ok", "five_hour": ["used_percent": 37.0, "status": "provider"], "seven_day": ["used_percent": 62.0, "status": "provider"]]]]]]
        try JSONSerialization.data(withJSONObject: status).write(to: home.appendingPathComponent("status.json"))
        defer { try? FileManager.default.removeItem(at: home) }
        let fixture = URL(fileURLWithPath: CommandLine.arguments[2])
        var events = try JSONDecoder().decode([ConversationEvent].self, from: Data(contentsOf: fixture))
        let offset = Date().timeIntervalSince(parseTimestamp("2026-10-04T10:03:12Z")!)
        for i in events.indices {
            if let ts = events[i].ts.flatMap(parseTimestamp) {
                events[i].ts = ISO8601DateFormatter().string(from: ts.addingTimeInterval(offset))
            }
        }
        let root = out.appendingPathComponent(".fixture-state")
        defer { try? FileManager.default.removeItem(at: root) }
        let scenarios = ProcessInfo.processInfo.environment["SF_SNAPSHOT_SCENES"]?.split(separator: ",").map(String.init)
            ?? ["sidebar", "finished", "live", "live-expanded", "blocked", "new", "refused", "permission", "empty"]
        for scenario in scenarios {
            for dark in [true, false] {
                let model = try snapshotModel(root.appendingPathComponent(UUID().uuidString), scenario: scenario, events: events)
                try await render(SnapshotCanvas(model: model, scenario: scenario),
                           to: out.appendingPathComponent("\(scenario)-\(dark ? "dark" : "light").png"), dark: dark,
                           settled: { !["new", "refused", "permission"].contains(scenario) || model.newDraft.workspaceCheck != nil })
                if ["new", "refused", "permission"].contains(scenario) {
                    precondition(model.newDraft.workspaceCheck?.ok == (scenario != "refused"),
                        "Folder fixture did not settle: \(String(describing: model.newDraft.workspaceCheck))")
                }
            }
        }
        precondition(NSApp.windows.allSatisfy { !$0.isVisible })
        print("Rendered \(scenarios.count * 2) snapshots at 2880×1800 pixels; visible windows: 0")
    }
}
