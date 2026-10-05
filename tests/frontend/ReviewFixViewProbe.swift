import AppKit
import SwiftUI
import QuartzCore

final class ReviewDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class ReviewClient: DaemonCalling, @unchecked Sendable {
    let fixtures: [JSONValue]
    private let getLock = NSLock()
    private var gets: [String: Int] = [:]
    init(_ fixtures: [JSONValue]) { self.fixtures = fixtures }
    func getCount(_ id: String) -> Int {
        getLock.lock(); defer { getLock.unlock() }
        return gets[id, default: 0]
    }
    func approval(_ fixture: JSONValue) -> ApprovalView {
        var display = ["description": fixture["headline"]!]
        if fixture["kind"]!.string! == "question" { display["questions"] = fixture["request"]?["input"]?["questions"] }
        return ApprovalView(approval_id: fixture["id"]!.string!, message_id: "m", conversation_id: "c",
            kind: fixture["kind"]!.string!, display: ApprovalDisplay(fields: display),
            options: fixture["kind"]!.string! == "question" ? ["answer", "deny"] : ["allow", "deny"],
            created_at: "2026-10-04T10:00:00Z", state: "pending")
    }
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "approval.get" {
            let id = try JSONValue.from(args)["approval_id"]!.string!
            let fixture = fixtures.first { $0["id"]!.string! == id }!
            let result = try JSONValue.from(ApprovalDetail(approval: approval(fixture), request: fixture["request"]!,
                masked: [], request_sha256: "fixture", nonce: "fixture")).decode(R.self)
            getLock.lock(); gets[id, default: 0] += 1; getLock.unlock()
            return result
        }
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("Review fixtures never contact a daemon")
    }
}

@MainActor func allViews(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(allViews) }

@MainActor func capture<V: View>(_ view: V, width: Int = 900, height: Int = 1500,
                               settled: () -> Bool = { true }) async -> NSBitmapImageRep {
    let host = NSHostingView(rootView: view.environment(\.textScale, 1).environment(\.colorScheme, .light)
        .frame(width: CGFloat(width), height: CGFloat(height), alignment: .topLeading).background(Color.white))
    let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: width, height: height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    defer { window.close() }
    host.frame = window.contentView!.bounds
    host.wantsLayer = true
    host.layoutSubtreeIfNeeded()
    if let warm = host.bitmapImageRepForCachingDisplay(in: host.bounds) { host.cacheDisplay(in: host.bounds, to: warm) }
    // Let .task load the exact request. The window remains unshown.
    try? await Task.sleep(nanoseconds: 500_000_000)
    for _ in 0..<50 where !settled() { try? await Task.sleep(nanoseconds: 100_000_000) }
    precondition(settled(), "The request must load before the grant is captured")
    try? await Task.sleep(nanoseconds: 100_000_000)
    host.layoutSubtreeIfNeeded()
    host.displayIfNeeded()
    CATransaction.flush()
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width * 2, pixelsHigh: height * 2,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: width, height: height)
    host.cacheDisplay(in: host.bounds, to: rep)
    return rep
}

func words(_ rep: NSBitmapImageRep, name: String? = nil) throws -> String {
    // Vision requires unavailable sandbox services on some macOS hosts.
    // Tesseract reads the actual raster entirely in the foreground.
    let path = URL(fileURLWithPath: CommandLine.arguments[2]).appendingPathComponent((name ?? UUID().uuidString) + ".png")
    try rep.representation(using: .png, properties: [:])!.write(to: path)
    let process = Process()
    process.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SF_REVIEW_TESSERACT"]!)
    process.arguments = [path.path, "stdout", "--psm", "6"]
    let output = Pipe()
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    precondition(process.terminationStatus == 0, "Foreground OCR failed")
    return String(data: data, encoding: .utf8)!.split(whereSeparator: \.isWhitespace).joined(separator: " ")
}

@main struct ReviewFixViewProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        NSApp.appearance = NSAppearance(named: .aqua)
        let fixtures = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).array!
        let root = URL(fileURLWithPath: CommandLine.arguments[2])
        let client = ReviewClient(fixtures)
        var state = ConversationStoreState()
        state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
            capabilities: ["conversation.v1"], codex_writable: true))
        var conversation = Conversation(conversation_id: "c", provider: "codex", title: "First conversation",
            workspace: "/repo/project", workspace_kind: "in-place", allow_main: false,
            settings: ConversationSettings(model: "gpt-6-astra", effort: "high", fast: true),
            origin: "person", created_at: "2026-10-04T10:00:00Z", updated_at: "2026-10-04T10:00:00Z", pending_approvals: 2, active: false)
        conversation.updated_at = ISO8601DateFormatter().string(from: Date())
        state.upsert(conversation)
        var second = conversation
        second.conversation_id = "second"
        second.title = "Second conversation"
        second.updated_at = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-86400 * 1.2))
        state.upsert(second)
        var earlier = second
        earlier.conversation_id = "earlier"
        earlier.title = "Earlier conversation"
        earlier.updated_at = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-86400 * 6))
        state.upsert(earlier)
        let model = UIModel(paths: .rooted(at: root), client: client, defaults: ReviewDefaults(), state: state)
        var approvals: [String: Any] = [:]
        for fixture in fixtures {
            let approval = client.approval(fixture)
            let card = ApprovalCard(approvalID: approval.approval_id, kind: approval.kind,
                display: approval.display, options: approval.options, state: .pending)
            let cardGets = client.getCount(approval.approval_id)
            let inline = await capture(ApprovalCardView(model: model, conversationID: "c", card: card, review: {})
                                        .fixedSize(horizontal: false, vertical: true),
                                       settled: { client.getCount(approval.approval_id) > cardGets })
            let sheetGets = client.getCount(approval.approval_id)
            let sheet = await capture(ApprovalSheet(model: model, card: card, approvalID: approval.approval_id, done: {})
                                        .fixedSize(horizontal: false, vertical: true),
                                      settled: { client.getCount(approval.approval_id) > sheetGets })
            approvals[approval.approval_id] = ["card": try words(inline, name: "card-" + approval.approval_id),
                                             "sheet": try words(sheet, name: "sheet-" + approval.approval_id)]
        }
        var turns: [String: String] = [:]
        for state in ["complete", "running", "failed", "interrupted", "cancelled", "stop-too-late", "failover", "unblock-note"] {
            let notice = ["failover", "unblock-note"].contains(state)
            let receipt = Receipt(message_id: "m", conversation_id: "c", origin: notice ? state : "person",
                state: state == "stop-too-late" || notice ? "complete" : state,
                state_reason: state == "failed" ? "model-mismatch" : state == "stop-too-late" ? state : nil, settings: conversation.settings,
                served: Served(fields: ["account": .string("max@example.com"), "model": .string("gpt-6-astra"), "effort": .string("high")]), text: "Please review")
            var turnState = model.state
            turnState.apply(open: ConversationOpenResult(conversation: conversation, messages: [receipt], events_cursor: 0, pending_approvals: []))
            let turnModel = UIModel(paths: .rooted(at: root.appendingPathComponent(state)), client: client,
                defaults: ReviewDefaults(), state: turnState)
            let item = turnState.timelines["c"]!.items.first { $0.id == "person:m" }!
            turns[state] = try words(await capture(TimelineRow(model: turnModel, conversation: conversation, item: item, review: { _ in })))
        }
        var selection: String? = "cv:c"
        let binding = Binding(get: { selection }, set: { selection = $0 })
        let sidebarHost = NSHostingView(rootView: SidebarView(model: model, selection: binding).frame(width: 300, height: 700))
        let sidebarWindow = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 300, height: 700), styleMask: [.borderless], backing: .buffered, defer: false)
        sidebarWindow.isReleasedWhenClosed = false
        sidebarWindow.contentView = sidebarHost
        sidebarHost.layoutSubtreeIfNeeded()
        let warmup = sidebarHost.bitmapImageRepForCachingDisplay(in: sidebarHost.bounds)!
        sidebarHost.cacheDisplay(in: sidebarHost.bounds, to: warmup)
        try await Task.sleep(nanoseconds: 500_000_000)
        sidebarHost.layoutSubtreeIfNeeded()
        sidebarHost.cacheDisplay(in: sidebarHost.bounds, to: warmup)
        let table = allViews(sidebarHost).compactMap { $0 as? NSTableView }.first
        let focusEvent = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command, .option], timestamp: 0,
            windowNumber: sidebarWindow.windowNumber, context: nil, characters: "s", charactersIgnoringModifiers: "s", isARepeat: false, keyCode: 1)!
        let focusHandled = sidebarWindow.performKeyEquivalent(with: focusEvent)
        try await Task.sleep(nanoseconds: 300_000_000)
        let focusReachedList = table != nil && sidebarWindow.firstResponder === table
        let before = selection
        if let table {
            sidebarWindow.makeFirstResponder(table)
            let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                windowNumber: sidebarWindow.windowNumber, context: nil, characters: "\u{F701}", charactersIgnoringModifiers: "\u{F701}", isARepeat: false, keyCode: 125)!
            table.keyDown(with: event)
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        let arrowChanged = selection != before
        var arrowPath: [String] = []
        if let table {
            for key in Array(repeating: UInt16(125), count: 8) + Array(repeating: UInt16(126), count: 8) {
                let character = key == 125 ? "\u{F701}" : "\u{F700}"
                let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                    windowNumber: sidebarWindow.windowNumber, context: nil, characters: character,
                    charactersIgnoringModifiers: character, isARepeat: false, keyCode: key)!
                table.keyDown(with: event)
                try await Task.sleep(nanoseconds: 50_000_000)
                arrowPath.append(selection ?? "nil")
            }
        }
        let buttons = allViews(sidebarHost).compactMap { $0 as? NSButton }
        var badgeClicked = false, badgeHit = false, badgeIndependent = false
        for button in buttons {
            // SwiftUI paints the badge's label itself inside a native List,
            // leaving the AppKit button title empty. Menus must never be opened.
            guard !(button is NSPopUpButton) else { continue }
            let prior = model.approvalReveal?.token
            // NSView.hitTest takes a point in its superview's coordinates.
            let point = button.convert(NSPoint(x: button.bounds.midX, y: button.bounds.midY), to: sidebarHost.superview)
            let hit = sidebarHost.hitTest(point)
            // Resolve the actual pointer target in the complete sidebar before
            // invoking it. Native mouse tracking ignores an unshown window.
            guard hit === button || hit.map({ allViews(button).contains($0) }) == true else { continue }
            button.performClick(nil)
            if model.approvalReveal?.token != prior {
                badgeClicked = true
                badgeHit = hit === button || hit.map { allViews(button).contains($0) } == true
                var ancestor = button.superview
                var nested = false
                while let view = ancestor, view !== sidebarHost {
                    if view is NSButton { nested = true }
                    ancestor = view.superview
                }
                badgeIndependent = !nested
            }
        }
        sidebarWindow.close()
        model.setProviderFilter("codex")
        let filter = try words(await capture(SidebarView(model: model, selection: binding), width: 300, height: 700))
        func send(_ enabled: Bool) async -> NSBitmapImageRep {
            await capture(Button("Send") {}.buttonStyle(QuietButtonStyle()).padding(20).disabled(!enabled), width: 180, height: 80)
        }
        let enabled = await send(true), disabled = await send(false)
        let count = enabled.bytesPerRow * enabled.pixelsHigh
        let changed = (0..<count).filter { enabled.bitmapData![$0] != disabled.bitmapData![$0] }.count
        var amberContrast: [Double] = []
        for dark in [false, true] {
            NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance {
                let color = NSColor(Theme.state.attention).usingColorSpace(.sRGB)!
                let rgb = (UInt32((color.redComponent * 255).rounded()) << 16)
                    | (UInt32((color.greenComponent * 255).rounded()) << 8) | UInt32((color.blueComponent * 255).rounded())
                amberContrast.append(Theme.contrast(rgb, dark ? Theme.surface.raised.dark : Theme.surface.raised.light))
            }
        }
        print(String(data: try JSONSerialization.data(withJSONObject: ["approvals": approvals, "turns": turns,
            "sidebar": ["native_selection": table != nil, "arrow_changed_selection": arrowChanged,
                        "arrow_path": arrowPath,
                        "focus_shortcut_handled": focusHandled, "focus_shortcut_reached_list": focusReachedList,
                        "badge_clicked": badgeClicked, "badge_hit_is_control": badgeHit,
                        "badge_is_independent": badgeIndependent,
                        "buttons": buttons.map { ["title": $0.title, "label": $0.accessibilityLabel() ?? "", "type": String(describing: type(of: $0))] }],
            "filter": filter, "disabled_pixel_difference": Double(changed) / Double(count),
            "amber_contrast": amberContrast,
            "visible_windows": NSApp.windows.filter(\.isVisible).count]), encoding: .utf8)!)
    }
}
