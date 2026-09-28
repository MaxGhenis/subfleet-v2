// Host the conversation window's real views without opening a window or running
// the app: the pinned strip with and without waiting cards, and a sidebar row
// with and without its hand badge. SwiftUI builds no accessibility tree without
// an assistive client, so a control is seen by the room it takes, and the
// AppKit-backed ones (link and borderless buttons) are clicked.
import AppKit
import SwiftUI

@MainActor final class Presses {
    var review = 0, stop = 0, badge = 0
}

@MainActor func host<V: View>(_ view: V, width: CGFloat) -> (size: CGSize, buttons: [NSButton], window: NSWindow) {
    let hosting = NSHostingView(rootView: view)
    let size = hosting.fittingSize
    hosting.frame = NSRect(x: 0, y: 0, width: width, height: max(size.height, 24))
    // Never ordered front: a layout test must not show a window.
    let window = NSWindow(contentRect: hosting.frame, styleMask: [.borderless], backing: .buffered, defer: false)
    window.contentView = hosting
    hosting.layoutSubtreeIfNeeded()
    func all(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(all) }
    return (size, all(hosting).compactMap { $0 as? NSButton }, window)
}

@main
struct ConversationViewProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var windows: [NSWindow] = []
        var strips: [[String: Any]] = []
        for pending in [0, 1, 2, 12] {
            let presses = Presses()
            // One state for all, so only the Review button changes the room the strip takes.
            let turn = TurnTimeline(messageID: "m7", state: "approval-needed")
            let strip = LiveTurnStrip(turn: turn, pendingApprovals: pending,
                                      review: { presses.review += 1 }, stop: { presses.stop += 1 })
            let hosted = host(strip, width: 640)
            windows.append(hosted.window)
            hosted.buttons.forEach { $0.performClick(nil) }
            strips.append(["pending": pending, "width": hosted.size.width, "height": hosted.size.height,
                           "appkit_buttons": hosted.buttons.count, "review": presses.review, "stop": presses.stop,
                           "label": reviewButtonLabel(pending: pending) as Any? ?? NSNull()])
        }
        var rows: [[String: Any]] = []
        for pending in [0, 2] {
            let presses = Presses()
            let entry = SidebarEntry(id: "cv:cv-1", target: .conversation("cv-1"), provider: "claude",
                                     title: "Subfleet desktop transition", subtitle: "~/subfleet-v2", workspace: nil,
                                     date: nil, pendingApprovals: pending, active: true, blockedBy: nil,
                                     liveElsewhere: false, continuable: true, continueBlocker: nil)
            let hosted = host(SidebarRow(entry: entry, showApprovals: { presses.badge += 1 }), width: 300)
            windows.append(hosted.window)
            hosted.buttons.forEach { $0.performClick(nil) }
            rows.append(["pending": pending, "width": hosted.size.width, "appkit_buttons": hosted.buttons.count,
                         "badge": presses.badge, "spoken": approvalsWaitingWords(pending)])
        }
        let result: [String: Any] = ["strips": strips, "rows": rows,
                                     "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
