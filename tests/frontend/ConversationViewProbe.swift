// Host the conversation window's real views without opening a window or running
// the app: the pinned strip with and without waiting cards, and a sidebar row
// with and without its hand badge (C-27.5). SwiftUI builds no accessibility tree
// without an assistive client, so a control is seen by the room it takes, and
// each AppKit-backed one (link and borderless buttons; bordered ones too before
// macOS 26) is clicked and named by the action it fired.
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
        for (pending, stops) in [(0, true), (1, true), (2, true), (12, true), (1, false)] {
            let presses = Presses()
            // One state for all, so only the Review button changes the room the strip takes.
            let turn = TurnTimeline(messageID: "m7", state: "approval-needed")
            let strip = LiveTurnStrip(turn: turn, pendingApprovals: pending,
                                      review: { presses.review += 1 }, stop: stops ? { presses.stop += 1 } : nil)
            let hosted = host(strip, width: 640)
            windows.append(hosted.window)
            // Which AppKit button does what: macOS 15 backs a bordered button with
            // one, macOS 26 draws it in SwiftUI, so Review is clicked only on the first.
            let clicks = hosted.buttons.map { button -> String in
                let before = (presses.review, presses.stop)
                button.performClick(nil)
                return presses.review > before.0 ? "review" : presses.stop > before.1 ? "stop" : "none"
            }
            strips.append(["pending": pending, "stops": stops, "width": hosted.size.width, "height": hosted.size.height,
                           "clicks": clicks, "label": reviewButtonLabel(pending: pending) as Any? ?? NSNull()])
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
            let clicks = hosted.buttons.map { button -> String in
                let before = presses.badge
                button.performClick(nil)
                return presses.badge > before ? "badge" : "none"
            }
            rows.append(["pending": pending, "width": hosted.size.width, "clicks": clicks,
                         "spoken": approvalsWaitingWords(pending)])
        }
        let result: [String: Any] = ["strips": strips, "rows": rows,
                                     "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
