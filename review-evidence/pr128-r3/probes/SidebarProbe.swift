// Round-three review probe: the selected sidebar row, list focused and unfocused, light and dark.
// Compiles against either tree (PR head or the round-two base 3e2f7f58); offscreen, unshown windows.
import AppKit
import SwiftUI

final class SPDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}
final class SPClient: DaemonCalling, @unchecked Sendable {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        throw DaemonClientError.unavailable("sidebar probe never contacts a daemon")
    }
}
@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

@main struct SidebarProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let out = URL(fileURLWithPath: CommandLine.arguments[1])
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        let root = out.appendingPathComponent(".state-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let now = ISO8601DateFormatter().string(from: Date())
        var state = ConversationStoreState()
        state.availability = .ready(Capabilities(protocol: 1, daemon_version: "fixture", conversation_schema: 1,
            capabilities: ["conversation.v1"], codex_writable: true, steer_providers: ["claude"]))
        for i in 0..<4 {
            state.upsert(Conversation(conversation_id: "c\(i)", provider: "claude", title: "Conversation \(i)",
                workspace: "/Users/example/subfleet", workspace_kind: "in-place", allow_main: false, lane_id: "claude-2",
                settings: ConversationSettings(model: "claude-opus-5-5", effort: "high", fast: false), origin: "person",
                blocked_by: nil, created_at: now, updated_at: now, pending_approvals: i == 1 ? 2 : 0, active: false,
                live_elsewhere: false))
        }
        let model = UIModel(paths: .rooted(at: root), client: SPClient(), defaults: SPDefaults(), state: state)
        for dark in [false, true] {
            for focused in [false, true] {
                NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                let host = NSHostingView(rootView: SidebarView(model: model, selection: .constant("cv:c1"))
                    .environment(\.colorScheme, dark ? .dark : .light).frame(width: 300, height: 400))
                let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 300, height: 400),
                                      styleMask: [.borderless], backing: .buffered, defer: false)
                window.isReleasedWhenClosed = false
                window.appearance = NSApp.appearance
                window.contentView = host
                host.frame = NSRect(x: 0, y: 0, width: 300, height: 400)
                host.layoutSubtreeIfNeeded()
                try? await Task.sleep(nanoseconds: 700_000_000)
                if focused, let table = descendants(host).compactMap({ $0 as? NSTableView }).first {
                    window.makeFirstResponder(table)
                }
                try? await Task.sleep(nanoseconds: 300_000_000)
                host.layoutSubtreeIfNeeded()
                let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 600, pixelsHigh: 800, bitsPerSample: 8,
                    samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                rep.size = host.bounds.size
                host.cacheDisplay(in: host.bounds, to: rep)
                let name = "sidebar-\(dark ? "dark" : "light")-\(focused ? "focused" : "unfocused").png"
                try rep.representation(using: .png, properties: [:])!.write(to: out.appendingPathComponent(name))
                window.close()
            }
        }
        print("visible windows \(NSApp.windows.filter(\.isVisible).count)")
    }
}
