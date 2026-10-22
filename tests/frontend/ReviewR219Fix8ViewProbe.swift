import AppKit
import SwiftUI

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
    private let lock = NSLock()
    private var log: [[String: Any]]
    let conversation: [String: Any]
    let receipts: [[String: Any]]
    /// A slow catch-up: each page read with wait_s 0 takes this long and holds at most pageSize events.
    var pageDelay = 0.0
    var pageSize = 200

    init(conversation: [String: Any], receipts: [[String: Any]], events: [[String: Any]]) {
        self.conversation = conversation
        self.receipts = receipts
        self.log = events
    }

    var lastSeq: Int {
        lock.lock(); defer { lock.unlock() }
        return (log.last?["seq"] as? Int) ?? 0
    }

    func push(_ events: [[String: Any]]) {
        lock.lock(); log += events; lock.unlock()
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
            out = ["messages": receipts.filter { ids.contains($0["message_id"] as! String) }]
        case "conversation.events":
            let after = value["after"]?.int ?? 0
            if pageDelay > 0, (value["wait_s"]?.double ?? 0) == 0 { Thread.sleep(forTimeInterval: pageDelay) }
            var events = page(after: after)
            if events.isEmpty, (value["wait_s"]?.double ?? 0) > 0 {
                Thread.sleep(forTimeInterval: 0.03)
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

    init(_ cid: String, root: URL, prior: Int, priorText: (Int) -> String, tray: [String],
         pageDelay: Double = 0, pageSize: Int = 200, revealOnMount: String? = nil,
         liveText: String = "the live request") async {
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
        daemon = R4ViewDaemon(conversation: conversation, receipts: receipts, events: events)
        daemon.pageDelay = pageDelay
        daemon.pageSize = pageSize
        model = UIModel(paths: .rooted(at: root.appendingPathComponent(cid)), client: daemon, defaults: R4Defaults())
        palette.model = model
        await model.open(.conversation(cid))
        guard let shown = model.state.conversation(cid) else {
            FileHandle.standardError.write(Data("open failed: \(model.problem ?? "no problem reported")\n".utf8))
            exit(3)
        }
        host = NSHostingView(rootView: AnyView(ConversationView(model: model, conversation: shown)
            .environment(\.searchPalette, palette)))
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 760), styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        host.frame = window.contentView!.bounds
        // The palette set this before the conversation showed (activate, then select).
        if let revealOnMount { palette.reveal = SearchReveal(conversationID: cid, itemID: revealOnMount) }
        await waitFolded(daemon.lastSeq)
        await settle(host, seconds: 1.2)
    }

    var caughtUp: Bool { model.state.timelines[cid]?.caughtUp == true }

    func waitFolded(_ target: Int) async {
        let deadline = Date().addingTimeInterval(8)
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
        let deadline = Date().addingTimeInterval(6)
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
            var started = Date()
            let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                                  queue: .main) { note in
                if note.object as? String == name { reveals.append(Date().timeIntervalSince(started)) }
            }
            let results = SearchIndex(scene.model.state.searchCandidates()).search("findme")?.results ?? []
            let match = results.first { $0.snippet?.itemID == "person:live" }
            var samples: [[String: Any]] = []
            var consumedAt = -1.0
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
                    if scrolledAt < 0 && Date().timeIntervalSince(started) >= 1.0 {
                        await scene.personScroll(toEnd: true)
                        scrolledAt = Date().timeIntervalSince(started)
                        afterPerson = sample(scene.host)["from_end"] ?? -1
                    }
                    var row: [String: Any] = sample(scene.host)
                    row["t"] = Date().timeIntervalSince(started)
                    row["reveal_pending"] = scene.palette.reveal != nil
                    samples.append(row)
                }
                let deadline = Date().addingTimeInterval(4)
                while scene.palette.reveal != nil && Date() < deadline { await settle(scene.host, seconds: 0.05) }
                if consumedAt < 0 && scene.palette.reveal == nil { consumedAt = Date().timeIntervalSince(started) }
                await settle(scene.host, seconds: 0.5)
            }
            NotificationCenter.default.removeObserver(observer)
            let after = samples.filter { ($0["t"] as? Double ?? 0) > scrolledAt && scrolledAt >= 0 }
            out[name] = ["found": match != nil, "consumed_at": consumedAt, "person_scrolled_at": scrolledAt,
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
    let started = Date()
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil, queue: .main) { note in
        if note.object as? String == "schedule" { reveals.append(Date().timeIntervalSince(started)) }
    }
    palette.reveal = SearchReveal(conversationID: "schedule", itemID: "target")
    var consumed = -1.0
    for (index, arrival) in schedule.enumerated() {
        while Date().timeIntervalSince(started) < arrival {
            await settle(host, seconds: 0.01)
            if consumed < 0 && palette.reveal == nil { consumed = Date().timeIntervalSince(started) }
        }
        rows.ids.append("arrival-\(index)")
        await settle(host, seconds: 0.01)
    }
    let deadline = Date().addingTimeInterval(2)
    while palette.reveal != nil && Date() < deadline { await settle(host, seconds: 0.01) }
    if consumed < 0 && palette.reveal == nil { consumed = Date().timeIntervalSince(started) }
    NotificationCenter.default.removeObserver(observer)
    window.close()
    return ["reveals": reveals, "consumed_at": consumed, "schedule": schedule]
}
