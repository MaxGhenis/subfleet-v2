import AppKit
import SwiftUI

final class Fix6Defaults: UserDefaults, @unchecked Sendable {
    override func string(forKey key: String) -> String? { nil }
    override func set(_ value: Any?, forKey key: String) {}
    override func removeObject(forKey key: String) {}
}

final class Fix6History: DaemonCalling {
    private let lock = NSLock()
    private var cancelled: String?
    var cancelledID: String? {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        if op.name == "message.cancel" {
            let value = try JSONValue.from(args)
            lock.lock()
            cancelled = value["message_id"]?.string
            lock.unlock()
            // Record which actual row was clicked, without withdrawing it.
            throw DaemonClientError.unavailable("probe records row identity only")
        }
        guard op.name == "conversation.history" else { throw DaemonClientError.unavailable("probe has no daemon") }
        return try JSONValue.from(history(80..<100, next: 80)).decode(R.self)
    }
}

func history(_ range: Range<Int>, next: Int?) -> HistoryPage {
    let items = range.map { index in
        HistoryItem(role: "assistant", kind: "text", text: "history \(index) " + String(repeating: "words ", count: 50),
                    ts: "2026-09-28T09:00:00Z", cursor: index)
    }
    return HistoryPage(items: items, next_before: next)
}

@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

@MainActor func settle(_ view: NSView, seconds: Double = 0.5) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        view.layoutSubtreeIfNeeded()
    }
}

@MainActor func mount<V: View>(_ view: V) -> (NSHostingView<AnyView>, NSWindow) {
    let host = NSHostingView(rootView: AnyView(view))
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 700),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    host.frame = window.contentView!.bounds
    settle(host)
    return (host, window)
}

func conversation(_ cid: String, held: Bool = false) -> Conversation {
    Conversation(conversation_id: cid, provider: "claude", workspace: "/tmp/project", workspace_kind: "in-place",
        allow_main: false, settings: ConversationSettings(model: "claude-opus-5-5"), origin: "person",
        blocked_by: held ? "unfinished-turn" : nil, created_at: "2026-09-28T09:00:00Z",
        updated_at: "2026-09-28T09:00:00Z", pending_approvals: 0, active: false)
}

@main struct ReviewR219Fix6ViewProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        var scrolls: [[String: Any]] = []
        for caughtUp in [false, true] {
            let cid = caughtUp ? "caught-up" : "catching-up"
            let cv = conversation(cid)
            var state = ConversationStoreState()
            let messages = (0..<50).map { index in
                Receipt(message_id: "message\(index)", conversation_id: cid, seq: index + 1, origin: "person",
                        state: "complete", text: "message \(index) " + String(repeating: "words ", count: 50))
            }
            state.apply(open: ConversationOpenResult(conversation: cv, messages: messages, events_cursor: 0,
                        pending_approvals: []))
            state.focus(cid)
            if caughtUp { state.apply(events: EventsPage(events: [], next: 0, reset: false), conversationID: cid) }
            let model = UIModel(paths: .rooted(at: root.appendingPathComponent(cid)), client: Fix6History(),
                                defaults: Fix6Defaults(), state: state)
            let (host, window) = mount(ConversationView(model: model, conversation: cv))
            let scroll = descendants(host).compactMap { $0 as? NSScrollView }.first!
            // The app's own reveal: no person gesture accompanies this notification.
            ConversationOpeningPin.revealRow(in: cid)
            scroll.contentView.scroll(to: NSPoint(x: 0, y: 250))
            scroll.reflectScrolledClipView(scroll.contentView)
            settle(host)
            let before = scroll.contentView.bounds.minY
            await model.loadHistory(cid)
            settle(host)
            scrolls.append(["caught_up": model.state.timelines[cid]!.caughtUp, "before": before,
                            "after": scroll.contentView.bounds.minY])
            window.close()
        }

        let cv = conversation("search", held: true)
        var state = ConversationStoreState()
        state.upsert(cv)
        for index in 0..<6 {
            let words = index == 5 ? "unique sixth needle" : "queued message \(index)"
            let raw = """
            {"conversation_id":"search","message_id":"queued\(index)","seq":\(index + 1),
             "origin":"person","state":"queued","text":"\(words)"}
            """
            state.apply(receipt: try JSONDecoder().decode(Receipt.self, from: Data(raw.utf8)))
        }
        state.focus("search")
        let client = Fix6History()
        let model = UIModel(paths: .rooted(at: root.appendingPathComponent("search")), client: client,
                            defaults: Fix6Defaults(), state: state)
        let palette = SearchPaletteModel()
        palette.model = model
        var trayLayouts: [QueueTrayRowLayout] = []
        let (host, window) = mount(ConversationView(model: model, conversation: cv)
            .environment(\.searchPalette, palette)
            .onPreferenceChange(QueueTrayRowLayoutKey.self) { trayLayouts += $0 })
        func identifiedWithdraws() -> [NSButton] {
            descendants(host).compactMap { $0 as? NSButton }.filter {
                $0.accessibilityIdentifier().hasPrefix("queue-tray-withdraw-")
            }
        }
        // Geometry is a legacy fallback only; identifiers need no widths or positions.
        let referenceWindow: NSWindow?
        let width: CGFloat
        let rowX: CGFloat?
        if identifiedWithdraws().isEmpty {
            let (reference, window) = mount(Button("Withdraw") {}.buttonStyle(.link).readingFont(.caption))
            referenceWindow = window
            width = reference.fittingSize.width
            rowX = descendants(host).compactMap { $0 as? NSButton }
                .filter { abs($0.frame.width - width) < 1 }
                .map { $0.convert($0.bounds, to: host).minX }.max()
        } else {
            referenceWindow = nil
            width = 0
            rowX = nil
        }
        func withdraws() -> [NSButton] {
            let identified = identifiedWithdraws()
            if !identified.isEmpty { return identified }
            guard let rowX else { return [] }
            return descendants(host).compactMap { $0 as? NSButton }.filter {
                abs($0.frame.width - width) < 1 && abs($0.convert($0.bounds, to: host).minX - rowX) < 0.5
            }
        }
        let before = withdraws().count
        let beforeIdentifiers = identifiedWithdraws().count
        let match = SearchIndex(model.state.searchCandidates()).search("needle")!.results.first!
        palette.activate(match)
        let target = palette.reveal?.itemID ?? ""
        let revealSet = palette.reveal != nil
        let started = Date()
        let deadlineSeconds = 10.0
        let deadline = started.addingTimeInterval(deadlineSeconds)
        func laidOutCount() -> Int { trayLayouts.last?.rowIDs.count ?? 0 }
        while (laidOutCount() < 6 || withdraws().count < 6) && Date() < deadline {
            settle(host, seconds: 0.02)
        }
        let buttons = withdraws()
        let expandedAfter = Date().timeIntervalSince(started)
        // Expansion and the target's scroll have distinct conditions.
        func targetButton() -> NSButton? {
            identifiedWithdraws().first { $0.accessibilityIdentifier() == "queue-tray-withdraw-queued5" }
        }
        while (targetButton()?.visibleRect.isEmpty != false || palette.reveal != nil) && Date() < deadline {
            settle(host, seconds: 0.02)
        }
        let last = targetButton() ?? buttons.sorted {
            let a = $0.convert($0.bounds, to: host).minY, b = $1.convert($1.bounds, to: host).minY
            return host.isFlipped ? a < b : a > b
        }.last
        let targetVisible = last.map { !$0.visibleRect.isEmpty } ?? false
        let traySawReveal = trayLayouts.contains { $0.reveal?.itemID == target }
        let laidOutRows = laidOutCount()
        let identifierCount = identifiedWithdraws().count
        let revealConsumed = palette.reveal == nil
        let elapsed = Date().timeIntervalSince(started)
        let failureCause: String
        if !revealSet { failureCause = "palette did not set reveal" }
        else if !traySawReveal { failureCause = "tray did not observe reveal" }
        else if laidOutRows < 6 { failureCause = "tray laid out fewer than six rows" }
        else if identifierCount != 6 { failureCause = "Withdraw identifiers missing from laid-out rows" }
        else if !revealConsumed { failureCause = "row scroll did not consume reveal before deadline" }
        else if !targetVisible { failureCause = "matched row remains outside the visible rect" }
        else { failureCause = "none" }
        last?.performClick(nil)
        for _ in 0..<100 {
            if client.cancelledID != nil { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        let search: [String: Any] = ["target": target, "before": before, "after": buttons.count,
                                    "revealed_id": client.cancelledID as Any? ?? NSNull(),
                                    "reference_width": width,
                                    "reveal_set": revealSet,
                                    "tray_saw_reveal": traySawReveal,
                                    "laid_out_rows": laidOutRows,
                                    "reveal_consumed": revealConsumed,
                                    "timed_out": failureCause != "none" && elapsed >= deadlineSeconds,
                                    "failure_cause": failureCause,
                                    "deadline_seconds": deadlineSeconds,
                                    "elapsed_seconds": elapsed,
                                    "expanded_after_seconds": expandedAfter,
                                    "model_tray_rows": model.state.layout(conversationID: "search")!.tray.count,
                                    "before_identifiers": beforeIdentifiers,
                                    "after_identifiers": identifierCount,
                                    "finder": identifierCount == 0 ? "geometry-fallback" : "identifier",
                                    "buttons": descendants(host).compactMap { $0 as? NSButton }.map {
                                        ["id": $0.accessibilityIdentifier(), "title": $0.title, "width": $0.frame.width,
                                         "x": $0.convert($0.bounds, to: host).minX] as [String: Any]
                                    },
                                    "target_visible": targetVisible]
        referenceWindow?.close()
        window.close()
        let output: [String: Any] = ["scrolls": scrolls, "search": search,
                                   "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), as: UTF8.self))
    }
}
