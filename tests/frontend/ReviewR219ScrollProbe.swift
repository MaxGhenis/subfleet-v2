// Review r1 of #219: where a conversation-like scroll view opens, offscreen.
// Regression probe. Uses the app's real `initialConversationScrollAnchor`.
import AppKit
import SwiftUI

final class Feed: ObservableObject {
    @Published var rows: Int
    @Published var jumps = 0
    init(rows: Int) { self.rows = rows }
}

struct OpeningHarness: View {
    @ObservedObject var feed: Feed
    let anchored: Bool
    let jumpOnAppear: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(0..<feed.rows, id: \.self) { index in
                        Text("row \(index) " + String(repeating: "word ", count: (index % 7) * 9))
                            .frame(maxWidth: .infinity, alignment: .leading).id("row\(index)")
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(12)
                .background {
                    if anchored || jumpOnAppear { ConversationOpeningPin() }
                }
            }
            .modifier(MaybeAnchor(anchored: anchored))
            .onChange(of: feed.jumps) { _, _ in jump(proxy) }
            .onAppear { if jumpOnAppear { jump(proxy) } }
        }
    }

    private func jump(_ proxy: ScrollViewProxy) {
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { proxy.scrollTo("bottom", anchor: .bottom) }
    }
}

struct MaybeAnchor: ViewModifier {
    let anchored: Bool
    func body(content: Content) -> some View {
        if anchored { content.initialConversationScrollAnchor() } else { content }
    }
}

func scrollViews(_ view: NSView) -> [NSScrollView] {
    (view as? NSScrollView).map { [$0] } ?? [] + view.subviews.flatMap(scrollViews)
}

@MainActor func sample(_ host: NSView) -> [String: Double] {
    guard let scroll = scrollViews(host).first, let document = scroll.documentView else { return ["found": 0] }
    let visible = scroll.contentView.bounds
    let height = document.frame.height
    // Distance from the end: 0 means the bottom of the content is in view.
    let fromEnd = document.isFlipped ? height - visible.maxY : visible.minY
    return ["found": 1, "offset": Double(visible.minY), "content": Double(height), "viewport": Double(visible.height),
            "from_end": Double(fromEnd)]
}

@main
struct ReviewR219ScrollProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var out: [[String: Any]] = []
        var windows: [NSWindow] = []
        for (name, anchored, jumpOnAppear) in [("anchor-15", true, false), ("emulated-14", false, true),
                                               ("neither", false, false)] {
            let feed = Feed(rows: 120)
            let host = NSHostingView(rootView: OpeningHarness(feed: feed, anchored: anchored, jumpOnAppear: jumpOnAppear))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 400), styleMask: [.borderless],
                                  backing: .buffered, defer: false)
            window.contentView = host
            host.frame = NSRect(x: 0, y: 0, width: 480, height: 400)
            windows.append(window)
            var samples: [[String: Double]] = []
            host.layoutSubtreeIfNeeded()
            samples.append(sample(host))
            for _ in 0..<30 {
                RunLoop.main.run(until: Date().addingTimeInterval(0.01))
                host.layoutSubtreeIfNeeded()
                samples.append(sample(host))
            }
            // A first events page may arrive before any follow-key change. No catch-up jump.
            feed.rows += 30
            var latePage: [[String: Double]] = []
            for _ in 0..<30 {
                RunLoop.main.run(until: Date().addingTimeInterval(0.01))
                host.layoutSubtreeIfNeeded()
                latePage.append(sample(host))
            }
            // Rows arrive while the log is read (settling): each change jumps, without animation.
            var settling: [[String: Double]] = []
            for _ in 0..<5 {
                feed.rows += 15
                feed.jumps += 1
                for _ in 0..<6 {
                    RunLoop.main.run(until: Date().addingTimeInterval(0.01))
                    host.layoutSubtreeIfNeeded()
                    settling.append(sample(host))
                }
            }
            // Emulate the AppKit notification emitted by the person's first
            // scroll. Later row growth must leave that reading position alone.
            var afterPerson: [[String: Double]] = []
            if let scroll = scrollViews(host).first, let document = scroll.documentView {
                NotificationCenter.default.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
                let clip = scroll.contentView
                let y = document.isFlipped ? max(0, document.frame.height - clip.bounds.height - 180) : 180
                clip.scroll(to: NSPoint(x: 0, y: y))
                scroll.reflectScrolledClipView(clip)
                feed.rows += 30
                for _ in 0..<30 {
                    RunLoop.main.run(until: Date().addingTimeInterval(0.01))
                    host.layoutSubtreeIfNeeded()
                    afterPerson.append(sample(host))
                }
            }
            out.append(["name": name, "opening": samples, "late_page": latePage, "settling": settling,
                        "after_person": afterPerson])
        }
        let result: [String: Any] = ["runs": out, "visible_windows": NSApp.windows.filter(\.isVisible).count,
                                     "os": ProcessInfo.processInfo.operatingSystemVersionString]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
