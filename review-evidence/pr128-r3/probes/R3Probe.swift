// Round-three review probe for PR #128 (review job only, never committed to the PR branch).
// Drives the production views offscreen in unshown, borderless windows at x = -10000; no daemon,
// no live state. Built from the round-two probe (R2ViewProbe.swift) with new parts.
import AppKit
import SwiftUI
import QuartzCore

final class R3Defaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class R3Client: DaemonCalling, @unchecked Sendable {
    var views: [String: ApprovalView] = [:]
    var requests: [String: JSONValue] = [:]
    var masked: [String: JSONValue] = [:]
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "approval.get" {
            let id = try JSONValue.parse(JSONEncoder().encode(args))["approval_id"]!.string!
            let view = try JSONValue.parse(JSONEncoder().encode(views[id]!))
            return try JSONValue.object(["approval": view, "nonce": .string("n"), "request_sha256": .string("s"),
                                         "request": requests[id]!, "masked": masked[id] ?? .array([])]).decode(R.self)
        }
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("round-three probe never contacts a daemon")
    }
}

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

@MainActor func backing(_ host: NSView, _ size: CGSize) -> NSWindow {
    let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: size.width, height: size.height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
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

@MainActor func snapshot(_ host: NSView, to url: URL) throws {
    let size = host.bounds.size
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = size
    host.cacheDisplay(in: host.bounds, to: rep)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
}

func tesseract(_ png: URL, _ extra: [String]) throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["R3_TESSERACT"]!)
    process.arguments = [png.path, "stdout", "--psm", "6"] + extra
    let output = Pipe()
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try process.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
}
func ocr(_ png: URL) throws -> String { try tesseract(png, []) }
/// Word boxes (top y in points at 2x capture) for words matching any of `words`.
func wordTops(_ png: URL, _ words: [String]) throws -> [String: Double] {
    var tops: [String: Double] = [:]
    for line in try tesseract(png, ["tsv"]).split(separator: "\n").dropFirst() {
        let cols = line.split(separator: "\t", omittingEmptySubsequences: false)
        guard cols.count >= 12 else { continue }
        let text = String(cols[11]).trimmingCharacters(in: .whitespaces)
        for word in words where text.hasPrefix(word) && tops[word] == nil { tops[word] = (Double(cols[7]) ?? -2) / 2 }
    }
    return tops
}

/// AppKit-backed controls inside the host: class, title, frame in host coordinates, and whether
/// the frame lies inside the host's bounds (a control outside them is not visible).
@MainActor func controls(_ host: NSView) -> [[String: Any]] {
    descendants(host).compactMap { view -> [String: Any]? in
        let name = String(describing: type(of: view))
        guard name.contains("Button") || view is NSButton else { return nil }
        let f = view.convert(view.bounds, to: host)
        let title = (view as? NSButton)?.title ?? (view.accessibilityLabel() ?? "")
        let inside = f.minY >= host.bounds.minY - 0.5 && f.maxY <= host.bounds.maxY + 0.5
        return ["class": name, "title": title, "frame": [f.minX, f.minY, f.width, f.height].map { Int($0) }, "inside": inside]
    }
}

/// Fitting render (the view at its own height, capped) and a render inside a fixed offer.
@MainActor func render<V: View>(_ view: V, width: CGFloat, height: CGFloat? = nil, scale: Double = 1, dark: Bool = false,
                                to url: URL) async throws -> [String: Any] {
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let base = view.environment(\.textScale, scale).environment(\.colorScheme, dark ? .dark : .light)
    let host: NSView
    if let height {
        host = NSHostingView(rootView: base.frame(width: width, height: height).background(Theme.surface.raised.color))
    } else {
        host = NSHostingView(rootView: base.frame(width: width).fixedSize(horizontal: false, vertical: true)
            .background(Theme.surface.conversation.color))
    }
    let window = backing(host, CGSize(width: width, height: height ?? 600))
    defer { window.close() }
    await settle(host)
    let fit = host.fittingSize
    if height == nil { host.frame = NSRect(x: 0, y: 0, width: width, height: min(3600, max(40, fit.height))) }
    await settle(host, 300)
    try snapshot(host, to: url)
    return ["fitting_height": fit.height, "frame_height": host.bounds.height, "ocr": try ocr(url), "controls": controls(host)]
}

func pretty(_ value: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!
}

@main struct R3Probe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        NSApp.appearance = NSAppearance(named: .aqua)
        let scenesURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let out = URL(fileURLWithPath: CommandLine.arguments[2])
        let only = ProcessInfo.processInfo.environment["R3_PARTS"]?.split(separator: ",").map(String.init)
        let onlyScenes = ProcessInfo.processInfo.environment["R3_SCENES"]?.split(separator: ",").map(String.init)
        func want(_ part: String) -> Bool { only?.contains(part) ?? true }
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let root = out.appendingPathComponent(".state-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        var result: [String: Any] = [:]
        let stamp = ISO8601DateFormatter()
        let now = stamp.string(from: Date())

        func conversation(_ id: String, provider: String, title: String, updated: String? = nil, pending: Int = 0) -> Conversation {
            Conversation(conversation_id: id, provider: provider, title: title, workspace: "/Users/example/subfleet",
                         workspace_kind: "in-place", allow_main: false, lane_id: provider == "codex" ? "codex-2" : "claude-2",
                         settings: ConversationSettings(model: provider == "codex" ? "gpt-6.1-sol" : "claude-opus-5-5",
                                                        effort: "high", fast: false),
                         origin: "person", blocked_by: nil, created_at: updated ?? now, updated_at: updated ?? now,
                         pending_approvals: pending, active: false, live_elsewhere: false)
        }
        func baseState() -> ConversationStoreState {
            var state = ConversationStoreState()
            state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
                capabilities: ["conversation.v1", "diff.v1", "steer.v1", "workspace.check.v1"], codex_writable: true,
                steer_providers: ["claude"]))
            state.laneLabels = ["claude-2": "max@example.com", "codex-2": "max@example.com"]
            return state
        }

        // MARK: 1. Approval card and sheet: heights, actions inside bounds, field order, kind
        if want("approvals") {
            let scenes = try JSONValue.parse(Data(contentsOf: scenesURL)).object!
            let client = R3Client()
            var state = baseState()
            state.upsert(conversation("c0", provider: "claude", title: "Approvals"))
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("approvals")), client: client,
                                defaults: R3Defaults(), state: state)
            let scales = (ProcessInfo.processInfo.environment["R3_SCALES"] ?? "1").split(separator: ",").compactMap { Double($0) }
            let modes = (ProcessInfo.processInfo.environment["R3_MODES"] ?? "light").split(separator: ",").map(String.init)
            var approvals: [String: Any] = [:]
            for id in scenes.keys.sorted() where onlyScenes?.contains(id) ?? true {
                let scene = scenes[id]!
                let kind = scene["kind"]!.string!
                let display = ApprovalDisplay(fields: scene["display"]!.object!)
                let options = scene["options"]!.array!.compactMap(\.string)
                client.views[id] = ApprovalView(approval_id: id, message_id: "m1", conversation_id: "c0",
                    provider_request_id: "r-\(id)", kind: kind, display: display, options: options,
                    created_at: now, state: "pending", request_id: "r-\(id)")
                client.requests[id] = scene["request"]!
                client.masked[id] = scene["masked"]
                let card = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .pending)
                let request = scene["request"]!
                var entry: [String: Any] = [
                    "command_key_loaded": ApprovalPresentation.commandFieldKey(card, request: request) as Any? ?? NSNull(),
                    "fields_loaded": ApprovalPresentation.grantedFields(card, request: request).map { "\($0.key): \($0.value.prefix(90))" },
                    "fields_before_load": ApprovalPresentation.grantedFields(card, request: nil).map { "\($0.key): \($0.value.prefix(90))" },
                    "headline": ApprovalPresentation.headline(card),
                ]
                for mode in modes {
                    for scale in scales {
                        let tag = "\(mode)-\(scale)"
                        entry["card-\(tag)"] = try await render(
                            ApprovalCardView(model: model, conversationID: "c0", card: card, review: {}),
                            width: 720, scale: scale, dark: mode == "dark", to: out.appendingPathComponent("card-\(id)-\(tag).png"))
                        entry["sheet-\(tag)"] = try await render(
                            ApprovalSheet(model: model, card: card, approvalID: id, done: {}),
                            width: 560, scale: scale, dark: mode == "dark", to: out.appendingPathComponent("sheet-\(id)-\(tag).png"))
                        entry["sheet-in-400-\(tag)"] = try await render(
                            ApprovalSheet(model: model, card: card, approvalID: id, done: {}),
                            width: 560, height: 400, scale: scale, dark: mode == "dark",
                            to: out.appendingPathComponent("sheet-in-400-\(id)-\(tag).png"))
                        if scene["masked"] != nil {
                            entry["sheet-confirmed-\(tag)"] = try await render(
                                ApprovalSheet(model: model, card: card, approvalID: id, done: {}).confirmingMaskedValuesForSnapshot(),
                                width: 560, scale: scale, dark: mode == "dark",
                                to: out.appendingPathComponent("sheet-confirmed-\(id)-\(tag).png"))
                        }
                    }
                }
                // Size a hosting controller proposes for the sheet, as a window would.
                let controller = NSHostingController(rootView: ApprovalSheet(model: model, card: card, approvalID: id, done: {})
                    .environment(\.textScale, 1))
                let window = backing(controller.view, CGSize(width: 560, height: 800))
                await settle(controller.view, 900)
                var fits: [String: Any] = [:]
                for height in [300.0, 400.0, 800.0, 1117.0] {
                    fits["fits_in_\(Int(height))"] = controller.sizeThatFits(in: CGSize(width: 560, height: height)).height
                }
                fits["preferred"] = controller.view.fittingSize.height
                window.close()
                entry["sheetfit"] = fits
                // History: settled card, with and without answers this app recorded.
                if kind == "question" {
                    let answers = Dictionary(uniqueKeysWithValues: card.questions.map { ($0.question, $0.options?.first?.label ?? "Other") })
                    for (label, recorded) in [("with-answers", answers), ("no-answers", [:])] {
                        let settled = ApprovalCard(approvalID: id, kind: kind, display: display, options: options,
                                                   state: .answered("answer"), answers: recorded)
                        entry["answered-\(label)"] = try await render(
                            ApprovalCardView(model: model, conversationID: "c0", card: settled, review: {}), width: 720,
                            to: out.appendingPathComponent("card-answered-\(label)-\(id).png"))
                    }
                    let pendingPNG = out.appendingPathComponent("card-\(id)-light-1.0.png")
                    if FileManager.default.fileExists(atPath: pendingPNG.path) {
                        entry["question_word_tops"] = try wordTops(pendingPNG, ["Choose", "Submit", "Next", "Details", "Option", "1"])
                    }
                }
                approvals[id] = entry
                log("approval \(id) done")
            }
            result["approvals"] = approvals
        }

        // MARK: 2. Codex row labels for recorded and constructed shell commands
        if want("labels"), let path = ProcessInfo.processInfo.environment["R3_COMMANDS"] {
            let commands = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: path))).array!
            result["labels"] = commands.map { entry -> [String: Any] in
                let command = entry["command"]!.string!
                return ["note": entry["note"]?.string ?? "", "script_head": String(ShellCommandPresentation.script(command).prefix(80)),
                        "label": ToolActivity(name: "command", summary: command, hidden: false, state: .running).label]
            }
        }

        // MARK: 3. Answers kept through replay: can they cross to another approval?
        if want("answers") {
            let questions = try JSONValue.from([ApprovalQuestion(question: "Which scope?", options: [.init(label: "Local"), .init(label: "Global")])])
            func view(_ id: String, message: String, request: String, state: String) -> ApprovalView {
                ApprovalView(approval_id: id, message_id: message, conversation_id: "c", provider_request_id: request,
                             kind: "question", display: ApprovalDisplay(fields: ["questions": questions]),
                             options: ["answer", "deny"], created_at: now, state: state, request_id: request)
            }
            func requested(_ seq: Int, _ message: String, _ request: String) -> ConversationEvent {
                ConversationEvent(seq: seq, message_id: message, kind: "approval.requested",
                    data: .object(["request_id": .string(request), "kind": .string("question"), "questions": questions,
                                   "options": .array([.string("answer"), .string("deny")])]))
            }
            func resolved(_ seq: Int, _ message: String, _ request: String, _ decision: String) -> ConversationEvent {
                ConversationEvent(seq: seq, message_id: message, kind: "approval.resolved",
                    data: .object(["request_id": .string(request), "decision": .string(decision)]))
            }
            func cards(_ t: Timeline) -> [String] {
                t.items.compactMap(\.card).map { "\($0.approvalID ?? "nil")|req=\($0.requestID ?? "nil")|\($0.isPending ? "pending" : "settled")|answers=\($0.answers)" }
            }
            var scenarios: [String: Any] = [:]
            // a) Realistic: two messages, unique request ids (Claude ids are crypto.randomUUID()).
            var a = Timeline(conversationID: "c")
            let aEvents = [requested(1, "m1", "u-1"), resolved(2, "m1", "u-1", "answer"), requested(3, "m2", "u-2")]
            _ = a.apply(events: aEvents)
            a.attach(approvals: [view("A1", message: "m1", request: "u-1", state: "answered"), view("A2", message: "m2", request: "u-2", state: "pending")])
            a.noteApprovalAnswer(approvalID: "A1", answers: ["Which scope?": "Local"])
            let aBefore = cards(a)
            a.resetEvents()
            _ = a.apply(events: aEvents)
            scenarios["a_two_messages_unique_ids"] = ["before_reset": aBefore, "after_replay": cards(a)]
            // b) A replacement in the same message (first withdrawn, asked again with a new id).
            var b = Timeline(conversationID: "c")
            let bEvents = [requested(1, "m1", "u-1"), resolved(2, "m1", "u-1", "answer"), requested(3, "m1", "u-3")]
            _ = b.apply(events: bEvents)
            b.attach(approvals: [view("B1", message: "m1", request: "u-1", state: "answered"), view("B2", message: "m1", request: "u-3", state: "pending")])
            b.noteApprovalAnswer(approvalID: "B1", answers: ["Which scope?": "Local"])
            b.resetEvents()
            _ = b.apply(events: bEvents)
            b.attach(approvals: [view("B2", message: "m1", request: "u-3", state: "pending")])
            scenarios["b_replacement_same_message"] = cards(b)
            // c) Constructed collision: one message, two approvals sharing a provider request id
            //    (not produced by Claude's randomUUID ids; Codex questions are refused by the driver).
            var c = Timeline(conversationID: "c")
            c.attach(approvals: [view("C1", message: "m1", request: "dup", state: "answered"), view("C2", message: "m1", request: "dup", state: "pending")])
            c.noteApprovalAnswer(approvalID: "C1", answers: ["Which scope?": "Local"])
            c.resetEvents()
            _ = c.apply(events: [requested(1, "m1", "dup"), resolved(2, "m1", "dup", "answer"), requested(3, "m1", "dup")])
            scenarios["c_constructed_collision"] = cards(c)
            // d) Event first, view later, answer, compaction drops the old request event, replay.
            var d = Timeline(conversationID: "c")
            _ = d.apply(events: [requested(1, "m1", "u-1"), resolved(2, "m1", "u-1", "answer")])
            d.attach(approvals: [view("D1", message: "m1", request: "u-1", state: "answered")])
            d.noteApprovalAnswer(approvalID: "D1", answers: ["Which scope?": "Global"])
            _ = d.apply(page: EventsPage(events: [], next: 2, reset: true, superseded: nil, floor: 2))
            _ = d.apply(page: EventsPage(events: [resolved(2, "m1", "u-1", "answer")], next: 2, reset: true, superseded: nil, floor: 2))
            scenarios["d_compacted_request_event"] = cards(d)
            result["answers"] = scenarios
        }

        // MARK: 4. Sidebar: arrow keys across section headings, and the selected row's highlight
        if want("sidebar") {
            var state = baseState()
            let today = stamp.string(from: Date())
            let yesterday = stamp.string(from: Date().addingTimeInterval(-86400 * 1.2))
            let older = stamp.string(from: Date().addingTimeInterval(-86400 * 6))
            for i in 0..<9 {
                state.upsert(conversation("c\(i)", provider: i % 2 == 0 ? "claude" : "codex", title: "Conversation \(i)",
                                          updated: i < 3 ? today : i < 6 ? yesterday : older, pending: i == 1 ? 2 : 0))
            }
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("sidebar")), client: R3Client(),
                                defaults: R3Defaults(), state: state)
            var selection: String? = "cv:c0"
            var writes: [String] = []
            let binding = Binding<String?>(get: { selection }, set: { selection = $0; writes.append($0 ?? "nil") })
            let host = NSHostingView(rootView: SidebarView(model: model, selection: binding)
                .environment(\.textScale, 1).environment(\.colorScheme, .light).frame(width: 300, height: 760))
            let window = backing(host, CGSize(width: 300, height: 760))
            await settle(host, 800)
            let table = descendants(host).compactMap { $0 as? NSTableView }.first
            var sidebar: [String: Any] = ["has_table": table != nil]
            if let table {
                sidebar["rows"] = table.numberOfRows
                window.makeFirstResponder(table)
                var path: [String] = [selection ?? "nil"]
                var selectedRows: [Int] = [table.selectedRow]
                for (key, code, count) in [("\u{F701}", UInt16(125), 10), ("\u{F700}", UInt16(126), 10)] {
                    for _ in 0..<count {
                        let e = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                            windowNumber: window.windowNumber, context: nil, characters: key, charactersIgnoringModifiers: key,
                            isARepeat: false, keyCode: code)!
                        table.keyDown(with: e)
                        try await Task.sleep(nanoseconds: 150_000_000)
                        path.append(selection ?? "nil")
                        selectedRows.append(table.selectedRow)
                    }
                }
                sidebar["arrow_path"] = path
                sidebar["table_selected_rows"] = selectedRows
                sidebar["binding_writes"] = writes
            }
            selection = "cv:c4"
            await settle(host, 400)
            try snapshot(host, to: out.appendingPathComponent("sidebar-selected-c4-light.png"))
            window.close()
            result["sidebar"] = sidebar
            for (dark, name) in [(false, "light"), (true, "dark")] {
                NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                let h = NSHostingView(rootView: SidebarView(model: model, selection: .constant("cv:c1"))
                    .environment(\.textScale, 1).environment(\.colorScheme, dark ? .dark : .light).frame(width: 300, height: 700))
                let w = backing(h, CGSize(width: 300, height: 700))
                await settle(h, 700)
                if let t = descendants(h).compactMap({ $0 as? NSTableView }).first {
                    t.window?.makeFirstResponder(t)
                    await settle(h, 300)
                }
                try snapshot(h, to: out.appendingPathComponent("sidebar-c1-\(name).png"))
                w.close()
            }
        }

        // MARK: 5. Expanding Details on an answered card (history) in this session
        if want("details") {
            var state = baseState()
            state.upsert(conversation("c0", provider: "claude", title: "Details"))
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent("details")), client: R3Client(),
                                defaults: R3Defaults(), state: state)
            let settled = ApprovalCard(requestID: "r1", approvalID: "a1", kind: "tool",
                display: ApprovalDisplay(fields: ["tool": .string("Bash"), "description": .string("Show status"),
                                                  "input": .string("git status")]),
                options: ["allow", "deny", "cancel-turn"], state: .answered("allow"))
            NSApp.appearance = NSAppearance(named: .aqua)
            let host = NSHostingView(rootView: ApprovalCardView(model: model, conversationID: "c0", card: settled, review: {})
                .environment(\.textScale, 1).frame(width: 720).fixedSize(horizontal: false, vertical: true))
            let window = backing(host, CGSize(width: 720, height: 300))
            await settle(host)
            var details: [String: Any] = ["problem_before": model.problem ?? "nil"]
            let buttons = descendants(host).compactMap { $0 as? NSButton }
            details["buttons"] = buttons.map { "\(type(of: $0)) bezel=\($0.bezelStyle.rawValue) title='\($0.title)'" }
            var pressed = 0
            for b in buttons where b.bezelStyle == .disclosure || b.title == "Details" {
                b.performClick(nil)
                pressed += 1
            }
            details["disclosures_pressed"] = pressed
            if pressed == 0 {
                // Locate "Details" by OCR on a capture, then click its disclosure row in the unshown window.
                let before = out.appendingPathComponent("details-before.png")
                try snapshot(host, to: before)
                var box: [Double]? = nil
                for line in try tesseract(before, ["tsv"]).split(separator: "\n").dropFirst() {
                    let c = line.split(separator: "\t", omittingEmptySubsequences: false)
                    if c.count >= 12, String(c[11]).hasPrefix("Details") {
                        box = [Double(c[6])!, Double(c[7])!, Double(c[8])!, Double(c[9])!].map { $0 / 2 }
                    }
                }
                details["details_box_pt"] = box ?? []
                log("details: box \(String(describing: box))")
                if let box {
                    let flippedY = host.bounds.height - (box[1] + box[3] / 2)
                    for x in [box[0] + box[2] / 2, box[0] - 8] {
                        let point = host.convert(NSPoint(x: x, y: flippedY), to: nil)
                        func mouse(_ type: NSEvent.EventType) -> NSEvent {
                            NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
                        }
                        log("details: click at \(x),\(flippedY)")
                        window.sendEvent(mouse(.leftMouseDown))
                        log("details: down sent")
                        window.sendEvent(mouse(.leftMouseUp))
                        log("details: up sent")
                        await settle(host, 900)
                        log("details: problem \(model.problem ?? "nil")")
                        if model.problem != nil { details["clicked_at"] = [x, flippedY]; break }
                    }
                }
            }
            await settle(host, 900)
            details["problem_after_expanding"] = model.problem ?? "nil"
            try snapshot(host, to: out.appendingPathComponent("details-expanded-answered.png"))
            window.close()
            // The same call load() makes, without the view: the model's identity check for a settled card.
            model.problem = nil
            let id = await model.approvalID(for: settled, conversationID: "c0")
            details["direct_approvalID_result"] = id ?? "nil"
            details["direct_problem"] = model.problem ?? "nil"
            result["details"] = details
        }

        result["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        print(pretty(result))
    }
}
