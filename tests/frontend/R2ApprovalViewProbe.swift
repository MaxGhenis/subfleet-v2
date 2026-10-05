// Regression probe adapted from the round-two review evidence for PR #128.
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
    var masked: [String: JSONValue] = [:]
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "approval.get" {
            let id = try JSONValue.parse(JSONEncoder().encode(args))["approval_id"]!.string!
            gets.append(id)
            let view = try JSONValue.parse(JSONEncoder().encode(views[id]!))
            return try JSONValue.object(["approval": view, "nonce": .string("n"), "request_sha256": .string("s"),
                                         "request": requests[id]!, "masked": masked[id] ?? .array([])]).decode(R.self)
        }
        if op.name == "conversation.runs" { return try JSONValue.object(["runs": .array([])]).decode(R.self) }
        throw DaemonClientError.unavailable("round-two probe never contacts a daemon")
    }
}

@MainActor var scrollEvidence: [String: Any] = [:]
@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

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
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let root = view.environment(\.textScale, 1).environment(\.colorScheme, dark ? .dark : .light)
        .frame(width: width).fixedSize(horizontal: false, vertical: true)
        .background(Theme.surface.conversation.color)
    let host = NSHostingView(rootView: root)
    let window = backing(host, CGSize(width: width, height: 600))
    defer { window.close() }
    await settle(host)
    let fit = host.fittingSize
    host.frame = NSRect(x: 0, y: 0, width: width, height: min(cap, max(40, fit.height)))
    await settle(host, 300)
    _ = try snapshot(host, to: url)
    let text = try ocr(url)
    let scrolls = descendants(host).compactMap { $0 as? NSScrollView }.filter {
        ($0.documentView?.bounds.height ?? 0) > $0.contentView.bounds.height + 1
    }
    for scroll in scrolls {
        if let doc = scroll.documentView {
            doc.scroll(NSPoint(x: 0, y: doc.bounds.maxY - scroll.contentView.bounds.height))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }
    if !scrolls.isEmpty {
        await settle(host, 100)
        let tailURL = url.deletingPathExtension().appendingPathExtension("scrolled.png")
        _ = try snapshot(host, to: tailURL)
        scrollEvidence[url.lastPathComponent] = ["tail_ocr": try ocr(tailURL), "scroll_count": scrolls.count]
    }

    return (fit, text)
}

func pretty(_ value: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]), encoding: .utf8)!
}

@main struct R2ApprovalViewProbe {
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
                client.masked[id] = scene["masked"]
                let card = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .pending)
                let request = scene["request"]!
                var entry: [String: Any] = [
                    "command_loaded": ApprovalPresentation.command(card, request: request) as Any? ?? NSNull(),
                    "command_before_load": ApprovalPresentation.command(card, request: nil) as Any? ?? NSNull(),
                    "fields_loaded": ApprovalPresentation.grantedFields(card, request: request).map { "\($0.key): \($0.value)" },
                    "fields_before_load": ApprovalPresentation.grantedFields(card, request: nil).map { "\($0.key): \($0.value)" },
                ]
                for dark in [false, true] {
                    let cardURL = out.appendingPathComponent("card-\(id)-\(dark ? "dark" : "light").png")
                    let (cardSize, cardText) = try await renderFitting(
                        ApprovalCardView(model: model, conversationID: "c0", card: card, review: {}), width: 720, dark: dark, to: cardURL)
                    let sheetURL = out.appendingPathComponent("sheet-\(id)-\(dark ? "dark" : "light").png")
                    let (sheetSize, sheetText) = try await renderFitting(
                        ApprovalSheet(model: model, card: card, approvalID: id, done: {}), width: 560, dark: dark, to: sheetURL)
                    entry[dark ? "dark" : "light"] = ["card_height": cardSize.height, "sheet_height": sheetSize.height, "card_ocr": cardText, "sheet_ocr": sheetText]
                    entry["card_height"] = cardSize.height
                    entry["sheet_height"] = sheetSize.height
                    entry["card_ocr"] = cardText
                    entry["sheet_ocr"] = sheetText
                    if scene["masked"] != nil {
                        let confirmed = ApprovalSheet(model: model, card: card, approvalID: id, done: {}).confirmingMaskedValuesForSnapshot()
                        let confirmedURL = out.appendingPathComponent("sheet-\(id)-\(dark ? "dark" : "light")-confirmed.png")
                        let (confirmedSize, confirmedText) = try await renderFitting(confirmed, width: 560, dark: dark, to: confirmedURL)
                        entry["confirmed_" + (dark ? "dark" : "light")] = ["height": confirmedSize.height, "ocr": confirmedText]
                    }
                }
                // The same card once answered: history shows each question and answer once.
                let settled = ApprovalCard(approvalID: id, kind: kind, display: display, options: options, state: .answered(kind == "question" ? "answer" : "allow"),
                    answers: Dictionary(uniqueKeysWithValues: card.questions.map { ($0.question, $0.options?.first?.label ?? "Custom answer") }))
                for dark in [false, true] {
                    let (settledSize, settledText) = try await renderFitting(
                        ApprovalCardView(model: model, conversationID: "c0", card: settled, review: {}), width: 720, dark: dark,
                        to: out.appendingPathComponent("card-answered-\(id)-\(dark ? "dark" : "light").png"))
                    entry["answered_card_height"] = settledSize.height
                    entry["answered_card_ocr"] = settledText
                }
                approvals[id] = entry
                log("approval \(id) card \(entry["card_height"]!) sheet \(entry["sheet_height"]!)")
            }
            result["approvals"] = approvals
            result["approval_gets"] = client.gets
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
            for id in scenes.keys.sorted() {
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

        result["scrolling"] = scrollEvidence
        result["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        print(pretty(result))
    }
}
