// Host the conversation window's real views without opening a window or running
// the app: the pinned strip with and without waiting cards, and a sidebar row
// with and without its hand badge (C-27.5); the queue tray above the composer and
// its rows (C-29.7). SwiftUI builds no accessibility tree
// without an assistive client, so a control is seen by the room it takes, and
// each AppKit-backed one (link and borderless buttons; bordered ones too before
// macOS 26) is clicked and named by the action it fired. The same limit hides
// what a control says to VoiceOver (`.accessibilityLabel`) and its tooltip
// (`.help`): on macOS 26.6 the hosting view reports no accessibility children
// and the buttons no label, help or tooltip, so nothing here reads them.
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

/// What the queue tray's closures were asked to do, in order: "withdraw:<id>" and "steer:<id>".
@MainActor final class TrayLog {
    var events: [String] = []
}

/// A view laid out as the window lays out the queue tray above the composer: a
/// fixed width, and the height the view asks for at that width. The hosting
/// view stays in its borderless window, never ordered front, so a click that
/// changes the view's own state (Show N more, Show fewer) is seen on the next layout.
@MainActor final class FixedWidthHost<V: View> {
    let controller: NSHostingController<V>
    let window: NSWindow
    let width: CGFloat

    init(_ view: V, width: CGFloat) {
        controller = NSHostingController(rootView: view)
        self.width = width
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: 24), styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.contentView = controller.view
    }

    /// The size the view asks for at the fixed width. The height proposed is
    /// more than any tray needs, as the window's column proposes a finite height.
    var size: CGSize { controller.sizeThatFits(in: CGSize(width: width, height: 10_000)) }

    /// Lay the view out at its size and collect what AppKit draws of it: the
    /// buttons in the order found and each one's top (down from the top of the
    /// view), the progress indicators, and each scroll view's visible and
    /// document heights. The run loop turns first, so a state
    /// change from an earlier click reaches the view.
    func layout() -> (size: CGSize, buttons: [NSButton], tops: [CGFloat], spinners: Int,
                      scrolls: [[String: CGFloat]]) {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        let size = self.size
        let height = max(size.height, 24)
        window.setContentSize(NSSize(width: width, height: height))
        controller.view.frame = NSRect(x: 0, y: 0, width: width, height: height)
        controller.view.layoutSubtreeIfNeeded()
        func all(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(all) }
        let views = all(controller.view)
        let scrolls = views.compactMap { $0 as? NSScrollView }.map { scroll in
            ["visible": scroll.contentView.bounds.height, "document": scroll.documentView?.frame.height ?? 0]
        }
        let buttons = views.compactMap { $0 as? NSButton }
        let root = controller.view
        let tops = buttons.map { button -> CGFloat in
            let rect = button.convert(button.bounds, to: root)
            return root.isFlipped ? rect.minY : root.bounds.height - rect.maxY
        }
        return (size, buttons, tops, views.filter { $0 is NSProgressIndicator }.count, scrolls)
    }
}

/// Click each button once, in the order found, and name what each click fired.
@MainActor func clickEach(_ buttons: [NSButton], log: TrayLog) -> [[String]] {
    buttons.map { button in
        let before = log.events.count
        button.performClick(nil)
        return Array(log.events[before...])
    }
}

/// A person's message the daemon holds, built with the memberwise initializer.
func queued(_ id: String, _ preview: String, status: String? = nil, canSteer: Bool = false) -> QueuedMessage {
    QueuedMessage(id: id, origin: "person", preview: preview, text: preview, attachments: 0, sending: false,
                  status: status, canSteer: canSteer, withdraw: .cancel(messageID: id))
}

/// `count` short queued messages, q01 first.
func numbered(_ count: Int) -> [QueuedMessage] {
    (1...count).map { queued(String(format: "q%02d", $0), "Queued message \($0)") }
}

/// One hosted tray: its rows, and the closures it gets.
struct TrayConfiguration {
    var name: String
    var rows: [QueuedMessage]
    var withdrawing: Set<String> = []
    var steer = false
    var held = false
    var width: CGFloat = 480
    /// Host the tray in a column of this height laid out like the conversation
    /// view's; nil hosts the tray alone.
    var column: CGFloat? = nil
    /// Rows that replace `rows` after the first pass, as the store's next
    /// layout would after withdrawals; the tray keeps its own state.
    var then: [QueuedMessage]? = nil
}

/// The tray alone, or in a column laid out like `ConversationView`'s lower part:
/// the scrolling timeline, a divider, the tray with its padding, and room for the
/// composer. The column is a stand-in: the real view needs a live model.
struct HostedTray: View {
    let tray: QueueTray
    let column: CGFloat?

    var body: some View {
        if let column {
            VStack(spacing: 0) {
                ScrollView { Color.clear.frame(height: 2_000) }
                Divider()
                tray.padding(.horizontal, 14).padding(.top, 6)
                Color.clear.frame(height: 90)
            }
            .frame(height: column)
        } else {
            tray
        }
    }
}

/// The queue tray hosted in each configuration the tests read (C-29.7). Each
/// configuration is laid out three times; after each layout every AppKit button
/// is clicked once, so a Show N more click shows expanded on the next pass and
/// a Show fewer click collapsed on the one after. A configuration with `then`
/// gets those rows before its second pass.
@MainActor func trayConfigurations(windows: inout [NSWindow]) -> [[String: Any]] {
    let note = QueuedMessage(id: "note", origin: "unblock-note",
                             preview: "Note to the next turn: the stopped turn is left, not resumed", text: nil,
                             attachments: 0, sending: false, status: nil, withdraw: .none)
    let sending = QueuedMessage(id: "q2", origin: "person", preview: "And the tests too", text: "And the tests too",
                                attachments: 0, sending: true, status: "Sending", withdraw: .withdraw(messageID: "q2"))
    let first = queued("q1", "Fix the failing test"), second = queued("q2", "And the tests too")
    var head = first
    head.canSteer = true
    var configurations = [
        TrayConfiguration(name: "one", rows: [first]),
        TrayConfiguration(name: "long", rows: [queued("q1", String(repeating: "abcdefghi ", count: 500))]),
        TrayConfiguration(name: "note", rows: [note, first, sending]),
        TrayConfiguration(name: "busy", rows: [first, second], withdrawing: ["q1"]),
        TrayConfiguration(name: "busy-only", rows: [first], withdrawing: ["q1"]),
        // The head row the daemon offers to steer, while its Withdraw is under way.
        TrayConfiguration(name: "steer-busy", rows: [head, second], withdrawing: ["q1"], steer: true),
        TrayConfiguration(name: "twelve", rows: numbered(12)),
        TrayConfiguration(name: "forty", rows: numbered(40)),
        TrayConfiguration(name: "twelve-held-narrow", rows: numbered(12), held: true, width: 300),
        TrayConfiguration(name: "steer", rows: [head, second], steer: true),
        TrayConfiguration(name: "steer-no-closure", rows: [head, second]),
        TrayConfiguration(name: "steer-not-offered", rows: [first, second], steer: true),
        TrayConfiguration(name: "twelve-then-four", rows: numbered(12), then: numbered(4)),
        TrayConfiguration(name: "column-5", rows: numbered(5), column: 700),
        TrayConfiguration(name: "column-12", rows: numbered(12), column: 700),
    ]
    // Thirteen hides ten: a count one short of it ("Show 9 more") is a digit narrower.
    configurations += (Array(1...9) + [13]).map { TrayConfiguration(name: "count-\($0)", rows: numbered($0)) }
    var out: [[String: Any]] = []
    for configuration in configurations {
        let log = TrayLog()
        let title = queueTrayTitle(configuration.rows, live: !configuration.held, held: configuration.held) ?? ""
        func hostedTray(_ rows: [QueuedMessage]) -> HostedTray {
            HostedTray(tray: QueueTray(title: title, rows: rows, withdrawing: configuration.withdrawing,
                                       withdraw: { log.events.append("withdraw:" + $0.id) },
                                       steer: configuration.steer ? { log.events.append("steer:" + $0.id) } : nil),
                       column: configuration.column)
        }
        let hosted = FixedWidthHost(hostedTray(configuration.rows), width: configuration.width)
        windows.append(hosted.window)
        var passes: [[String: Any]] = []
        for pass in 0..<3 {
            if pass == 1, let then = configuration.then { hosted.controller.rootView = hostedTray(then) }
            let shown = hosted.layout()
            passes.append(["width": shown.size.width, "height": shown.size.height, "buttons": shown.buttons.count,
                           "widths": shown.buttons.map(\.frame.width), "tops": shown.tops,
                           "spinners": shown.spinners, "scrolls": shown.scrolls,
                           "clicks": clickEach(shown.buttons, log: log)])
        }
        let visible = queueTrayVisible(count: configuration.rows.count, expanded: false)
        out.append(["name": configuration.name, "rows": configuration.rows.map(\.id), "title": title,
                    "withdrawing": configuration.withdrawing.sorted(),
                    "width": configuration.width, "shown": visible.shown, "hidden": visible.hidden,
                    "passes": passes, "visible_windows": NSApp.windows.filter(\.isVisible).count])
    }
    return out
}

/// How wide a link button in the tray's caption font is for each label the
/// tray draws. The drawn label is not readable from AppKit (the button's title
/// is empty and there is no accessibility tree), so a tray button is named by
/// matching its width to these. For each count of hidden rows a tray has, the
/// Show N more labels one short and one over are measured too, so a test can
/// tell where a wrong count would draw a different width.
@MainActor func labelWidths(hidden: [Int], windows: inout [NSWindow]) -> [String: CGFloat] {
    var out: [String: CGFloat] = [:]
    let counts = Set(hidden.flatMap { [$0 - 1, $0, $0 + 1] }).sorted()
    for label in ["Withdraw", "Steer", "Show fewer"] + counts.map({ "Show \($0) more" }) {
        let hosted = FixedWidthHost(Button(label) {}.buttonStyle(.link).font(.caption), width: 480)
        windows.append(hosted.window)
        out[label] = hosted.layout().buttons.first?.frame.width ?? 0
    }
    return out
}

/// How many progress indicators AppKit draws for the busy row's
/// `ProgressView().controlSize(.mini)` hosted alone, laid out as a tray is.
@MainActor func spinnerReference(windows: inout [NSWindow]) -> Int {
    let hosted = FixedWidthHost(ProgressView().controlSize(.mini), width: 480)
    windows.append(hosted.window)
    return hosted.layout().spinners
}

/// One tray row's height at the tray's width for each preview the tests compare (C-29.7).
@MainActor func rowHeights(windows: inout [NSWindow]) -> [String: Any] {
    let width: CGFloat = 480
    func size(_ row: QueuedMessage) -> CGSize {
        let hosted = FixedWidthHost(QueueTrayRow(row: row, withdraw: {}), width: width)
        windows.append(hosted.window)
        return hosted.size
    }
    func height(_ row: QueuedMessage) -> CGFloat { size(row).height }
    func words(_ count: Int) -> String { (1...count).map { "word\($0)" }.joined(separator: " ") }
    let oneLine = height(queued("r", words(1)))
    // The fewest words that wrap at this width: that row's text is exactly two lines long.
    var low = 1, high = 400
    while low < high {
        let middle = (low + high) / 2
        if height(queued("r", words(middle))) > oneLine { high = middle } else { low = middle + 1 }
    }
    let long = String(repeating: "abcdefghi ", count: 500)
    let lines = (1...40).map { "line \($0)" }.joined(separator: "\n")
    return [
        "width": width,
        "one_line": oneLine,
        "two_line_words": low,
        "one_word_short_of_two_lines": height(queued("r", words(low - 1))),
        "two_line": height(queued("r", words(low))),
        "long_raw_length": long.count,
        "long_raw": height(queued("r", long)),
        "long_raw_width": size(queued("r", long)).width,
        "long_preview_length": queuePreview(long, attachments: 0).count,
        "long_preview": height(queued("r", queuePreview(long, attachments: 0))),
        "many_lines": height(queued("r", lines)),
        "status": height(queued("r", words(1), status: "Deferred: provider busy")),
        "status_two_line": height(queued("r", words(low), status: "Deferred: provider busy")),
        "long_status": height(queued("r", long, status: "Deferred: provider busy")),
    ]
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
            // Which AppKit button does what: macOS 15 backs a bordered button with
            // one, macOS 26 draws it in SwiftUI, so Review is clicked only on the first.
            let clicks = hosted.buttons.map { button -> String in
                let before = (presses.review, presses.stop)
                button.performClick(nil)
                return presses.review > before.0 ? "review" : presses.stop > before.1 ? "stop" : "none"
            }
            strips.append(["pending": pending, "width": hosted.size.width, "height": hosted.size.height,
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
        let trays = trayConfigurations(windows: &windows)
        let hidden = trays.compactMap { $0["hidden"] as? Int }.filter { $0 > 0 }
        let result: [String: Any] = ["strips": strips, "rows": rows, "trays": trays,
                                     "tray_rows": rowHeights(windows: &windows),
                                     "label_widths": labelWidths(hidden: hidden, windows: &windows),
                                     "spinner_reference": spinnerReference(windows: &windows),
                                     "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
