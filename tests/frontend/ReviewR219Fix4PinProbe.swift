import AppKit
import SwiftUI

final class Fix4Document: NSView {
    override var isFlipped: Bool { true }
}

@main struct ReviewR219Fix4PinProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var runs: [[String: Any]] = []
        for cause in ["approval", "search"] {
            let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: 400))
            let document = Fix4Document(frame: NSRect(x: 0, y: 0, width: 480, height: 1600))
            scroll.documentView = document
            let window = NSWindow(contentRect: scroll.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = scroll
            let pin = OpeningPinView(frame: NSRect(x: 0, y: 0, width: 1, height: 1))
            document.addSubview(pin)
            pin.attach()
            func settle() {
                for _ in 0..<8 { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            }
            settle()
            // Schedule a pin, then issue the app's explicit row scroll before it runs.
            document.setFrameSize(NSSize(width: 480, height: 1900))
            NotificationCenter.default.post(name: NSView.frameDidChangeNotification, object: document)
            NotificationCenter.default.post(name: Notification.Name("SubfleetConversationRowScroll"), object: nil)
            scroll.contentView.scroll(to: NSPoint(x: 0, y: 250))
            scroll.reflectScrolledClipView(scroll.contentView)
            settle()
            let afterQueuedPin = scroll.contentView.bounds.minY
            // A later history/layout update must keep the row reveal as well.
            document.setFrameSize(NSSize(width: 480, height: 2200))
            NotificationCenter.default.post(name: NSView.frameDidChangeNotification, object: document)
            settle()
            runs.append(["cause": cause, "after_queued_pin": afterQueuedPin,
                         "after_history": scroll.contentView.bounds.minY])
            pin.detach()
            window.close()
        }
        let data = try JSONSerialization.data(withJSONObject: ["runs": runs, "visible_windows": NSApp.windows.filter(\.isVisible).count], options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
