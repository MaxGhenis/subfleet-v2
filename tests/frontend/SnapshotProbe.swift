// Real SwiftUI/AppKit views, rendered at 1440 x 900 @2x without a window.
import AppKit
import SwiftUI

final class SnapshotDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class SnapshotClient: DaemonCalling, @unchecked Sendable {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        throw DaemonClientError.unavailable("Snapshots never contact a daemon")
    }
}

@MainActor
func snapshotModel(_ root: URL, scenario: String, events: [ConversationEvent]) throws -> UIModel {
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
            pending_approvals: i == 1 ? 1 : 0, active: i == 0 && scenario == "live", live_elsewhere: i == 4))
    }
    state.conversations[0].provider = "claude"
    let live = scenario == "live" || scenario == "sidebar"
    if scenario == "blocked" { state.conversations[0].blocked_by = "unfinished-turn" }
    let receipt = Receipt(message_id: "m1", conversation_id: "c0", seq: 1, origin: "person",
        state: live ? "running" : "complete", settings: state.conversations[0].settings,
        text: "Make the progress easier to read, and keep the existing actions reachable.")
    state.apply(open: ConversationOpenResult(conversation: state.conversations[0], messages: [receipt], events_cursor: 0,
                                            pending_approvals: []))
    var replay = events
    if !live {
        replay.append(ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "tool.completed",
             ts: "2026-10-04T10:03:10Z", data: .object(["id": .string("tool14"), "is_error": .bool(false)])))
        replay.append(ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "turn.completed",
             ts: "2026-10-04T10:03:12Z", data: .object(["state": .string("succeeded")])))
        replay.append(ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "text",
             ts: "2026-10-04T10:03:12Z", data: .object(["block": .string("answer"), "text": .string("The changes are ready to review.\n\n```swift\nlet surface = Theme.surface.raised\n```\n\n| View | Result |\n| --- | --- |\n| Sidebar | One-line names |\n| Timeline | Grouped commands |") ])))
    }
    _ = state.apply(events: EventsPage(events: replay, next: replay.count, reset: false), conversationID: "c0")
    if live, scenario == "live" {
        let request = ConversationEvent(seq: replay.count + 1, message_id: "m1", kind: "approval.requested",
            ts: "2026-10-04T10:03:11Z", data: .object(["request_id": .string("request1"), "kind": .string("command"),
               "description": .string("Run the frontend tests in this checkout"), "tool": .string("Bash"),
               "options": .array([.string("allow"), .string("deny")])]))
        _ = state.apply(events: EventsPage(events: [request], next: request.seq, reset: false), conversationID: "c0")
    }
    state.focus("c0")
    state.laneLabels = ["claude-2": "max@example.com"]
    let model = UIModel(paths: .rooted(at: root), client: SnapshotClient(), defaults: SnapshotDefaults(), state: state)
    if scenario == "new" || scenario == "refused" {
        model.newDraft.workspace = scenario == "refused" ? "/Users/example" : "/Users/example/subfleet"
        model.newDraft.settings = ConversationSettings(model: "claude-opus-5-5", effort: "medium")
        model.newDraft.text = "Review the sidebar and timeline, then render snapshots."
        model.newDraft.isPresented = true
        model.newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: scenario != "refused",
            reason: scenario == "refused" ? "This folder is protected." : nil,
            fix: scenario == "refused" ? "Choose a project folder or use a new scratch folder." : nil, workspace: model.newDraft.workspace),
            workspace: model.newDraft.workspace!, provider: "claude", permission: "ask")
    }
    return model
}

struct SnapshotCanvas: View {
    @ObservedObject var model: UIModel
    let scenario: String
    var body: some View {
        HStack(spacing: 0) {
            SidebarView(model: model, selection: .constant("cv:c0")).frame(width: 280)
            Divider()
            if scenario == "new" || scenario == "refused" {
                NewConversationDraftView(model: model)
            } else if scenario == "empty" {
                VStack(spacing: 12) {
                    Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle)
                    Text("Choose a conversation or start a new one")
                    Text("Press ⌘K to search conversations and messages").font(.caption)
                    Button("New conversation") {}
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let conversation = model.state.focusedConversation {
                ConversationView(model: model, conversation: conversation)
            }
        }
    }
}

@MainActor func render<V: View>(_ view: V, to url: URL, dark: Bool) throws {
    let host = NSHostingView(rootView: view.environment(\.textScale, 1)
        .environment(\.colorScheme, dark ? .dark : .light).frame(width: 1440, height: 900))
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    host.appearance = NSApp.appearance
    // A backing context is required by SwiftUI Lists and native text editors.
    // This window is never ordered on screen or made key.
    let backing = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 1440, height: 900),
                           styleMask: [.borderless], backing: .buffered, defer: false)
    backing.isReleasedWhenClosed = false
    backing.appearance = NSApp.appearance
    backing.contentView = host
    defer { backing.close() }
    host.frame = NSRect(x: 0, y: 0, width: 1440, height: 900)
    host.layoutSubtreeIfNeeded()
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2880, pixelsHigh: 1800,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = host.frame.size
    host.cacheDisplay(in: host.bounds, to: rep)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

@main struct SnapshotProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let out = URL(fileURLWithPath: CommandLine.arguments[1])
        let fixture = URL(fileURLWithPath: CommandLine.arguments[2])
        let events = try JSONDecoder().decode([ConversationEvent].self, from: Data(contentsOf: fixture))
        let root = out.appendingPathComponent(".fixture-state")
        defer { try? FileManager.default.removeItem(at: root) }
        for scenario in ["sidebar", "finished", "live", "blocked", "new", "refused", "empty"] {
            for dark in [true, false] {
                let model = try snapshotModel(root.appendingPathComponent(UUID().uuidString), scenario: scenario, events: events)
                try render(SnapshotCanvas(model: model, scenario: scenario),
                           to: out.appendingPathComponent("\(scenario)-\(dark ? "dark" : "light").png"), dark: dark)
            }
        }
        precondition(NSApp.windows.allSatisfy { !$0.isVisible })
        print("Rendered 14 snapshots at 2880×1800 pixels; visible windows: 0")
    }
}
