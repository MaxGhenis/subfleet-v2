// Adapted from the review's SidebarProbe and R3Probe. All windows stay unshown.
import AppKit
import SwiftUI

final class R3FixDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func object(forKey key: String) -> Any? { values[key] }
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}
final class R3FixClient: DaemonCalling, @unchecked Sendable {
    let fixture: JSONValue
    let codex: JSONValue
    init(_ fixture: JSONValue, codex: JSONValue) { self.fixture = fixture; self.codex = codex }
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "approval.get" {
            let id = try JSONValue.from(args)["approval_id"]!.string!
            let isCodex = id == "pending-codex", pending = id.hasPrefix("pending")
            let current = isCodex ? codex : fixture
            let approval = ApprovalView(approval_id: id, message_id: "m", conversation_id: "c1", kind: isCodex ? "command" : "tool",
                display: ApprovalDisplay(fields: current["display"]!.object!),
                options: isCodex ? ["allow", "allow-session", "deny", "cancel-turn"] : ["allow", "deny"],
                created_at: "2026-10-05T12:00:00Z", state: pending ? "pending" : id)
            let request: JSONValue = pending ? current["request"]! : .object([
                "subtype": .string("can_use_tool"), "tool_name": .string("Bash"),
                "input": .object(["command": .string("git status")])])
            return try JSONValue.from(ApprovalDetail(approval: approval, request: request,
                masked: pending ? [MaskedSpan(path: isCodex ? "params.command" : "input.content", rule: "fixture", length: 6, sha256: "fixture")] : [],
                request_sha256: "fixture", nonce: "fixture")).decode(R.self)
        }
        throw DaemonClientError.unavailable("The round-three probe never contacts a daemon")
    }
}
@MainActor func r3Descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(r3Descendants) }
@MainActor func r3Settle(_ host: NSView) async {
    host.layoutSubtreeIfNeeded()
    if let warm = host.bitmapImageRepForCachingDisplay(in: host.bounds) { host.cacheDisplay(in: host.bounds, to: warm) }
    try? await Task.sleep(nanoseconds: 800_000_000)
    host.layoutSubtreeIfNeeded()
}
func r3OCR(_ url: URL, tsv: Bool = false) throws -> String {
    let child = Process(), pipe = Pipe()
    child.executableURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["R3_TESSERACT"]!)
    child.arguments = [url.path, "stdout", "--psm", "6"]
    if tsv { child.arguments!.append("tsv") }
    child.standardOutput = pipe; child.standardError = FileHandle.nullDevice
    try child.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    child.waitUntilExit()
    precondition(child.terminationStatus == 0)
    return String(data: data, encoding: .utf8)!
}
@MainActor func r3Capture<V: View>(_ view: V, size: CGSize, dark: Bool, scale: Double, url: URL,
                                 focusList: Bool = false, fittingHeight: Bool = false) async throws -> [String: Any] {
    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let host = NSHostingView(rootView: view.environment(\.colorScheme, dark ? .dark : .light)
        .environment(\.textScale, scale).frame(width: size.width)
        .fixedSize(horizontal: false, vertical: fittingHeight)
        .background(Theme.surface.conversation.color))
    let window = NSWindow(contentRect: NSRect(origin: NSPoint(x: -10000, y: -10000), size: size),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false; window.appearance = NSApp.appearance; window.contentView = host
    defer { window.close() }
    host.frame = NSRect(origin: .zero, size: size)
    await r3Settle(host)
    if fittingHeight {
        host.frame.size.height = min(720, host.fittingSize.height)
        await r3Settle(host)
    }
    if focusList, let table = r3Descendants(host).compactMap({ $0 as? NSTableView }).first {
        window.makeFirstResponder(table)
        await r3Settle(host)
    }
    let captureSize = host.bounds.size
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(captureSize.width * 2), pixelsHigh: Int(captureSize.height * 2),
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = captureSize; host.cacheDisplay(in: host.bounds, to: rep)
    try rep.representation(using: .png, properties: [:])!.write(to: url)
    var result: [String: Any] = ["ocr": try r3OCR(url), "height": captureSize.height, "width": captureSize.width,
        "scroll_heights": r3Descendants(host).compactMap { ($0 as? NSScrollView)?.contentView.bounds.height }]
    if fittingHeight {
        var actionBottoms: [String: Double] = [:]
        for line in try r3OCR(url, tsv: true).split(separator: "\n").dropFirst() {
            let parts = line.split(separator: "\t", omittingEmptySubsequences: false)
            if parts.count >= 12, ["Allow", "Deny", "Cancel"].contains(String(parts[11])),
               let y = Double(parts[7]), let height = Double(parts[9]) {
                actionBottoms[String(parts[11])] = (y + height) / 2
            }
        }
        result["action_bottoms"] = actionBottoms
    }
    if focusList || url.lastPathComponent.hasPrefix("sidebar") {
        func rgb(_ x: Int, _ y: Int) -> UInt32 {
            let color = rep.colorAt(x: x, y: y)!.usingColorSpace(.sRGB)!
            return UInt32((color.redComponent * 255).rounded()) << 16 |
                UInt32((color.greenComponent * 255).rounded()) << 8 | UInt32((color.blueComponent * 255).rounded())
        }
        var amber: [(Int, Int, UInt32)] = []
        for y in 0..<rep.pixelsHigh {
            for x in Int(Double(rep.pixelsWide) * 0.8)..<rep.pixelsWide {
                let value = rgb(x, y)
                if Int((value >> 16) & 255) - Int(value & 255) > 60 { amber.append((x, y, value)) }
            }
        }
        precondition(!amber.isEmpty, "The count must be visible")
        let glyph = amber.max { a, b in
            Int((a.2 >> 16) & 255) - Int(a.2 & 255) < Int((b.2 >> 16) & 255) - Int(b.2 & 255)
        }!.2
        let x = min(rep.pixelsWide - 1, amber.map { $0.0 }.max()! + 4)
        let y = (amber.map { $0.1 }.min()! + amber.map { $0.1 }.max()!) / 2
        result["badge_contrast"] = Theme.contrast(glyph, rgb(x, y))
    }
    return result
}

@main struct R3FixViewProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let scenes = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let out = URL(fileURLWithPath: CommandLine.arguments[2])
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let root = out.appendingPathComponent("state")
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = scenes["masked-write-120-lines"]!
        var codex = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[3]))).array!.first!.object!
        var codexRequest = codex["request"]!.object!, params = codex["request"]!["params"]!.object!
        params["command"] = .string("/bin/zsh -lc 'printf [MASKED]'")
        codexRequest["params"] = .object(params); codex["request"] = .object(codexRequest)
        var state = ConversationStoreState()
        state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
            capabilities: ["conversation.v1"], codex_writable: true))
        let now = ISO8601DateFormatter().string(from: Date())
        for i in 0..<3 {
            state.upsert(Conversation(conversation_id: "c\(i)", provider: "claude", title: "Conversation \(i)",
                workspace: "/repo", workspace_kind: "in-place", allow_main: false,
                settings: ConversationSettings(model: "claude-opus-5-5", effort: "high", fast: false), origin: "person",
                created_at: now, updated_at: now, pending_approvals: i == 1 ? 2 : 0, active: false))
        }
        let model = UIModel(paths: .rooted(at: root), client: R3FixClient(fixture, codex: .object(codex)), defaults: R3FixDefaults(), state: state)
        let display = ApprovalDisplay(fields: fixture["display"]!.object!)
        var result: [String: Any] = [:], sidebar: [String: Any] = [:], history: [String: Any] = [:]
        for dark in [false, true] {
            for focused in [false, true] {
                let name = "\(dark ? "dark" : "light")-\(focused ? "focused" : "unfocused")"
                sidebar[name] = try await r3Capture(SidebarView(model: model, selection: .constant("cv:c1")),
                    size: CGSize(width: 300, height: 400), dark: dark, scale: 1,
                    url: out.appendingPathComponent("sidebar-\(name).png"), focusList: focused)
            }
        }
        for (id, status) in [("answered", ApprovalCard.State.answered("allow")), ("withdrawn", .withdrawn)] {
            model.problem = "An unrelated notice"
            let card = ApprovalCard(approvalID: id, kind: "tool", display: display, options: ["allow", "deny"], state: status)
            let rendered = try await r3Capture(ApprovalCardView(model: model, conversationID: "c1", card: card, review: {})
                .showingDetailsForSnapshot(), size: CGSize(width: 720, height: 700), dark: false, scale: 1,
                url: out.appendingPathComponent("history-\(id).png"))
            history[id] = ["problem": model.problem ?? "nil", "ocr": rendered["ocr"]!]
            model.problem = nil
            let actionID = await model.approvalID(for: card, conversationID: "c1")
            history[id + "-action"] = ["id": actionID ?? "nil", "problem": model.problem ?? "nil"]
        }
        var large: [String: Any] = [:]
        for (name, id, kind, input) in [("write", "pending", "tool", fixture), ("codex", "pending-codex", "command", JSONValue.object(codex))] {
            let pending = ApprovalCard(approvalID: id, kind: kind, display: ApprovalDisplay(fields: input["display"]!.object!),
                options: ["allow", "deny"], state: .pending)
            for dark in [false, true] {
                let key = "\(name)-\(dark ? "dark" : "light")"
                large[key] = try await r3Capture(
                    ApprovalSheet(model: model, card: pending, approvalID: id, done: {}).confirmingMaskedValuesForSnapshot(),
                    size: CGSize(width: 900, height: 720), dark: dark, scale: TextScale.range.upperBound,
                    url: out.appendingPathComponent("large-\(key).png"), fittingHeight: true)
            }
        }
        result["sidebar"] = sidebar; result["history"] = history; result["large"] = large
        result["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    }
}
