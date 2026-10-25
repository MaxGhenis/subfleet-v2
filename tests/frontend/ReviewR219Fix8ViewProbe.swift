import AppKit
import SwiftUI
import Combine

// Position/count probes need a live view hierarchy and synchronous scroll
// transactions. This module-local overload controls the app's unqualified
// withAnimation calls only when this probe is compiled alongside its sources.
enum ProbePresentation {
    static var animationCalls = 0
    static var mountedWindows = 0
}
func withAnimation<Value>(_ animation: Animation? = .default, _ body: () -> Value) -> Value {
    ProbePresentation.animationCalls += 1
    var transaction = Transaction(animation: nil)
    transaction.disablesAnimations = true
    return SwiftUI.withTransaction(transaction, body)
}

func withAnimation<Value>(_ animation: Animation?, completionCriteria: AnimationCompletionCriteria,
                          _ body: () -> Value, completion: @escaping () -> Void) -> Value {
    ProbePresentation.animationCalls += 1
    var transaction = Transaction(animation: nil)
    transaction.disablesAnimations = true
    return SwiftUI.withTransaction(transaction) {
        SwiftUI.withAnimation(nil, completionCriteria: completionCriteria, body, completion: completion)
    }
}

@MainActor func mountOffscreen(_ window: NSWindow) {
    let screens = NSScreen.screens.map(\.frame)
    let left = (screens.map(\.minX).min() ?? 0) - 10000
    let below = (screens.map(\.minY).min() ?? 0) - 10000
    window.isRestorable = false
    window.setFrameOrigin(NSPoint(x: left, y: below))
    precondition(!screens.contains { $0.intersects(window.frame) })
    window.orderBack(nil)
    window.displayIfNeeded()
    precondition(window.isVisible && !window.isKeyWindow
                 && !screens.contains { $0.intersects(window.frame) })
    ProbePresentation.mountedWindows += 1
}


// Review r4 of #219: the real ConversationView, UIModel.open and its events loop
// against a scripted daemon. Measures whether the timeline keeps following its
// end after the app's own row reveals (approval follow, search), compiled at the
// head and at the pre-fix-6 merge.

final class R4Defaults: UserDefaults, @unchecked Sendable {
    override func string(forKey key: String) -> String? { nil }
    override func set(_ value: Any?, forKey key: String) {}
    override func removeObject(forKey key: String) {}
}

func json(_ any: Any) -> JSONValue { try! JSONValue.parse(try! JSONSerialization.data(withJSONObject: any)) }

func stamp(_ seq: Int) -> String {
    String(format: "2026-10-20T%02d:%02d:%02dZ", (seq / 3600) % 24, (seq / 60) % 60, seq % 60)
}

func event(_ seq: Int, _ mid: String, _ kind: String, _ data: [String: Any]) -> [String: Any] {
    ["seq": seq, "message_id": mid, "kind": kind, "ts": stamp(seq), "data": data]
}

final class R4ViewDaemon: DaemonCalling, @unchecked Sendable {
    private let lock = NSCondition()
    private let immediateDelivery: Bool
    private var log: [[String: Any]]
    private let catchupGate = NSCondition()
    private var catchupReleased = true
    private var waitingForCatchup = false
    var catchupPending: Bool {
        catchupGate.lock(); defer { catchupGate.unlock() }
        return waitingForCatchup
    }
    func pauseCatchup() {
        catchupGate.lock(); catchupReleased = false; catchupGate.unlock()
    }
    func resumeCatchup() {
        catchupGate.lock(); catchupReleased = true; catchupGate.broadcast(); catchupGate.unlock()
    }
    let conversation: [String: Any]
    let receipts: [[String: Any]]
    /// A slow catch-up: each page read with wait_s 0 takes this long and holds at most pageSize events.
    var pageDelay = 0.0
    var pageSize = 200
    // Count real UIModel.open status requests, including the identities in each batch.
    private var requestedIDs: [String] = []
    func takeStatusIDs() -> [String] {
        lock.lock(); defer { lock.unlock() }
        let result = requestedIDs
        requestedIDs = []
        return result
    }

    init(conversation: [String: Any], receipts: [[String: Any]], events: [[String: Any]], immediateDelivery: Bool = false) {
        self.immediateDelivery = immediateDelivery
        self.conversation = conversation
        self.receipts = receipts
        self.log = events
    }

    var lastSeq: Int {
        lock.lock(); defer { lock.unlock() }
        return (log.last?["seq"] as? Int) ?? 0
    }

    func push(_ events: [[String: Any]]) {
        lock.lock(); log += events; lock.broadcast(); lock.unlock()
    }

    private func page(after: Int) -> [[String: Any]] {
        lock.lock(); defer { lock.unlock() }
        return Array(log.filter { ($0["seq"] as! Int) > after }.prefix(pageSize))
    }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let value = try JSONValue.from(args)
        let out: Any
        switch op.name {
        case "capabilities":
            out = ["protocol": 1, "daemon_version": "probe", "conversation_schema": 1,
                   "capabilities": ["conversations.v1", "conversation.status.v1"]]
        case "conversation.open":
            out = ["conversation": conversation, "messages": Array(receipts.suffix(50)), "events_cursor": 0,
                   "pending_approvals": []]
        case "conversation.status":
            out = ["conversation": conversation, "message_ids": receipts.map { $0["message_id"]! }]
        case "message.status":
            let ids = Set(value["message_ids"]?.array?.compactMap(\.string) ?? [])
            lock.lock(); requestedIDs += ids.sorted(); lock.unlock()
            out = ["messages": receipts.filter { ids.contains($0["message_id"] as! String) }]
        case "conversation.events":
            let after = value["after"]?.int ?? 0
            if (value["wait_s"]?.double ?? 0) == 0 {
                catchupGate.lock()
                waitingForCatchup = !catchupReleased
                let deadline = Date().addingTimeInterval(60)
                while !catchupReleased {
                    if !catchupGate.wait(until: deadline) {
                        catchupGate.unlock()
                        throw DaemonClientError.unavailable("probe catch-up barrier was never released")
                    }
                }
                waitingForCatchup = false
                catchupGate.unlock()
                if pageDelay > 0 { Thread.sleep(forTimeInterval: pageDelay) }
            }
            var events = page(after: after)
            if events.isEmpty, (value["wait_s"]?.double ?? 0) > 0 {
                if immediateDelivery {
                    // A real long poll wakes when output arrives. The streaming
                    // scenes must not add an unrelated 30 ms polling delay.
                    lock.lock()
                    if !log.contains(where: { ($0["seq"] as! Int) > after }) {
                        _ = lock.wait(until: Date().addingTimeInterval(15))
                    }
                    lock.unlock()
                } else { Thread.sleep(forTimeInterval: 0.03) }
                events = page(after: after)
            }
            out = ["events": events, "next": (events.last?["seq"] as? Int) ?? after, "reset": false]
        case "conversation.history":
            out = ["items": [], "next_before": NSNull()]
        case "conversation.runs":
            out = ["runs": []]
        default:
            throw DaemonClientError.unavailable("probe has no \(op.name)")
        }
        return try json(out).decode(R.self)
    }
}

@MainActor func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

@MainActor func settle(_ view: NSView, seconds: Double) async {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        view.layoutSubtreeIfNeeded()
        NSApp.updateWindows()
        view.window?.displayIfNeeded()
        try? await Task.sleep(nanoseconds: 5_000_000)
    }
}

@MainActor func timelineScroll(_ host: NSView) -> NSScrollView? {
    descendants(host).compactMap { $0 as? NSScrollView }
        .max { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) }
}

@MainActor func sample(_ host: NSView) -> [String: Double] {
    guard let scroll = timelineScroll(host), let document = scroll.documentView else { return ["found": 0] }
    let visible = scroll.contentView.bounds
    let fromEnd = document.isFlipped ? document.frame.height - visible.maxY : visible.minY
    return ["offset": Double(visible.minY), "content": Double(document.frame.height),
            "viewport": Double(visible.height), "from_end": Double(fromEnd)]
}

@MainActor func trayIdentifiers(_ host: NSView) -> [String] {
    descendants(host).compactMap { $0 as? NSButton }.compactMap { button -> String? in
        let id = button.identifier?.rawValue ?? ""
        return id.hasPrefix("queue-tray-withdraw-") ? id : nil
    }.sorted()
}

@MainActor final class Scene {
    let cid: String
    let daemon: R4ViewDaemon
    let model: UIModel
    let palette = SearchPaletteModel()
    var host: NSHostingView<AnyView>!
    var window: NSWindow!
    var seq: Int
    let live = "live"
    var beforeCatchup: [String: Double] = [:]
    var afterInitialReveal: [String: Double] = [:]
    var heldBeforeReveal = false

    init(_ cid: String, root: URL, prior: Int, priorText: (Int) -> String, tray: [String],
         pageDelay: Double = 0, pageSize: Int = 200, revealOnMount: String? = nil,
         liveText: String = "the live request", pauseCatchup: Bool = false,
         immediateDelivery: Bool = false) async {
        self.cid = cid
        var events: [[String: Any]] = []
        var receipts: [[String: Any]] = []
        var seq = 0
        func next() -> Int { seq += 1; return seq }
        for index in 0..<prior {
            let mid = String(format: "m%06d", index)
            receipts.append(["message_id": mid, "conversation_id": cid, "seq": index + 1, "origin": "person",
                             "state": "complete", "text": priorText(index)])
            events.append(event(next(), mid, "accepted", [:]))
            events.append(event(next(), mid, "text", ["block": "b", "text": (0..<8).map { "reply \(index) line \($0)" }
                .joined(separator: "\n")]))
            events.append(event(next(), mid, "turn.completed", ["state": "complete"]))
        }
        receipts.append(["message_id": live, "conversation_id": cid, "seq": prior + 1, "origin": "person",
                         "state": "running", "text": liveText])
        events.append(event(next(), live, "accepted", [:]))
        events.append(event(next(), live, "text.delta", ["block": "live", "text": "streaming starts\n"]))
        for (index, text) in tray.enumerated() {
            receipts.append(["message_id": "q\(index)", "conversation_id": cid, "seq": prior + 2 + index,
                             "origin": "person", "state": "queued", "text": text])
        }
        self.seq = seq
        let conversation: [String: Any] = ["conversation_id": cid, "provider": "claude", "workspace": "/tmp/project",
            "workspace_kind": "in-place", "allow_main": false, "settings": ["model": "claude-opus-5-5", "fast": false, "permission": "ask", "auto_continue": true],
            "origin": "person", "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z",
            "pending_approvals": 0, "active": true]
        daemon = R4ViewDaemon(conversation: conversation, receipts: receipts, events: events,
                              immediateDelivery: immediateDelivery)
        daemon.pageDelay = pageDelay
        daemon.pageSize = pageSize
        if pauseCatchup { daemon.pauseCatchup() }
        model = UIModel(paths: .rooted(at: root.appendingPathComponent(cid)), client: daemon, defaults: R4Defaults())
        palette.model = model
        await model.open(.conversation(cid))
        guard let shown = model.state.conversation(cid) else {
            FileHandle.standardError.write(Data("open failed: \(model.problem ?? "no problem reported")\n".utf8))
            exit(3)
        }
        host = NSHostingView(rootView: AnyView(ConversationView(model: model, conversation: shown)
            .environment(\.searchPalette, palette).transaction { $0.animation = nil; $0.disablesAnimations = true }))
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 760), styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        host.frame = window.contentView!.bounds
        mountOffscreen(window)
        if pauseCatchup {
            // Establish the invariant's actual precondition, independent of host scheduling:
            // the opening pin has placed the live row/end on screen, and catch-up is pending.
            let deadline = Date().addingTimeInterval(60)
            while (!daemon.catchupPending || (sample(host)["from_end"] ?? 1e9) > 30) && Date() < deadline {
                await settle(host, seconds: 0.02)
            }
            beforeCatchup = sample(host)
            heldBeforeReveal = daemon.catchupPending && !caughtUp
        }
        if let revealOnMount { palette.reveal = SearchReveal(conversationID: cid, itemID: revealOnMount) }
        if pauseCatchup {
            let deadline = Date().addingTimeInterval(60)
            while palette.reveal != nil && Date() < deadline { await settle(host, seconds: 0.02) }
            await settle(host, seconds: 0.3)
            afterInitialReveal = sample(host)
            daemon.resumeCatchup()
        }
        await waitFolded(daemon.lastSeq)
        let revealDeadline = Date().addingTimeInterval(60)
        while palette.reveal != nil && Date() < revealDeadline { await settle(host, seconds: 0.02) }
        await settle(host, seconds: 1.2)
        if revealOnMount == nil {
            // Wait for the opening pin's final layout, not a fixed sampling time.
            let endDeadline = Date().addingTimeInterval(60)
            while (sample(host)["from_end"] ?? 1e9) > 30 && Date() < endDeadline {
                await settle(host, seconds: 0.02)
            }
        }
    }

    var caughtUp: Bool { model.state.timelines[cid]?.caughtUp == true }

    func waitFolded(_ target: Int) async {
        let deadline = Date().addingTimeInterval(60)
        while ((model.state.timelines[cid]?.cursor ?? 0) < target || !caughtUp) && Date() < deadline {
            await settle(host ?? NSView(), seconds: 0.02)
        }
    }

    func push(_ build: (inout Int) -> [[String: Any]]) async {
        let events = build(&seq)
        daemon.push(events)
        await waitFolded(seq)
        await settle(host, seconds: 1.0)
    }

    func stream(_ lines: Int, block: String = "live") async {
        await push { seq in
            seq += 1
            return [event(seq, live, "text.delta", ["block": block,
                    "text": (0..<lines).map { "streamed \(block) line \($0)" }.joined(separator: "\n") + "\n"])]
        }
    }

    func activate(_ query: String, item: String) async -> [String: Any] {
        let results = SearchIndex(model.state.searchCandidates()).search(query)?.results ?? []
        guard let match = results.first(where: { $0.snippet?.itemID == item }) else {
            return ["found": false, "items": results.compactMap { $0.snippet?.itemID }]
        }
        palette.activate(match)
        let deadline = Date().addingTimeInterval(60)
        while palette.reveal != nil && Date() < deadline { await settle(host, seconds: 0.02) }
        await settle(host, seconds: 0.6)
        return ["found": true, "consumed": palette.reveal == nil, "results": results.count]
    }

    /// The person's own scroll: AppKit's live-scroll notice, then the offset.
    func personScroll(toEnd: Bool, offset: CGFloat = 300) async {
        // A person scrolling to the end keeps going until it is there: lazy rows
        // measured on the way move the end, so repeat until it stays in view.
        for _ in 0..<(toEnd ? 12 : 1) {
            guard let scroll = timelineScroll(host), let document = scroll.documentView else { return }
            NotificationCenter.default.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
            let clip = scroll.contentView
            let end = document.isFlipped ? max(0, document.frame.height - clip.bounds.height) : 0
            clip.scroll(to: NSPoint(x: 0, y: toEnd ? end : offset))
            scroll.reflectScrolledClipView(clip)
            NotificationCenter.default.post(name: NSScrollView.didEndLiveScrollNotification, object: scroll)
            await settle(host, seconds: 0.25)
            if toEnd, (sample(host)["from_end"] ?? 1e9) <= 1 { break }
        }
        await settle(host, seconds: 0.6)
    }

    func close() { window.close() }
}

@main struct R4ViewProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        NSApp.finishLaunching()
        NSApp.setActivationPolicy(.accessory)
        if let mode = CommandLine.arguments.last, mode.hasPrefix("fix11-") {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await settlingReveal(root, mode: String(mode.dropFirst("fix11-".count)))
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if let mode = CommandLine.arguments.last, mode.hasPrefix("fix10-streaming") {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let only = mode == "fix10-streaming" ? nil : String(mode.dropFirst("fix10-streaming-".count))
            let result = await streamingCases(root, only: only)
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if let mode = CommandLine.arguments.last,
           mode == "fix10" || mode == "fix10-far-text-50ms" {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await fix10Cases(root, only: mode == "fix10" ? nil : "far_text_50ms")
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if let mode = CommandLine.arguments.last, mode.hasPrefix("fix9") {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await fix9Cases(root, mode: mode)
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if CommandLine.arguments.count > 2 {
            let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
            let schedule = try JSONDecoder().decode([Double].self, from: data)
            let result = await arrivalSchedule(schedule)
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        var out: [String: Any] = [:]
        let plain: (Int) -> String = { "person message \($0) asks something" }

        // A. Control: at the end with a live turn, streamed text is followed.
        do {
            let scene = await Scene("control", root: root, prior: 30, priorText: plain, tray: [])
            let opened = sample(scene.host)
            await scene.stream(40)
            out["control"] = ["opened": opened, "after_stream": sample(scene.host)]
            scene.close()
        }

        // B. A search match in a tray that shows all its rows; the timeline never moves.
        do {
            let tray = ["queued zero", "queued one", "queued two", "queued needle three"]
            let scene = await Scene("tray-reveal", root: root, prior: 30, priorText: plain, tray: tray)
            let opened = sample(scene.host)
            let activation = await scene.activate("needle", item: "person:q3")
            let afterReveal = sample(scene.host)
            await scene.stream(40)
            let afterStream = sample(scene.host)
            await scene.stream(40)
            out["tray_reveal"] = ["opened": opened, "activation": activation, "after_reveal": afterReveal,
                                  "after_stream": afterStream, "after_second_stream": sample(scene.host),
                                  "identifiers": trayIdentifiers(scene.host)]
            scene.close()
        }

        // A timeline search reveal whose target and end both fit on screen.
        // The end sentinel stays mounted; a notice must not change its visibility.
        do {
            let scene = await Scene("timeline-end", root: root, prior: 30, priorText: plain, tray: [],
                                    liveText: "the live endneedle request")
            let activation = await scene.activate("endneedle", item: "person:live")
            let afterReveal = sample(scene.host)
            await scene.stream(40)
            let afterStream = sample(scene.host)
            await scene.stream(40)
            out["timeline_end"] = ["activation": activation, "after_reveal": afterReveal,
                                    "after_stream": afterStream, "after_second_stream": sample(scene.host)]
            scene.close()
        }

        // C. A new approval card arrives at the end; the person answers; the reply streams.
        do {
            let scene = await Scene("approval-end", root: root, prior: 30, priorText: plain, tray: [])
            let opened = sample(scene.host)
            await scene.push { seq in
                seq += 1
                return [event(seq, "live", "approval.requested", ["request_id": "r1", "kind": "tool",
                        "options": ["allow", "deny"], "tool": "Bash", "summary": "run the tests"])]
            }
            let afterCard = sample(scene.host)
            await scene.push { seq in
                seq += 1
                let resolved = event(seq, "live", "approval.resolved", ["request_id": "r1", "decision": "allow"])
                seq += 1
                return [resolved, event(seq, "live", "text.delta", ["block": "after",
                        "text": (0..<40).map { "reply after approval line \($0)" }.joined(separator: "\n")])]
            }
            let afterReply = sample(scene.host)
            await scene.stream(40, block: "after")
            out["approval_end"] = ["opened": opened, "after_card": afterCard, "after_reply": afterReply,
                                   "after_more": sample(scene.host),
                                   "pending": scene.model.state.timelines["approval-end"]?.pendingApprovalItems.count ?? -1]
            scene.close()
        }

        // D. Review r3's P2-3 shape after catching up: the app's own reveal of a row
        // far up (posted and scrolled as fix 6's probe does), new arrivals, back to
        // the end, then away again.
        do {
            let scene = await Scene("cycle", root: root, prior: 30, priorText: plain, tray: [])
            let opened = sample(scene.host)
            ConversationOpeningPin.revealRow(in: "cycle")
            if let scroll = timelineScroll(scene.host) {
                scroll.contentView.scroll(to: NSPoint(x: 0, y: 250))
                scroll.reflectScrolledClipView(scroll.contentView)
            }
            await settle(scene.host, seconds: 0.8)
            let revealed = sample(scene.host)
            await scene.stream(20)
            let arrivals = sample(scene.host)
            await scene.push { seq in
                seq += 1
                return [event(seq, "live", "tool.started", ["id": "cycle-tool", "name": "Read", "summary": "a new row"])]
            }
            let newRow = sample(scene.host)
            await scene.personScroll(toEnd: true)
            let atEnd = sample(scene.host)
            await scene.stream(20)
            let followed = sample(scene.host)
            await scene.personScroll(toEnd: false, offset: 300)
            let away = sample(scene.host)
            await scene.stream(20)
            out["cycle"] = ["opened": opened, "revealed": revealed, "arrivals": arrivals, "new_row": newRow,
                            "back_at_end": atEnd, "followed": followed, "away": away, "away_arrivals": sample(scene.host)]
            scene.close()
        }

        // E. Several matches: one in the timeline, one in a collapsed tray.
        do {
            let text: (Int) -> String = { $0 == 2 ? "person message 2 needle haystack" : "person message \($0) asks something" }
            let tray = (0..<6).map { $0 == 5 ? "queued needle five" : "queued \($0)" }
            let scene = await Scene("multi", root: root, prior: 30, priorText: text, tray: tray)
            let opened = sample(scene.host)
            // One result per conversation; its snippet is the message with the most words.
            let first = await scene.activate("needle haystack", item: "person:m000002")
            let timelineFirst = sample(scene.host)
            let trayFirst = trayIdentifiers(scene.host)
            let second = await scene.activate("needle", item: "person:q5")
            let timelineSecond = sample(scene.host)
            let traySecond = trayIdentifiers(scene.host)
            let third = await scene.activate("needle haystack", item: "person:m000002")
            let timelineThird = sample(scene.host)
            let trayThird = trayIdentifiers(scene.host)
            await scene.stream(10)
            out["multi"] = ["opened": opened, "first": first, "timeline_first": timelineFirst, "tray_first": trayFirst,
                            "second": second, "timeline_second": timelineSecond, "tray_second": traySecond,
                            "third": third, "timeline_third": timelineThird, "tray_third": trayThird,
                            "tray_after_stream": trayIdentifiers(scene.host)]
            scene.close()
        }

        // F. A match in the live turn's own message, activated while the turn keeps
        // adding rows (a tool row every `interval` for 3 s). The person scrolls back
        // to the end 1 s in. Counts the app's row reveals (each precedes a scroll to
        // the match) and records whether the view leaves the end after the person's scroll.
        for (name, interval) in [("busy_200ms", 0.2), ("busy_400ms", 0.4)] {
            let scene = await Scene(name, root: root, prior: 30, priorText: plain, tray: [],
                                    liveText: "the live request findme")
            await scene.stream(30)
            var reveals: [Double] = []
            var revealStarts = 0
            var targetStayedPresent = true
            let starts = NotificationCenter.default.addObserver(forName: .init("subfleet.test.searchRevealStarted"),
                                                                 object: nil, queue: .main) { note in
                if note.object as? String == name { revealStarts += 1 }
            }
            var started = Date()
            let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                                  queue: .main) { note in
                if note.object as? String == name { reveals.append(Date().timeIntervalSince(started)) }
            }
            let results = SearchIndex(scene.model.state.searchCandidates()).search("findme")?.results ?? []
            let match = results.first { $0.snippet?.itemID == "person:live" }
            var samples: [[String: Any]] = []
            var consumedAt = -1.0
            // Observe consumption itself. The simulated person's scroll below
            // awaits layout and can delay the probe's next sampling turn.
            let consumption = scene.palette.$reveal.dropFirst().sink { value in
                if value == nil && consumedAt < 0 { consumedAt = Date().timeIntervalSince(started) }
            }
            var scrolledAt = -1.0
            var afterPerson: Double = -1
            if let match {
                started = Date()
                scene.palette.activate(match)
                var tick = 0
                var nextRow = 0.0
                while Date().timeIntervalSince(started) < 3.0 {
                    let elapsed = Date().timeIntervalSince(started)
                    if elapsed >= nextRow {
                        tick += 1
                        nextRow += interval
                        scene.seq += 1
                        scene.daemon.push([event(scene.seq, "live", "tool.started",
                                                 ["id": "t\(tick)", "name": "Read", "summary": "file \(tick)"])])
                    }
                    await settle(scene.host, seconds: 0.05)
                    if consumedAt < 0 && scene.palette.reveal == nil { consumedAt = Date().timeIntervalSince(started) }
                    targetStayedPresent = targetStayedPresent &&
                        (scene.model.state.layout(conversationID: name)?.items.contains { $0.id == "person:live" } ?? false)
                    if scrolledAt < 0 && scene.palette.reveal == nil {
                        await scene.personScroll(toEnd: true)
                        scrolledAt = Date().timeIntervalSince(started)
                        afterPerson = sample(scene.host)["from_end"] ?? -1
                    }
                    var row: [String: Any] = sample(scene.host)
                    row["t"] = Date().timeIntervalSince(started)
                    row["reveal_pending"] = scene.palette.reveal != nil
                    samples.append(row)
                }
                let deadline = Date().addingTimeInterval(60)
                while scene.palette.reveal != nil && Date() < deadline { await settle(scene.host, seconds: 0.05) }
                if consumedAt < 0 && scene.palette.reveal == nil { consumedAt = Date().timeIntervalSince(started) }
                await settle(scene.host, seconds: 0.5)
            }
            consumption.cancel()
            NotificationCenter.default.removeObserver(observer)
            NotificationCenter.default.removeObserver(starts)
            let after = samples.filter { ($0["t"] as? Double ?? 0) > scrolledAt && scrolledAt >= 0 }
            out[name] = ["found": match != nil, "consumed_at": consumedAt, "consumed": scene.palette.reveal == nil,
                         "reveal_starts": revealStarts, "target_stayed_present": targetStayedPresent,
                         "person_scrolled_at": scrolledAt,
                         "reveals_at": reveals, "after_person": afterPerson,
                         "max_from_end_after_person": after.compactMap { $0["from_end"] as? Double }.max() ?? -1,
                         "final": sample(scene.host), "samples": samples]
            scene.close()
        }

        // G. Opening a conversation at a tray row while its log is still being read
        // (ten slow catch-up pages), with the same opening and no reveal as control.
        for (name, reveal) in [("opening_control", nil as String?), ("tray_opening", "person:q3")] {
            let tray = ["queued zero", "queued one", "queued two", "queued needle three"]
            let scene = await Scene(name, root: root, prior: 120, priorText: plain, tray: tray,
                                    pageDelay: 0.25, pageSize: 40, revealOnMount: reveal)
            out[name] = ["after_open": sample(scene.host), "caught_up": scene.caughtUp,
                         "reveal_consumed": scene.palette.reveal == nil]
            scene.close()
        }

        out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        out["os"] = ProcessInfo.processInfo.operatingSystemVersionString
        print(String(decoding: try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), as: UTF8.self))
    }
}

// Drive the real SearchRevealer with bounded, generated arrival schedules.
@MainActor final class ArrivalRows: ObservableObject {
    @Published var ids = ["target"]
}
struct ArrivalView: View {
    @ObservedObject var rows: ArrivalRows
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack {
                    ForEach(rows.ids, id: \.self) { Text($0).frame(height: 25).id($0) }
                }.conversationColumn("schedule", proxy: proxy, revealIDs: Set(rows.ids))
            }
        }
    }
}
@MainActor func arrivalSchedule(_ schedule: [Double]) async -> [String: Any] {
    let rows = ArrivalRows(), palette = SearchPaletteModel()
    let host = NSHostingView(rootView: ArrivalView(rows: rows).environment(\.searchPalette, palette))
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 500), styleMask: [.borderless],
                          backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    await settle(host, seconds: 0.1)
    var reveals: [Double] = []
    var revealStarts = 0
    let starts = NotificationCenter.default.addObserver(forName: .init("subfleet.test.searchRevealStarted"),
                                                       object: nil, queue: .main) { note in
        if note.object as? String == "schedule" { revealStarts += 1 }
    }
    let started = Date()
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil, queue: .main) { note in
        if note.object as? String == "schedule" { reveals.append(Date().timeIntervalSince(started)) }
    }
    var consumed = -1.0
    let consumption = palette.$reveal.dropFirst().sink { value in
        if value == nil && consumed < 0 { consumed = Date().timeIntervalSince(started) }
    }
    palette.reveal = SearchReveal(conversationID: "schedule", itemID: "target")
    for (index, arrival) in schedule.enumerated() {
        while Date().timeIntervalSince(started) < arrival {
            await settle(host, seconds: 0.01)
            if consumed < 0 && palette.reveal == nil { consumed = Date().timeIntervalSince(started) }
        }
        rows.ids.append("arrival-\(index)")
        await settle(host, seconds: 0.01)
    }
    let deadline = Date().addingTimeInterval(60)
    while palette.reveal != nil && Date() < deadline { await settle(host, seconds: 0.01) }
    if consumed < 0 && palette.reveal == nil { consumed = Date().timeIntervalSince(started) }
    consumption.cancel()
    NotificationCenter.default.removeObserver(observer)
    NotificationCenter.default.removeObserver(starts)
    window.close()
    return ["reveals": reveals, "consumed_at": consumed, "consumed": palette.reveal == nil,
            "reveal_starts": revealStarts, "schedule": schedule]
}

// Reused from reviewer r5’s R5ViewProbe2.swift: slow live-row opening and real UIModel reopen.
/// Reviewer’s slow-page fixture, with the end-visible reveal established before releasing catch-up.
@MainActor func opening(_ name: String, root: URL, reveal: String?, delay: Double, size: Int,
                        liveText: String = "the live request") async -> [String: Any] {
    var notices = 0
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        if note.object as? String == name { notices += 1 }
    }
    let scene = await Scene(name, root: root, prior: 120, priorText: { "person message \($0) asks something" },
                            tray: [], pageDelay: delay, pageSize: size, revealOnMount: reveal, liveText: liveText,
                            pauseCatchup: true)
    let opened = sample(scene.host)
    await settle(scene.host, seconds: 1.0)
    let later = sample(scene.host)
    await scene.stream(20)
    NotificationCenter.default.removeObserver(observer)
    let result: [String: Any] = ["after_open": opened, "a_second_later": later, "after_stream": sample(scene.host),
                                 "caught_up": scene.caughtUp, "reveal_consumed": scene.palette.reveal == nil,
                                 "notices": notices, "before_catchup": scene.beforeCatchup,
                                 "after_initial_reveal": scene.afterInitialReveal,
                                 "held_before_reveal": scene.heldBeforeReveal,
                                 "settling_control": await controlledSettlingPage(name, delay: delay)]
    scene.close()
    return result
}

// A focused view fixture for the settling predicate. The full UIModel opening
// above is still exercised, but its historical pages need not happen to change
// the follow key at exactly the right point in lazy layout. Here the probe owns
// that sequence: real reveal, measured end position, then one changed-key page.
@MainActor final class SettlingPage: ObservableObject {
    @Published var loaded = false
    var suspended = false
    var request: UUID?
    var confirmations = 0
    var atBottom = false
    var transition: [String: Bool] = [:]

    var key: FollowKey {
        FollowKey(last: "live", lastIsOwnSend: false, followed: "reply:live",
                  followedLength: loaded ? 4000 : 0, tray: [], settling: true, hasHistory: false)
    }
}

struct SettlingPageView: View {
    @ObservedObject var page: SettlingPage
    let conversationID: String

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    Text("Earlier history").frame(height: 900)
                    Text("The live request").frame(height: 40).id("person:live")
                    if page.loaded { Text("One catch-up page").frame(height: 4000) }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .background(ConversationOpeningPin(conversationID: conversationID, onRevealEndVisible: { request in
                    guard page.request == request else { return }
                    page.suspended = false
                    page.confirmations += 1
                }))
                .conversationColumn(conversationID, proxy: proxy, revealIDs: ["person:live"])
            }
            .onChange(of: page.key) { old, new in
                page.transition = ["changed_key": old != new, "settling": old.settling || new.settling,
                                   "suspended": page.suspended, "at_bottom": page.atBottom]
                // This is the actual product predicate, including the mutation
                // under test. Only the fixture's state/page sequence is controlled.
                if FollowKey.move(from: old, to: new, atBottom: page.atBottom,
                                  suspended: page.suspended) == .jump {
                    var transaction = Transaction(animation: nil)
                    transaction.disablesAnimations = true
                    withTransaction(transaction) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
            }
        }
    }
}

@MainActor func controlledSettlingPage(_ name: String, delay: Double) async -> [String: Any] {
    let cid = name + "-controlled"
    let page = SettlingPage(), palette = SearchPaletteModel()
    let host = NSHostingView(rootView: SettlingPageView(page: page, conversationID: cid)
        .environment(\.searchPalette, palette))
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 760),
                          styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    var reveals = 0
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        if note.object as? String == cid {
            reveals += 1
            page.suspended = true
            page.request = note.userInfo?["request"] as? UUID
        }
    }
    defer { NotificationCenter.default.removeObserver(observer); window.close() }
    await settle(host, seconds: 0.1)
    palette.reveal = SearchReveal(conversationID: cid, itemID: "person:live")
    let deadline = Date().addingTimeInterval(60)
    while palette.reveal != nil && Date() < deadline { await settle(host, seconds: 0.02) }
    // Explicitly position at the end after the reveal and before releasing the
    // page. This does not generate a person-scroll/reveal or alter suspension.
    while Date() < deadline {
        if let scroll = timelineScroll(host), let document = scroll.documentView {
            let clip = scroll.contentView
            clip.scroll(to: NSPoint(x: 0, y: document.isFlipped ? max(0, document.frame.height - clip.bounds.height) : 0))
            scroll.reflectScrolledClipView(clip)
        }
        await settle(host, seconds: 0.02)
        if (sample(host)["from_end"] ?? 1e9) <= 30 { break }
    }
    let before = sample(host)
    page.atBottom = (before["from_end"] ?? 1e9) <= 30
    // Both original schedules are retained as stimuli, never as pass/fail bounds.
    // No page can arrive until all the above state has been established.
    await settle(host, seconds: delay)
    page.loaded = true
    while page.transition.isEmpty && Date() < deadline { await settle(host, seconds: 0.02) }
    await settle(host, seconds: 0.3)
    var result: [String: Any] = page.transition
    result["before_page"] = before
    result["after_page"] = sample(host)
    result["reveals"] = reveals
    result["confirmations"] = page.confirmations
    result["consumed"] = palette.reveal == nil
    return result
}


@MainActor func fix9Cases(_ root: URL, mode: String = "fix9") async -> [String: Any] {
    var out: [String: Any] = [:]
    if mode != "fix9-reopen" {
        for (name, delay, size) in [("open_live_250", 0.25, 40), ("open_live_100", 0.1, 20)] {
            out[name] = await opening(name, root: root, reveal: "person:live", delay: delay, size: size)
        }
    }
    if mode != "fix9-catchup" {
        let scene = await Scene("reopen", root: root, prior: 60,
                                priorText: { "person message \($0) asks something" },
                                tray: ["queued zero", "queued one", "queued two"])
        let first = scene.daemon.takeStatusIDs()
        let trayFirst = trayIdentifiers(scene.host)
        await scene.model.open(.conversation("reopen"))
        await scene.waitFolded(scene.daemon.lastSeq)
        await settle(scene.host, seconds: 0.5)
        out["reopen"] = ["first_open_status_ids": first, "reopen_status_ids": scene.daemon.takeStatusIDs(),
                         "tray_first": trayFirst, "tray_again": trayIdentifiers(scene.host),
                         "known": scene.model.state.knownMessageIDs(in: "reopen").count]
        scene.close()
    }
    out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
    return out
}


// Drive presentation until successive layout samples agree. Deadlines guard a
// hung probe; neither a timestamp nor a fixed post-reveal sleep proves landing.
@MainActor func stablePosition(_ host: NSView, awayFromEnd: Bool = false) async
    -> (position: [String: Double], samples: Int) {
    let deadline = Date().addingTimeInterval(60)
    var previous: [String: Double] = [:]
    var stable = 0
    while Date() < deadline {
        await settle(host, seconds: 0.02)
        let current = sample(host)
        let eligible = current["offset"] != nil && (!awayFromEnd || (current["from_end"] ?? 0) > 600)
        // LazyVStack can revise its offscreen height estimate while the visible
        // position stays fixed. Track the viewport geometry for this barrier.
        let unchanged = ["offset", "viewport"].allSatisfy {
            guard let old = previous[$0], let new = current[$0] else { return false }
            return abs(old - new) <= 0.5
        }
        stable = eligible && unchanged ? stable + 1 : 0
        if stable >= 3 { return (current, stable) }
        previous = current
    }
    return (sample(host), stable)
}

// Reviewer r5's five distant targets, using the real model, search activation
// and reveal code. No output is released until the actual reveal has landed.
@MainActor func farReveal(_ name: String, root: URL, interval: Double, kind: String) async -> [String: Any] {
    let word = "zebraneedle"
    let scene = await Scene(name, root: root, prior: 30,
                            priorText: { $0 == 10 ? "person message \($0) \(word) here" : "person message \($0) asks something" },
                            tray: [])
    await scene.stream(30)
    let start = sample(scene.host)
    var notices: [Double] = []
    var starts = 0
    let started = Date()
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        if note.object as? String == name { notices.append(Date().timeIntervalSince(started)) }
    }
    let startObserver = NotificationCenter.default.addObserver(forName: .init("subfleet.test.searchRevealStarted"),
                                                               object: nil, queue: .main) { note in
        if note.object as? String == name { starts += 1 }
    }
    defer {
        NotificationCenter.default.removeObserver(observer)
        NotificationCenter.default.removeObserver(startObserver)
        scene.close()
    }
    let results = SearchIndex(scene.model.state.searchCandidates()).search(word)?.results ?? []
    let match = results.first { $0.snippet?.itemID == "person:m000010" }
    var pushed = 0
    var arrivalPages = 0
    var landed = (position: start, samples: 0)
    if let match {
        scene.palette.activate(match)
        let deadline = Date().addingTimeInterval(60)
        while scene.palette.reveal != nil && Date() < deadline { await settle(scene.host, seconds: 0.02) }
        landed = await stablePosition(scene.host, awayFromEnd: true)
    }
    let beforeOutput: [String: Any] = ["position": landed.position, "stable_samples": landed.samples,
                                      "consumed": scene.palette.reveal == nil, "notices": notices.count]
    let ready = match != nil && scene.palette.reveal == nil && notices.count == 2
        && landed.samples >= 3 && (landed.position["from_end"] ?? 0) > 600
    let watch = PositionWatch(scene.host, baseline: landed.position)
    defer { watch.stop() }
    var lastArrivalAt = 0.0
    if ready {
        let count = interval > 0 ? Int((2.5 / interval).rounded(.up)) : 0
        // The interval is now a stimulus after measured landing. Release one
        // atomic page with the same finite arrivals, rather than let early
        // chunks race the two-scroll reveal or depend on timer scheduling.
        if count > 0 { await settle(scene.host, seconds: interval) }
        var page: [[String: Any]] = []
        for _ in 0..<count {
            pushed += 1
            scene.seq += 1
            if kind == "text" {
                page.append(event(scene.seq, "live", "text.delta",
                                  ["block": "live", "text": "chunk \(pushed) of streamed words\n"]))
            } else {
                page.append(event(scene.seq, "live", "tool.started",
                                  ["id": "t\(pushed)", "name": "Read", "summary": "file \(pushed)"]))
            }
        }
        if !page.isEmpty {
            lastArrivalAt = Date().timeIntervalSince(started)
            scene.daemon.push(page)
            arrivalPages += 1
        }
    }
    await scene.waitFolded(scene.seq)
    let quiet = await stablePosition(scene.host)
    if ready {
        lastArrivalAt = Date().timeIntervalSince(started)
        await scene.stream(20)
    }
    let afterMore = await stablePosition(scene.host)
    // Retain the original schedule's observation horizon as well as the gates.
    // A stable early sample cannot rule out a delayed follow-to-tail task.
    let horizon = started.addingTimeInterval(max(6.3, lastArrivalAt + 2.5))
    while Date() < horizon { await settle(scene.host, seconds: 0.02) }
    return ["found": match != nil, "start": start, "notices_at": notices, "reveal_starts": starts,
            "reveal_consumed": scene.palette.reveal == nil, "caught_up": scene.caughtUp,
            "before_output": beforeOutput, "output_released": ready,
            "folded_seq": scene.model.state.timelines[name]?.cursor ?? 0, "target_seq": scene.seq,
            "pushed": pushed, "arrival_pages": arrivalPages,
            "quiet": quiet.position, "quiet_stable_samples": quiet.samples,
            "after_more": afterMore.position, "after_more_stable_samples": afterMore.samples,
            "horizon": sample(scene.host), "horizon_at": Date().timeIntervalSince(started),
            "last_arrival_at": lastArrivalAt, "horizon_max_drift": watch.maxDrift]
}

@MainActor func farOpening(_ name: String, root: URL, delay: Double, size: Int) async -> [String: Any] {
    var notices = 0
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        if note.object as? String == name { notices += 1 }
    }
    let scene = await Scene(name, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            pageDelay: delay, pageSize: size, revealOnMount: "person:m000040")
    defer { NotificationCenter.default.removeObserver(observer); scene.close() }
    let opened = sample(scene.host)
    await scene.stream(20)
    return ["after_open": opened, "after_stream": sample(scene.host), "caught_up": scene.caughtUp,
            "reveal_consumed": scene.palette.reveal == nil, "notices": notices]
}

// Review r3's settling replay: reveal while catch-up is held, then resume the
// actual paginated model with only marker-disappearance publication delayed.
@MainActor func settlingReveal(_ root: URL, mode: String) async -> [String: Any] {
    let hooked = mode == "stale"
    let reveal: String? = mode == "plain" ? nil : mode == "near" ? "person:live" : "person:m000040"
    let started = Date()
    var timeline: [[String: Any]] = []
    ConversationFollowTest.disappearanceDelay = hooked ? 3 : 0
    ConversationFollowTest.record = { cid, entry in
        guard cid == mode else { return }
        var entry = entry
        entry["at"] = Date().timeIntervalSince(started)
        if let host = NSApp.windows.compactMap(\.contentView).first(where: { timelineScroll($0) != nil }) {
            entry["position"] = sample(host)
        }
        timeline.append(entry)
    }
    defer {
        ConversationFollowTest.record = nil
        ConversationFollowTest.disappearanceDelay = 0
    }
    let scene = await Scene(mode, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            pageDelay: 0.1, pageSize: 20, revealOnMount: reveal, pauseCatchup: true)
    defer { scene.close() }
    let opened = sample(scene.host)
    let watch = PositionWatch(scene.host, baseline: opened)
    await scene.stream(20)
    let afterStream = sample(scene.host)
    // Observe beyond the delayed marker publication as well as later output.
    await settle(scene.host, seconds: 3.2)
    watch.stop()
    let horizon = sample(scene.host)
    scene.close()
    return ["mode": mode, "hooked": hooked, "held_before_reveal": scene.heldBeforeReveal,
            "before_catchup": scene.beforeCatchup, "after_initial_reveal": scene.afterInitialReveal,
            "after_open": opened, "after_stream": afterStream, "horizon": horizon,
            "max_drift": watch.maxDrift, "caught_up": scene.caughtUp,
            "reveal_consumed": scene.palette.reveal == nil, "timeline": timeline,
            "visible_windows": NSApp.windows.filter(\.isVisible).count]
}

@MainActor func fix10Cases(_ root: URL, only: String? = nil) async -> [String: Any] {
    var out: [String: Any] = [:]
    for (name, interval, kind) in [("far_quiet", 0.0, "text"), ("far_text_50ms", 0.05, "text"),
                                   ("far_text_120ms", 0.12, "text"), ("far_rows_150ms", 0.15, "tool"),
                                   ("far_rows_400ms", 0.4, "tool")] where only == nil || only == name {
        out[name] = await farReveal(name, root: root, interval: interval, kind: kind)
    }
    for (name, delay, size) in [("open_reveal_250", 0.25, 40), ("open_reveal_100", 0.1, 20)] where only == nil || only == name {
        out[name] = await farOpening(name, root: root, delay: delay, size: size)
    }
    out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
    out["mounted_windows"] = ProbePresentation.mountedWindows
    out["animation_calls"] = ProbePresentation.animationCalls
    return out
}
// Passive samples cover the whole post-landing period, including waits for
// folding and further output. Stability gates alone can accept slow drift.
@MainActor final class PositionWatch {
    var maxDrift = 0.0
    private var timer: Timer?
    init(_ host: NSView, baseline: [String: Double]) {
        timer = Timer(timeInterval: 0.025, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                let current = sample(host)
                self.maxDrift = max(self.maxDrift, abs((current["offset"] ?? 0) - (baseline["offset"] ?? 0)))
            }
        }
        RunLoop.main.add(timer!, forMode: .common)
    }
    func stop() { timer?.invalidate() }
}

// Daemon pushes run independently of the main actor's presentation work.
final class RevealArrivals: @unchecked Sendable {
    private let lock = NSLock()
    private var times: [Double] = []
    private var pages = 0
    func push(_ events: [[String: Any]], to daemon: R4ViewDaemon, at: Double) {
        lock.lock()
        times += Array(repeating: at, count: events.count)
        pages += 1
        daemon.push(events)
        lock.unlock()
    }
    var snapshot: (times: [Double], pages: Int) {
        lock.lock(); defer { lock.unlock() }
        return (times, pages)
    }
}

@MainActor func streamingReveal(_ name: String, root: URL, kind: String,
                                delay: Double = 0, interval: Double = 0,
                                boundary: String? = nil, staleMarker: Bool = false,
                                consumptionPause: Double = 0, firstNoticePause: Double = 0) async -> [String: Any] {
    let word = "zebraneedle"
    let scene = await Scene(name, root: root, prior: 30,
                            priorText: { $0 == 10 ? "person message \($0) \(word) here" : "person message \($0) asks something" },
                            tray: [], immediateDelivery: true)
    await scene.stream(30)
    // Construction counts, never runtime-dependent timer counts.
    let count = boundary != nil ? 10 : interval > 0 ? Int((2.5 / interval).rounded(.up)) : 1
    var events: [[String: Any]] = []
    for index in 1...count {
        scene.seq += 1
        events.append(event(scene.seq, "live", kind == "text" ? "text.delta" : "tool.started",
                            kind == "text" ? ["block": "live", "text": "overlap chunk \(index)\n"]
                            : ["id": "overlap\(index)", "name": "Read", "summary": "file \(index)"]))
    }
    let sequences = events.map { $0["seq"] as! Int }
    let arrivals = RevealArrivals()
    var folds: [Int: Double] = [:]
    var notices: [Double] = []
    var starts = 0
    var consumed = -1.0
    var boundaryArrival = -1.0
    var started = Date()
    var producer: Task<Void, Never>?
    let consumptionGate = DispatchSemaphore(value: 0)
    var timeline: [[String: Any]] = []
    ConversationFollowTest.disappearanceDelay = staleMarker ? 3 : 0
    ConversationFollowTest.record = { cid, entry in
        guard cid == name else { return }
        timeline.append(entry.merging(["at": Date().timeIntervalSince(started), "position": sample(scene.host)]) { _, new in new })
    }
    var previousPosition: [String: Double] = [:]
    let positionTimer = Timer(timeInterval: 0.025, repeats: true) { _ in
        MainActor.assumeIsolated {
            let position = sample(scene.host)
            if position != previousPosition {
                timeline.append(["event": "position", "at": Date().timeIntervalSince(started), "position": position])
                previousPosition = position
            }
        }
    }
    RunLoop.main.add(positionTimer, forMode: .common)
    let folded = scene.model.$state.sink { state in
        let cursor = state.timelines[name]?.cursor ?? 0
        for seq in sequences where seq <= cursor && folds[seq] == nil {
            folds[seq] = Date().timeIntervalSince(started)
        }
    }
    func burst(at time: Double) {
        guard boundaryArrival < 0 else { return }
        boundaryArrival = time
        arrivals.push(events, to: scene.daemon, at: time)
    }
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        guard note.object as? String == name else { return }
        // Inject presentation scheduling delay, leaving the detached producer free.
        if notices.isEmpty && firstNoticePause > 0 { Thread.sleep(forTimeInterval: firstNoticePause) }
        let at = Date().timeIntervalSince(started)
        notices.append(at)
        if (boundary == "first_notice" && notices.count == 1)
            || (boundary == "second_notice" && notices.count == 2) { burst(at: at) }
        // Model a slow presentation turn after the second notice. Detached
        // producers keep delivering; only the reserved trailing event waits
        // for consumption, even after the nominal finite stream would end.
        if notices.count == 2 && consumptionPause > 0 { Thread.sleep(forTimeInterval: consumptionPause) }
    }
    let startObserver = NotificationCenter.default.addObserver(forName: .init("subfleet.test.searchRevealStarted"),
                                                               object: nil, queue: .main) { note in
        guard note.object as? String == name else { return }
        starts += 1
        guard boundary == nil && producer == nil else { return }
        let daemon = scene.daemon
        let epoch = started
        let page = events
        producer = Task.detached {
            // Absolute deadlines avoid accumulating main-actor or timer delay.
            for (index, event) in page.enumerated() {
                // Reserve the final event for actual consumption. Earlier
                // arrivals retain their independent absolute schedule.
                if interval > 0 && index == page.count - 1 {
                    guard consumptionGate.wait(timeout: .now() + 60) == .success else { return }
                }
                let deadline = epoch.addingTimeInterval(delay + Double(index) * interval)
                let wait = deadline.timeIntervalSinceNow
                if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1e9)) }
                if Task.isCancelled { return }
                arrivals.push([event], to: daemon, at: Date().timeIntervalSince(epoch))
            }
        }
    }
    let consumption = scene.palette.$reveal.dropFirst().sink { value in
        if value == nil && consumed < 0 {
            consumed = Date().timeIntervalSince(started)
            consumptionGate.signal()
            if boundary == "consumption" { burst(at: consumed) }
        }
    }
    defer {
        producer?.cancel()
        consumptionGate.signal()
        positionTimer.invalidate()
        ConversationFollowTest.record = nil
        ConversationFollowTest.disappearanceDelay = 0
        folded.cancel()
        consumption.cancel()
        NotificationCenter.default.removeObserver(observer)
        NotificationCenter.default.removeObserver(startObserver)
        scene.close()
    }
    let results = SearchIndex(scene.model.state.searchCandidates()).search(word)?.results ?? []
    let match = results.first { $0.snippet?.itemID == "person:m000010" }
    started = Date()
    if let match { scene.palette.activate(match) }
    let deadline = Date().addingTimeInterval(60)
    if interval == 0 && boundary == nil {
        // The early-input scenes need UIModel's two main-queue round trips to
        // fold the page before the first notice. Repeated forced AppKit layout
        // here can monopolize that queue for longer than the reveal's sleep.
        // Pump delivery without forcing presentation until the input folds;
        // the real reveal task and both of its timers continue running.
        while folds.count < count && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.001))
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
    }
    while scene.palette.reveal != nil && Date() < deadline { await settle(scene.host, seconds: 0.02) }
    // Do not require distance inside the wait: a bad landing must be reported
    // promptly and rejected by Python, rather than hidden behind a timeout.
    let landed = await stablePosition(scene.host)
    let watch = PositionWatch(scene.host, baseline: landed.position)
    defer { watch.stop() }
    if match != nil { await scene.waitFolded(scene.seq) }
    await producer?.value
    let quiet = await stablePosition(scene.host)
    let lastArrival = Date().timeIntervalSince(started)
    await scene.stream(20)
    let afterMore = await stablePosition(scene.host)
    let horizon = started.addingTimeInterval(max(6.3, lastArrival + 2.5))
    while Date() < horizon { await settle(scene.host, seconds: 0.02) }
    let final = sample(scene.host)
    let delivered = arrivals.snapshot
    return ["found": match != nil, "caught_up": scene.caughtUp, "reveal_consumed": scene.palette.reveal == nil,
            "notices_at": notices, "reveal_starts": starts, "consumed_at": consumed,
            "stale_marker": staleMarker, "consumption_pause": consumptionPause,
            "first_notice_pause": firstNoticePause, "timeline": timeline,
            "pushed": delivered.times.count, "arrival_pages": delivered.pages,
            "arrival_sequences": sequences, "arrivals_at": delivered.times, "folds_at": sequences.compactMap { folds[$0] },
            "boundary_arrival_at": boundaryArrival,
            "folded_seq": scene.model.state.timelines[name]?.cursor ?? 0, "target_seq": scene.seq,
            "landed": landed.position, "landed_stable_samples": landed.samples,
            "quiet": quiet.position, "after_more": afterMore.position, "horizon": final,
            "horizon_at": Date().timeIntervalSince(started), "horizon_max_drift": watch.maxDrift]
}

@MainActor func streamingCases(_ root: URL, only: String? = nil) async -> [String: Any] {
    var out: [String: Any] = [:]
    for kind in ["text", "tool"] {
        for delay in [0, 16, 50, 120] {
            let name = "early_\(kind)_\(delay)ms"
            if only == nil || only == name {
                out[name] = await streamingReveal(name, root: root, kind: kind, delay: Double(delay) / 1000)
            }
        }
        let name = "background_\(kind)"
        if only == nil || only == name {
            out[name] = await streamingReveal(name, root: root, kind: kind, interval: kind == "text" ? 0.05 : 0.15)
        }
        let delayedName = "delayed_background_\(kind)"
        if only == delayedName {
            out[delayedName] = await streamingReveal(delayedName, root: root, kind: kind,
                interval: kind == "text" ? 0.05 : 0.15, firstNoticePause: 3)
        }
        let staleName = "stale_\(kind)"
        if only == staleName {
            out[staleName] = await streamingReveal(staleName, root: root, kind: kind,
                interval: kind == "text" ? 0.05 : 0.15, staleMarker: true)
        }
        for boundary in ["first_notice", "second_notice", "consumption"] {
            let name = "burst_\(kind)_\(boundary)"
            if only == nil || only == name {
                out[name] = await streamingReveal(name, root: root, kind: kind, boundary: boundary)
            }
        }
    }
    if only == nil || only == "late_consumption_tool" {
        out["late_consumption_tool"] = await streamingReveal("late_consumption_tool", root: root,
            kind: "tool", interval: 0.15, consumptionPause: 3)
    }
    out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
    out["mounted_windows"] = ProbePresentation.mountedWindows
    out["animation_calls"] = ProbePresentation.animationCalls
    return out
}
