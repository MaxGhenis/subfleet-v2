import AppKit
import SwiftUI

final class Fix6Defaults: UserDefaults, @unchecked Sendable {
    override func string(forKey key: String) -> String? { nil }
    override func set(_ value: Any?, forKey key: String) {}
    override func removeObject(forKey key: String) {}
}

final class Fix6History: DaemonCalling {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
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

@MainActor func mount<V: View>(_ view: V) -> (NSHostingView<V>, NSWindow) {
    let host = NSHostingView(rootView: view)
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
        let model = UIModel(paths: .rooted(at: root.appendingPathComponent("search")), client: Fix6History(),
                            defaults: Fix6Defaults(), state: state)
        let palette = SearchPaletteModel()
        palette.model = model
        let (host, window) = mount(ConversationView(model: model, conversation: cv).environment(\.searchPalette, palette))
        // Compare with the real Withdraw control's width: macOS 26 hides its AX label offscreen.
        let (reference, referenceWindow) = mount(Button("Withdraw") {}.buttonStyle(.link).readingFont(.caption))
        let width = reference.fittingSize.width
        // Tray Withdraw controls form the rightmost column; two unrelated
        // conversation controls happen to have the same width on macOS 26.
        let rowX = descendants(host).compactMap { $0 as? NSButton }
            .filter { abs($0.frame.width - width) < 1 }
            .map { $0.convert($0.bounds, to: host).minX }.max()!
        func withdraws() -> [NSButton] {
            descendants(host).compactMap { $0 as? NSButton }.filter {
                abs($0.frame.width - width) < 1 && abs($0.convert($0.bounds, to: host).minX - rowX) < 0.5
            }
        }
        let before = withdraws().count
        let match = SearchIndex(model.state.searchCandidates()).search("needle")!.results.first!
        palette.activate(match)
        let target = palette.reveal?.itemID ?? ""
        settle(host, seconds: 1.5)
        let buttons = withdraws()
        let last = buttons.sorted {
            let a = $0.convert($0.bounds, to: host).minY, b = $1.convert($1.bounds, to: host).minY
            return host.isFlipped ? a < b : a > b
        }.last
        let search: [String: Any] = ["target": target, "before": before, "after": buttons.count,
                                    "reference_width": width,
                                    "buttons": descendants(host).compactMap { $0 as? NSButton }.map {
                                        ["title": $0.title, "width": $0.frame.width,
                                         "x": $0.convert($0.bounds, to: host).minX] as [String: Any]
                                    },
                                    "target_visible": last.map { !$0.visibleRect.isEmpty } ?? false]
        referenceWindow.close()
        window.close()
        let output: [String: Any] = ["scrolls": scrolls, "search": search,
                                   "visible_windows": NSApp.windows.filter(\.isVisible).count]
        print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), as: UTF8.self))
    }
}
