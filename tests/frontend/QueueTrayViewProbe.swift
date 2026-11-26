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

/// What the queue tray's closures were asked to do, in order: "withdraw:<id>" and
/// "steer:<id> into <turn>", the turn being the one the row's Send now names.
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
func queued(_ id: String, _ preview: String, status: String? = nil, steerInto: String? = nil) -> QueuedMessage {
    QueuedMessage(id: id, origin: "person", preview: preview, text: preview, attachments: 0, sending: false,
                  status: status, steerInto: steerInto, withdraw: .cancel(messageID: id))
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
    var notice = false
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
    head.steerInto = "host-a"
    var configurations = [
        TrayConfiguration(name: "one", rows: [first]),
        TrayConfiguration(name: "long", rows: [queued("q1", String(repeating: "abcdefghi ", count: 500))]),
        TrayConfiguration(name: "note", rows: [note, first, sending]),
        TrayConfiguration(name: "busy", rows: [first, second], withdrawing: ["q1"]),
        TrayConfiguration(name: "busy-only", rows: [first], withdrawing: ["q1"]),
        // The head row the daemon offers to steer, while its Withdraw is under way.
        TrayConfiguration(name: "notice", rows: [head, second], notice: true),
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
                                       steer: configuration.steer ? { log.events.append("steer:\($0.id) into \($1)") } : nil,
                                       notice: configuration.notice ? { id in
                                           guard id == "q1" else { return nil }
                                           return AnyView(VStack(alignment: .leading) {
                                               Text("Send stopped: permission needed").readingFont(.caption)
                                               Button("Try again") { log.events.append("retry:" + id) }
                                                   .buttonStyle(.link).readingFont(.caption)
                                           })
                                       } : nil),
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
    for label in ["Withdraw", "Send now", "Show fewer"] + counts.map({ "Show \($0) more" }) {
        let hosted = FixedWidthHost(Button(label) {}.buttonStyle(.link).readingFont(.caption), width: 480)
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

/// Writes nothing: the model's remembered picks stay out of the probe's own defaults.
final class SendNowDefaults: UserDefaults, @unchecked Sendable {
    override func string(forKey key: String) -> String? { nil }
    override func set(_ value: Any?, forKey key: String) {}
    override func removeObject(forKey key: String) {}
}

/// Turn the run loop and yield the main actor for `seconds`: the model's tasks
/// (its steer, its pump) and SwiftUI's updates run meanwhile.
@MainActor func settleMain(_ seconds: Double) async {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        try? await Task.sleep(nanoseconds: 5_000_000)
    }
}

/// The steer of `messageID` as the app's outbox journal holds it, read from the file
/// without opening the journal (an `Outbox` may repair what it loads).
func journaledSteer(_ url: URL, _ messageID: String) -> OutboxSteer? {
    guard let data = try? Data(contentsOf: url),
          let journal = try? JSONDecoder().decode(OutboxJournal.self, from: data) else { return nil }
    return journal.steers.first { $0.messageID == messageID }
}

func steerRow(_ steer: OutboxSteer?) -> Any {
    guard let steer else { return NSNull() }
    return ["state": steer.state.rawValue, "attempts": steer.attempts, "into": steer.into as Any? ?? NSNull(),
            "failure": steer.failure.map { ["code": $0.code as Any? ?? NSNull(), "reason": $0.reason as Any? ?? NSNull(),
                                            "retryable": $0.retryable] } as Any? ?? NSNull(),
            "receipt_state": steer.receipt?.state as Any? ?? NSNull()]
}

/// `send-now <spec.json>` (#265 review r1, P2): the queue tray's "Send now" through the
/// real `UIModel`, against the daemon socket the spec names. The model opens the
/// conversation (`UIModel.open`); the tray is hosted from the model's layout and wired as
/// the conversation view wires it, and its Send now button is kept. The model then reads
/// the conversation again (a second `UIModel.open`), and the kept button is clicked: with
/// `offer_after_reopen`, the tray is hosted after that second read instead (the control).
/// The model's pump runs until the steer is answered for good, and the result is what the
/// person saw at each point, the journaled steer, and the message as the model shows it.
@MainActor func sendNow(_ spec: JSONValue) async throws -> [String: Any] {
    let cid = spec["conversation_id"]?.string ?? "", queued = spec["queued"]?.string ?? ""
    var initial = ConversationStoreState()
    initial.availability = .ready(try spec["capabilities"]!.decode(Capabilities.self))
    let root = URL(fileURLWithPath: spec["app"]?.string ?? "")
    let paths = AppPaths.rooted(at: root)
    let client = DaemonClient(transport: UnixSocketTransport(path: spec["socket"]?.string ?? ""))
    let model = UIModel(paths: paths, client: client, defaults: SendNowDefaults(), state: initial)
    var windows: [NSWindow] = []
    let sendNowWidth = labelWidths(hidden: [], windows: &windows)["Send now"] ?? 0
    var out: [String: Any] = [:]

    /// Host the tray from the model's layout now, as the conversation view builds it, and
    /// keep its Send now button: the one button that is not a row's Withdraw (each
    /// Withdraw carries its message id), as wide as a link labelled "Send now". The tray
    /// holds only the queued message here.
    func offer() -> NSButton? {
        let layout = model.state.layout(conversationID: cid)
        var shown: [String: Any] = ["live": model.state.timelines[cid]?.liveMessageID as Any? ?? NSNull(),
                                    "rows": layout?.tray.map(\.id) ?? [],
                                    "steer_into": layout?.tray.map { $0.steerInto as Any? ?? NSNull() } ?? []]
        guard let layout, let title = layout.trayTitle else { out["offered"] = shown; return nil }
        let tray = QueueTray(model: model, conversationID: cid, title: title, rows: layout.tray)
        let hosted = FixedWidthHost(tray, width: 480)
        windows.append(hosted.window)
        let buttons = hosted.layout().buttons
        let others = buttons.filter { !($0.identifier?.rawValue ?? "").hasPrefix("queue-tray-withdraw-") }
        shown["buttons"] = buttons.count
        shown["send_now"] = others.count == 1 && others[0].frame.width == sendNowWidth
        out["offered"] = shown
        return others.count == 1 ? others[0] : nil
    }

    await model.open(.conversation(cid))
    var kept = spec["offer_after_reopen"]?.bool == true ? nil : offer()
    await model.open(.conversation(cid))
    if spec["offer_after_reopen"]?.bool == true { kept = offer() }
    out["live_at_click"] = model.state.timelines[cid]?.liveMessageID as Any? ?? NSNull()
    out["offered_at_click"] = model.state.layout(conversationID: cid)?.tray.first { $0.id == queued }?.canSteer ?? false
    guard let button = kept else {
        out["clicked"] = false
        return out
    }
    button.performClick(nil)
    out["clicked"] = true
    // The click journals the steer and pumps once; the app's pump timer would go on every
    // 2 s, so pump every 0.5 s until the steer is answered for good and the model has it.
    let deadline = Date().addingTimeInterval(60)
    var pumped = Date()
    while Date() < deadline {
        await settleMain(0.05)
        let steer = journaledSteer(paths.outboxURL, queued)
        if let steer, !steer.isOpen, model.state.timelines[cid]?.turn(queued)?.steerRequested == false { break }
        if Date().timeIntervalSince(pumped) >= 0.5 { model.pump(); pumped = Date() }
    }
    await settleMain(0.2)
    out["steer"] = steerRow(journaledSteer(paths.outboxURL, queued))
    let turn = model.state.timelines[cid]?.turn(queued)
    out["queued"] = ["state": turn?.state as Any? ?? NSNull(), "status": turn?.statusText as Any? ?? NSNull(),
                     "in_tray": model.state.layout(conversationID: cid)?.tray.contains { $0.id == queued } ?? false]
    out["problem"] = model.problem as Any? ?? NSNull()
    out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
    withExtendedLifetime(windows) {}
    return out
}

@main
struct QueueTrayViewProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        if CommandLine.arguments.count > 2, CommandLine.arguments[1] == "send-now" {
            // The model's work is asynchronous: run it as a main-actor task, turning the run
            // loop (which serves the main actor) until it is done. The layouts below stay as
            // they were, synchronous.
            let spec = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])))
            var finished: Result<[String: Any], Error>?
            Task { @MainActor in
                do { finished = .success(try await sendNow(spec)) } catch { finished = .failure(error) }
            }
            while finished == nil { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            let result = try finished!.get()
            print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
            return
        }
        var windows: [NSWindow] = []
        let trays = trayConfigurations(windows: &windows)
        let hidden = trays.compactMap { $0["hidden"] as? Int }.filter { $0 > 0 }
        let result: [String: Any] = ["trays": trays, "tray_rows": rowHeights(windows: &windows),
            "label_widths": labelWidths(hidden: hidden, windows: &windows),
            "spinner_reference": spinnerReference(windows: &windows),
            "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
        withExtendedLifetime(windows) {}
    }
}
