// Round-two review probe for PR #128 (review job only, never committed to the PR).
// Drives the production views offscreen in unshown windows; no daemon, no live state.
import AppKit
import SwiftUI
import QuartzCore

final class R2Defaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class R2Client: DaemonCalling, @unchecked Sendable {
    var views: [String: ApprovalView] = [:]
    var requests: [String: JSONValue] = [:]
    var gets: [String] = []
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "approval.get" {
            let id = try JSONValue.parse(JSONEncoder().encode(args))["approval_id"]!.string!
            gets.append(id)
            let view = try JSONValue.parse(JSONEncoder().encode(views[id]!))
            return try JSONValue.object(["approval": view, "nonce": .string("n"), "request_sha256": .string("s"),
                                         "request": requests[id]!, "masked": .array([])]).decode(R.self)
        }
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("round-two probe never contacts a daemon")
    }
}

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

@MainActor func backing(_ host: NSView, _ size: CGSize, titled: Bool = false) -> NSWindow {
    let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: size.width, height: size.height),
                          styleMask: titled ? [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView] : [.borderless],
                          backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.appearance = NSApp.appearance
    window.contentView = host
    host.frame = NSRect(origin: .zero, size: size)
    return window
}

@MainActor func settle(_ host: NSView, _ ms: UInt64 = 700) async {
    host.layoutSubtreeIfNeeded()
    if let warm = host.bitmapImageRepForCachingDisplay(in: host.bounds) { host.cacheDisplay(in: host.bounds, to: warm) }
    try? await Task.sleep(nanoseconds: ms * 1_000_000)
    host.layoutSubtreeIfNeeded()
}

@MainActor func snapshot(_ host: NSView, to url: URL) throws -> NSBitmapImageRep {
    let size = host.bounds.size
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = size
    host.cacheDisplay(in: host.bounds, to: rep)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
    return rep
}

func ocr(_ png: URL) throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["R2_TESSERACT"]!)
    process.arguments = [png.path, "stdout", "--psm", "6"]
    let output = Pipe()
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
}

/// Renders a view at a fixed width, at its own fitting height (capped), after `.task` work settles.
@MainActor func renderFitting<V: View>(_ view: V, width: CGFloat, cap: CGFloat = 3200, dark: Bool = false, to url: URL) async throws -> (CGSize, String) {
    let root = view.environment(\.textScale, 1).environment(\.colorScheme, dark ? .dark : .light)
        .frame(width: width).fixedSize(horizontal: false, vertical: true)
        .background(dark ? Color.black : Color.white)
    let host = NSHostingView(rootView: root)
    let window = backing(host, CGSize(width: width, height: 600))
    defer { window.close() }
    await settle(host)
    let fit = host.fittingSize
    host.frame = NSRect(x: 0, y: 0, width: width, height: min(cap, max(40, fit.height)))
    await settle(host, 300)
    _ = try snapshot(host, to: url)
    return (fit, try ocr(url))
}

func pretty(_ value: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!
}

@main struct R2ViewProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        NSApp.appearance = NSAppearance(named: .aqua)
        let scenesURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let out = URL(fileURLWithPath: CommandLine.arguments[2])
        let only = ProcessInfo.processInfo.environment["R2_PARTS"]?.split(separator: ",").map(String.init)
        func want(_ part: String) -> Bool { only?.contains(part) ?? true }
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let root = out.appendingPathComponent(".state-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        var result: [String: Any] = [:]
        let stamp = ISO8601DateFormatter()
        let now = stamp.string(from: Date())

        func conversation(_ id: String, provider: String, title: String, fast: Bool = false, active: Bool = false,
                          updated: String? = nil, pending: Int = 0) -> Conversation {
            Conversation(conversation_id: id, provider: provider, title: title, workspace: "/Users/example/subfleet",
                         workspace_kind: "in-place", allow_main: false, lane_id: provider == "codex" ? "codex-2" : "claude-2",
                         settings: ConversationSettings(model: provider == "codex" ? "gpt-6.1-sol" : "claude-opus-5-5",
                                                        effort: "high", fast: fast),
                         origin: "person", blocked_by: nil, created_at: updated ?? now, updated_at: updated ?? now,
                         pending_approvals: pending, active: active, live_elsewhere: false)
        }
        func baseState() -> ConversationStoreState {
            var state = ConversationStoreState()
            state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
                capabilities: ["conversation.v1", "diff.v1", "steer.v1", "workspace.check.v1"], codex_writable: true,
                steer_providers: ["claude"]))
            state.laneLabels = ["claude-2": "max@example.com", "codex-2": "max@example.com"]
            return state
        }

        // MARK: 1. Approval cards and sheets for every scene
        if want("approvals") {
            let scenes = try JSONValue.parse(Data(contentsOf: scenesURL)).object!
            let client = R2Client()
            var state = baseState()
            state.upsert(conversation("c0", provider: "claude", title: "Approvals"))
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("approvals")), client: client,
                                defaults: R2Defaults(), state: state)
            var approvals: [String: Any] = [:]
            for id in scenes.keys.sorted() {
                let scene = scenes[id]!
                let kind = scene["kind"]!.string!
                let display = ApprovalDisplay(fields: scene["display"]!.object!)
                let options = scene["options"]!.array!.compactMap(\.string)
                client.views[id] = ApprovalView(approval_id: id, message_id: "m1", conversation_id: "c0",
                    provider_request_id: "r-\(id)", kind: kind, display: display, options: options,
                    created_at: now, state: "pending", request_id: "r-\(id)")
                client.requests[id] = scene["request"]!
                let card = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .pending)
                let request = scene["request"]!
                var entry: [String: Any] = [
                    "command_loaded": ApprovalPresentation.command(card, request: request) as Any? ?? NSNull(),
                    "command_before_load": ApprovalPresentation.command(card, request: nil) as Any? ?? NSNull(),
                    "fields_loaded": ApprovalPresentation.grantedFields(card, request: request).map { "\($0.key): \($0.value)" },
                    "fields_before_load": ApprovalPresentation.grantedFields(card, request: nil).map { "\($0.key): \($0.value)" },
                ]
                for dark in [false] {
                    let cardURL = out.appendingPathComponent("card-\(id).png")
                    let (cardSize, cardText) = try await renderFitting(
                        ApprovalCardView(model: model, conversationID: "c0", card: card, review: {}), width: 720, dark: dark, to: cardURL)
                    let sheetURL = out.appendingPathComponent("sheet-\(id).png")
                    let (sheetSize, sheetText) = try await renderFitting(
                        ApprovalSheet(model: model, card: card, approvalID: id, done: {}), width: 560, dark: dark, to: sheetURL)
                    entry["card_height"] = cardSize.height
                    entry["sheet_height"] = sheetSize.height
                    entry["card_ocr"] = cardText
                    entry["sheet_ocr"] = sheetText
                }
                // The same card once answered: history shows the display summary only.
                let settled = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .answered("allow"))
                let (settledSize, _) = try await renderFitting(
                    ApprovalCardView(model: model, conversationID: "c0", card: settled, review: {}), width: 720,
                    to: out.appendingPathComponent("card-answered-\(id).png"))
                entry["answered_card_height"] = settledSize.height
                approvals[id] = entry
                log("approval \(id) card \(entry["card_height"]!) sheet \(entry["sheet_height"]!)")
            }
            result["approvals"] = approvals
            result["approval_gets"] = client.gets
        }

        // MARK: 2. Turn outcomes, serving facts, Fast warning, Codex progress
        if want("turns") {
            var turns: [String: Any] = [:]
            let scenarios = ["failed-model-mismatch", "failed-continued-elsewhere", "stopped", "withdrawn", "stop-too-late",
                             "complete-text-only", "complete-tools", "live-claude", "fast-mismatch", "codex-live-real",
                             "codex-live-real-expanded", "codex-finished-real"]
            for scenario in scenarios {
                let codex = scenario.hasPrefix("codex")
                let live = scenario.hasPrefix("live") || scenario.hasPrefix("codex-live")
                var state = baseState()
                let conv = conversation("c0", provider: codex ? "codex" : "claude", title: "Turn: \(scenario)",
                                        fast: scenario == "fast-mismatch", active: live)
                state.conversations = [conv]
                let receiptState: String = {
                    switch scenario {
                    case "failed-model-mismatch", "failed-continued-elsewhere": return "failed"
                    case "stopped": return "interrupted"
                    case "withdrawn": return "cancelled"
                    default: return live ? "running" : "complete"
                    }
                }()
                let reason: String? = scenario == "failed-model-mismatch" ? "model-mismatch"
                    : scenario == "failed-continued-elsewhere" ? "continued-elsewhere"
                    : scenario == "withdrawn" ? "withdrawn" : scenario == "stop-too-late" ? "stop-too-late" : nil
                let receipt = Receipt(message_id: "m1", conversation_id: "c0", seq: 1, origin: "person", state: receiptState,
                                      state_reason: reason, settings: conv.settings, text: "Please do the next step.")
                state.apply(open: ConversationOpenResult(conversation: conv, messages: [receipt], events_cursor: 0, pending_approvals: []))
                var events: [ConversationEvent] = []
                func add(_ kind: String, _ data: [String: JSONValue]) {
                    events.append(ConversationEvent(seq: events.count + 1, message_id: "m1", kind: kind,
                        ts: stamp.string(from: Date().addingTimeInterval(Double(events.count * 5) - 200)), data: .object(data)))
                }
                func tool(_ id: String, _ name: String, _ summary: String, failed: Bool?) {
                    add("tool.started", ["id": .string(id), "name": .string(name), "summary": .string(summary), "hidden": .bool(false)])
                    if let failed { add("tool.completed", ["id": .string(id), "is_error": .bool(failed), "preview": .string(failed ? "exit 1" : "ok")]) }
                }
                if scenario != "withdrawn" { add("accepted", [:]) }
                let served: [String: JSONValue] = codex
                    ? ["account": .string("max@example.com"), "model": .string("gpt-6.1-sol"), "effort": .string("high"), "lane_id": .string("codex-2")]
                    : ["account": .string("max@example.com"),
                       "model": .string(scenario == "failed-model-mismatch" ? "claude-sonnet-5-5" : "claude-opus-5-5"),
                       "effort": .string("high"), "lane_id": .string("claude-2"),
                       "fast_mode_state": .string("off")]
                if !["withdrawn", "failed-continued-elsewhere"].contains(scenario) { add("served", served) }
                switch scenario {
                case "failed-model-mismatch":
                    add("turn.completed", ["state": .string("failed"), "reason": .string("model-mismatch"),
                                           "detail": .string("asked for claude-opus-5-5, served claude-sonnet-5-5")])
                case "failed-continued-elsewhere":
                    add("turn.completed", ["state": .string("failed"), "reason": .string("continued-elsewhere")])
                case "stopped":
                    tool("t1", "Bash", "git status\ndescription: Show the working tree status", failed: false)
                    tool("t2", "Bash", "swift build\ndescription: Build the app", failed: nil)
                    add("turn.completed", ["state": .string("interrupted")])
                case "stop-too-late", "complete-text-only", "fast-mismatch":
                    add("text", ["block": .string("0"), "text": .string("Here is the answer."), "final": .bool(true)])
                    add("turn.completed", ["state": .string("complete")])
                case "complete-tools":
                    tool("t1", "Bash", "git status\ndescription: Show the working tree status", failed: false)
                    add("text", ["block": .string("0"), "text": .string("Done."), "final": .bool(true)])
                    add("turn.completed", ["state": .string("complete")])
                case "live-claude":
                    tool("t1", "Bash", "git status\ndescription: Show the working tree status", failed: false)
                    tool("t2", "Bash", "swift build\ndescription: Build the app", failed: nil)
                default:
                    // Command strings in the form Codex 0.159 emits (`/bin/zsh -lc '…'`, observed in a lane's stream.jsonl).
                    tool("c1", "command", "/bin/zsh -lc 'git status --short'", failed: false)
                    tool("c2", "command", "/bin/zsh -lc 'pytest -q tests/frontend'", failed: true)
                    tool("c3", "command", "/bin/zsh -lc 'rg -n TODO app/Sources'", failed: false)
                    add("text", ["block": .string("t0"), "text": .string("One test fails; checking the build next.")])
                    tool("c4", "command", "/bin/zsh -lc 'swift build -c release'", failed: scenario == "codex-finished-real" ? false : nil)
                    if scenario == "codex-finished-real" {
                        add("text", ["block": .string("t1"), "text": .string("Built."), "final": .bool(true)])
                        add("turn.completed", ["state": .string("complete")])
                    }
                }
                _ = state.apply(events: EventsPage(events: events, next: events.count, reset: false), conversationID: "c0")
                state.focus("c0")
                let model = UIModel(paths: .rooted(at: root.appendingPathComponent(scenario)), client: R2Client(),
                                    defaults: R2Defaults(), state: state)
                let url = out.appendingPathComponent("turn-\(scenario).png")
                let view = ConversationView(model: model, conversation: model.state.focusedConversation!,
                                            initiallyExpandedWork: scenario.hasSuffix("expanded"))
                let host = NSHostingView(rootView: view.environment(\.textScale, 1).environment(\.colorScheme, .light)
                    .frame(width: 1100, height: 760))
                let window = backing(host, CGSize(width: 1100, height: 760))
                await settle(host, 900)
                _ = try snapshot(host, to: url)
                window.close()
                let timeline = model.state.timelines["c0"]!
                let rows = WorkPresentation.rows(in: timeline).map { row -> String in
                    switch row {
                    case .work(let g): return "work: \(g.label) | running: \(g.running?.label ?? "-") | labels: \(g.tools.map(\.label))"
                    case .item(let item): return "item: \(item.id)"
                    }
                }
                turns[scenario] = ["ocr": try ocr(url), "rows": rows,
                                   "status": timeline.statusText(of: "m1", assistant: codex ? "Codex" : "Claude") ?? "-",
                                   "shows_acknowledgment": timeline.turn("m1")?.showsMessageAcknowledgment ?? false]
                log("turn \(scenario)")
            }
            result["turns"] = turns
        }

        // MARK: 3. Tool labels (P2.3 and P3)
        if want("labels") {
            func label(_ name: String, _ summary: String) -> String {
                ToolActivity(name: name, summary: summary, hidden: false, state: .succeeded).label
            }
            result["labels"] = [
                "codex zsh git status": label("command", "/bin/zsh -lc 'git status --short'"),
                "codex zsh -c cat": label("command", "/bin/zsh -c 'cat /Users/example/review.md'"),
                "codex plain": label("command", "git status --short"),
                "codex bash -lc python": label("command", "bash -lc 'python -m pytest -q'"),
                "codex python -m": label("command", "python -m pytest -q tests/frontend"),
                "codex cd &&": label("command", "cd app && rm -rf build"),
                "claude grep": label("Grep", "pattern: TODO\npath: /repo/src"),
                "claude grep no path": label("Grep", "pattern: TODO"),
                "claude glob": label("Glob", "pattern: **/*.swift"),
                "claude webfetch": label("WebFetch", "url: https://example.com/docs\nprompt: Summarize"),
                "claude websearch": label("WebSearch", "query: swiftui list selection"),
                "codex web search": label("web search", "query: swiftui list selection"),
                "codex two-file edit": label("edit", "path: /repo/app/a.swift, /repo/app/b.swift"),
                "codex one-file edit": label("edit", "path: /repo/app/a.swift"),
                "claude heredoc yaml": label("Bash", "cat > SKILL.md <<'EOF'\n---\nname: x\ndescription: Wrong label\n---\nEOF\ndescription: Write the skill file"),
                "claude heredoc truncated": label("Bash", "cat > SKILL.md <<'EOF'\n---\nname: x\ndescription: Wrong label"),
                "claude plain description": label("Bash", "git status\ndescription: Show status"),
                "claude bash no description": label("Bash", "git status"),
                "claude read": label("Read", "file_path: /repo/README.md"),
                "claude edit": label("Edit", "file_path: /repo/README.md"),
            ]
            let commands = ["/bin/zsh -lc 'git status'", "/bin/zsh -lc 'pytest -q'", "/bin/zsh -lc 'rg TODO'"].enumerated().map {
                TimelineItem(id: "t\($0.offset)", content: .tool(ToolActivity(name: "command", summary: $0.element, hidden: false,
                                                                               state: $0.offset == 1 ? .failed : .succeeded)))
            }
            var group: [String: Any] = [:]
            group["live"] = WorkGroup(id: "g", items: commands, completed: false, duration: nil).label
            for outcome in ["failed", "interrupted", "cancelled", "complete"] {
                group[outcome] = WorkGroup(id: "g", items: commands, completed: true, duration: "3m 12s", outcome: outcome).label
            }
            group["failed-no-duration"] = WorkGroup(id: "g", items: commands, completed: true, duration: nil, outcome: "failed").label
            result["groups"] = group
        }

        // MARK: 4. Sidebar keyboard, focus shortcut, badge, provider filter; MainWindow sidebar toggle
        if want("sidebar") {
            var state = baseState()
            let today = stamp.string(from: Date())
            let yesterday = stamp.string(from: Date().addingTimeInterval(-86400 * 1.2))
            let older = stamp.string(from: Date().addingTimeInterval(-86400 * 6))
            for i in 0..<9 {
                state.upsert(conversation("c\(i)", provider: i % 2 == 0 ? "claude" : "codex", title: "Conversation \(i)",
                                          updated: i < 3 ? today : i < 6 ? yesterday : older, pending: i == 1 ? 2 : 0))
            }
            let client = R2Client()
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("sidebar")), client: client,
                                defaults: R2Defaults(), state: state)
            var selection: String? = "cv:c0"
            var writes: [String] = []
            let binding = Binding<String?>(get: { selection }, set: { selection = $0; writes.append($0 ?? "nil") })
            let host = NSHostingView(rootView: SidebarView(model: model, selection: binding)
                .environment(\.textScale, 1).environment(\.colorScheme, .light).frame(width: 300, height: 760))
            let window = backing(host, CGSize(width: 300, height: 760))
            await settle(host, 800)
            func all(_ v: NSView) -> [NSView] { [v] + v.subviews.flatMap(all) }
            let table = all(host).compactMap { $0 as? NSTableView }.first
            log("sidebar: hosted, table=\(String(describing: all(host).compactMap { $0 as? NSTableView }.first.map { type(of: $0) }))")
            var sidebar: [String: Any] = ["has_table": table != nil, "table_class": table.map { String(describing: type(of: $0)) } ?? "-"]
            if let table {
                sidebar["rows"] = table.numberOfRows
                let focus = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command, .option], timestamp: 0,
                    windowNumber: window.windowNumber, context: nil, characters: "s", charactersIgnoringModifiers: "s",
                    isARepeat: false, keyCode: 1)!
                window.makeFirstResponder(nil)
                log("sidebar: focus shortcut")
                sidebar["focus_shortcut_handled"] = window.performKeyEquivalent(with: focus)
                log("sidebar: focus handled")
                try await Task.sleep(nanoseconds: 300_000_000)
                sidebar["first_responder_is_table"] = window.firstResponder === table
                window.makeFirstResponder(table)
                var path: [String] = [selection ?? "nil"]
                for (key, code, count) in [("\u{F701}", UInt16(125), 10), ("\u{F700}", UInt16(126), 4)] {
                    for _ in 0..<count {
                        let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                            windowNumber: window.windowNumber, context: nil, characters: key, charactersIgnoringModifiers: key,
                            isARepeat: false, keyCode: code)!
                        log("sidebar: key \(code)")
                        table.keyDown(with: e)
                        try await Task.sleep(nanoseconds: 120_000_000)
                        path.append(selection ?? "nil")
                    }
                }
                sidebar["arrow_path"] = path
                log("sidebar: arrow path \(path)")
            }
            // A synthesized click on the hand badge (row of c1, two approvals waiting).
            let buttons = all(host).compactMap { $0 as? NSButton }.filter { !($0 is NSPopUpButton) }
            sidebar["buttons"] = buttons.map { b -> String in
                let f = b.convert(b.bounds, to: nil)
                return "\(type(of: b)) '\(b.title)' \(Int(f.minX)),\(Int(f.minY)) \(Int(f.width))x\(Int(f.height))"
            }
            selection = "cv:c0"; writes = []
            var clicks: [[String: Any]] = []
            for b in buttons where ProcessInfo.processInfo.environment["R2_NO_CLICK"] == nil {
                let f = b.convert(b.bounds, to: nil)
                log("sidebar: click \(type(of: b)) \(b.title) at \(f)")
                let point = NSPoint(x: f.midX, y: f.midY)
                let prior = model.approvalReveal?.token
                func mouse(_ type: NSEvent.EventType) -> NSEvent {
                    NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                       windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
                }
                NSApp.postEvent(mouse(.leftMouseUp), atStart: false)
                window.sendEvent(mouse(.leftMouseDown))
                if let queued = NSApp.nextEvent(matching: .leftMouseUp, until: Date(), inMode: .default, dequeue: true) {
                    window.sendEvent(queued)
                }
                try await Task.sleep(nanoseconds: 300_000_000)
                clicks.append(["button": "\(Int(f.minX)),\(Int(f.minY))", "revealed": model.approvalReveal?.token != prior,
                               "reveal_conversation": model.approvalReveal?.conversationID ?? "nil", "selection_writes": writes])
                writes = []
            }
            sidebar["synthesized_clicks"] = clicks
            log("sidebar: clicks done")
            window.close()
            model.setProviderFilter("codex")
            let (_, filterText) = try await renderFitting(SidebarView(model: model, selection: .constant("cv:c1")).frame(height: 400),
                                                          width: 300, to: out.appendingPathComponent("sidebar-filter-codex.png"))
            sidebar["filter_codex_ocr"] = filterText
            log("sidebar: filter rendered")
            model.setProviderFilter(nil)
            let (_, sidebarText) = try await renderFitting(SidebarView(model: model, selection: .constant("cv:c1")).frame(height: 700),
                                                           width: 300, to: out.appendingPathComponent("sidebar-light.png"))
            sidebar["sidebar_ocr"] = sidebarText
            _ = try await renderFitting(SidebarView(model: model, selection: .constant("Yesterday")).frame(height: 700),
                                        width: 300, to: out.appendingPathComponent("sidebar-header-selected.png"))
            let (_, _) = try await renderFitting(SidebarView(model: model, selection: .constant("cv:c1")).frame(height: 700),
                                                 width: 300, dark: true, to: out.appendingPathComponent("sidebar-dark.png"))

            // The full main window: does ⌃⌘S hide and show the sidebar column?
            if ProcessInfo.processInfo.environment["R2_NO_MAIN"] == nil {
            let mainModel = UIModel(paths: .rooted(at: root.appendingPathComponent("main")), client: R2Client(),
                                    defaults: R2Defaults(), state: state)
            let mainHost = NSHostingView(rootView: MainWindow(model: mainModel, palette: SearchPaletteModel())
                .environment(\.colorScheme, .light))
            let mainWindow = backing(mainHost, CGSize(width: 1200, height: 800), titled: true)
            await settle(mainHost, 900)
            func sidebarWidth() -> CGFloat {
                let tables = all(mainWindow.contentView!).compactMap { $0 as? NSTableView }
                return tables.map { t -> CGFloat in
                    let f = t.convert(t.bounds, to: nil)
                    return t.isHiddenOrHasHiddenAncestor || t.window == nil ? 0 : max(0, min(f.maxX, 1200) - max(f.minX, 0))
                }.max() ?? 0
            }
            var toggle: [String: Any] = ["toolbar_items": mainWindow.toolbar?.items.map { $0.label } ?? [],
                                         "visible_width_before": sidebarWidth()]
            let chord = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command, .control], timestamp: 0,
                windowNumber: mainWindow.windowNumber, context: nil, characters: "s", charactersIgnoringModifiers: "s",
                isARepeat: false, keyCode: 1)!
            log("main: hosted \(toggle)")
            toggle["handled_1"] = mainWindow.performKeyEquivalent(with: chord)
            log("main: toggled once")
            await settle(mainHost, 900)
            toggle["visible_width_after_1"] = sidebarWidth()
            _ = try snapshot(mainHost, to: out.appendingPathComponent("main-after-toggle.png"))
            toggle["handled_2"] = mainWindow.performKeyEquivalent(with: chord)
            await settle(mainHost, 900)
            toggle["visible_width_after_2"] = sidebarWidth()
            _ = try snapshot(mainHost, to: out.appendingPathComponent("main-after-second-toggle.png"))
            mainWindow.close()
            sidebar["main_window_toggle"] = toggle
            }
            result["sidebar"] = sidebar
        }

        // MARK: 5. Send appearance and amber contrast
        if want("controls") {
            func send(_ disabled: Bool) -> AnyView { AnyView(
                Button {} label: {
                    Image(systemName: "arrow.up").frame(width: 32, height: 32)
                        .foregroundStyle(Theme.onAccent).background(Circle().fill(Theme.accent))
                }.buttonStyle(QuietButtonStyle()).disabled(disabled).padding(10).background(Theme.surface.raised.color))
            }
            var pixels: [String: Any] = [:]
            for dark in [false, true] {
                let a = out.appendingPathComponent("send-enabled-\(dark ? "dark" : "light").png")
                let b = out.appendingPathComponent("send-disabled-\(dark ? "dark" : "light").png")
                _ = try await renderFitting(send(false), width: 52, dark: dark, to: a)
                _ = try await renderFitting(send(true), width: 52, dark: dark, to: b)
                let ra = NSBitmapImageRep(data: try Data(contentsOf: a))!, rb = NSBitmapImageRep(data: try Data(contentsOf: b))!
                func center(_ r: NSBitmapImageRep) -> [Int] {
                    let c = r.colorAt(x: r.pixelsWide / 2 - 14, y: r.pixelsHigh / 2)!.usingColorSpace(.sRGB)!
                    return [Int(c.redComponent * 255), Int(c.greenComponent * 255), Int(c.blueComponent * 255)]
                }
                pixels[dark ? "dark" : "light"] = ["enabled_fill": center(ra), "disabled_fill": center(rb)]
            }
            result["send"] = pixels
            var amber: [String: Double] = [:]
            let names = ["conversation", "sidebar", "raised", "selected", "hover"]
            for (name, surface) in zip(names, Theme.surface.all) {
                amber["light/" + name] = (Theme.contrast(Theme.text.attention.light, surface.light) * 100).rounded() / 100
                amber["dark/" + name] = (Theme.contrast(Theme.text.attention.dark, surface.dark) * 100).rounded() / 100
            }
            result["amber"] = amber
        }

        // MARK: 6. Can the review sheet's content shrink to a window-sized height?
        if want("sheetfit") {
            let scenes = try JSONValue.parse(Data(contentsOf: scenesURL)).object!
            let client = R2Client()
            var state = baseState()
            state.upsert(conversation("c0", provider: "claude", title: "Sheet fit"))
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("sheetfit")), client: client,
                                defaults: R2Defaults(), state: state)
            var fits: [String: Any] = [:]
            for id in ["claude-write-120-lines", "real-claude-bash", "r1-codex-command"] {
                let scene = scenes[id]!
                let kind = scene["kind"]!.string!
                let display = ApprovalDisplay(fields: scene["display"]!.object!)
                let options = scene["options"]!.array!.compactMap(\.string)
                client.views[id] = ApprovalView(approval_id: id, message_id: "m1", conversation_id: "c0",
                    provider_request_id: "r-\(id)", kind: kind, display: display, options: options,
                    created_at: now, state: "pending", request_id: "r-\(id)")
                client.requests[id] = scene["request"]!
                let card = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .pending)
                let controller = NSHostingController(rootView: ApprovalSheet(model: model, card: card, approvalID: id, done: {})
                    .environment(\.textScale, 1))
                let window = backing(controller.view, CGSize(width: 560, height: 800))
                await settle(controller.view, 900)
                var entry: [String: Any] = [:]
                for height in [400.0, 800.0, 1117.0] {
                    entry["fits_in_\(Int(height))"] = controller.sizeThatFits(in: CGSize(width: 560, height: height)).height
                }
                entry["preferred"] = controller.view.fittingSize.height
                window.close()
                fits[id] = entry
            }
            result["sheetfit"] = fits
        }

        result["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        print(pretty(result))
    }
}
