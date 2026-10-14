// The real timeline notice view, hosted offscreen with and without a status.
import AppKit
import SwiftUI

@main
struct QueueTrayNoticeProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var windows: [NSWindow] = []
        var rows: [[String: Any]] = []
        for width: CGFloat in [300, 480] {
            for (name, withdrawn, words, status) in [
                ("short", true, "Withdrawn before it was sent: hello", true),
                ("long", true, "Withdrawn before it was sent: " + String(repeating: "word ", count: 500), true),
                ("notice", false, "Continuation queued", false),
                ("notice-status", false, "Continuation queued", true),
            ] {
                let controller = NSHostingController(rootView: ConversationNoticeRow(words: words, withdrawn: withdrawn,
                    status: status ? AnyView(Text("Withdrawn").readingFont(.caption)) : nil))
                let view = controller.view
                let size = controller.sizeThatFits(in: NSSize(width: width, height: 10_000))
                let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: size.height),
                                      styleMask: [.borderless], backing: .buffered, defer: false)
                window.contentView = view
                view.frame = NSRect(x: 0, y: 0, width: width, height: size.height)
                view.layoutSubtreeIfNeeded()
                windows.append(window)
                func all(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(all) }
                rows.append(["name": name, "width": width, "height": size.height,
                             "buttons": all(view).filter { $0 is NSButton }.count])
            }
        }
        let result: [String: Any] = ["rows": rows, "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
