import AppKit
import SwiftUI
import Combine

// Position/count probes need a live view hierarchy and synchronous scroll
// transactions. This module-local overload controls the app's unqualified
// withAnimation calls only when this probe is compiled alongside its sources.
// A natural scene (review r8) removes both controls, this overload and the
// scene's root transaction, so the app's own animations run as they do in the app.
enum ProbePresentation {
    static var animationCalls = 0
    static var mountedWindows = 0
    static var afterAnimation: (() -> Void)?
    static var natural = false
    /// What a natural scene handed to SwiftUI: each animation, and the root transactions it
    /// let through unchanged (with how many carried an animation).
    static var forwardedAnimations: [String] = []
    static var rootTransactions = 0
    static var animatedRootTransactions = 0
}
func withAnimation<Value>(_ animation: Animation? = .default, _ body: () -> Value) -> Value {
    ProbePresentation.animationCalls += 1
    if ProbePresentation.natural {
        ProbePresentation.forwardedAnimations.append(animation.map { String(describing: $0) } ?? "nil")
        return SwiftUI.withAnimation(animation, body)
    }
    var transaction = Transaction(animation: nil)
    transaction.disablesAnimations = true
    let result = SwiftUI.withTransaction(transaction, body)
    ProbePresentation.afterAnimation?()
    return result
}

func withAnimation<Value>(_ animation: Animation?, completionCriteria: AnimationCompletionCriteria,
                          _ body: () -> Value, completion: @escaping () -> Void) -> Value {
    ProbePresentation.animationCalls += 1
    if ProbePresentation.natural {
        ProbePresentation.forwardedAnimations.append(animation.map { String(describing: $0) } ?? "nil")
        return SwiftUI.withAnimation(animation, completionCriteria: completionCriteria, body, completion: completion)
    }
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

// A scene's layout, from the environment, so a scene can be replayed on more than
// one (round 8's continuation: CI's macOS 15 runner laid round 7's shrink replay
// out differently from a lane, and only there did its fixed shrink not clamp).
// `R219_PROBE_WINDOW_HEIGHT` is the scene window's height in points (760 unless
// set) and `R219_PROBE_TEXT_SCALE` the conversation's text scale (actual size
// unless set), the one View > Bigger and Smaller step.
@MainActor enum ProbeLayout {
    static let windowHeight = CGFloat(ProcessInfo.processInfo.environment["R219_PROBE_WINDOW_HEIGHT"]
        .flatMap(Double.init) ?? 760)
    static let textScale = TextScale.clamp(ProcessInfo.processInfo.environment["R219_PROBE_TEXT_SCALE"]
        .flatMap(Double.init) ?? TextScale.actual)
    static var described: [String: Any] { ["window_height": Double(windowHeight), "text_scale": textScale] }
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

    // Round 8: the next events read answers `reset: true`, as when the cursor fell
    // behind the compaction floor; the model drops what events made and re-reads.
    private var resetPending = false
    func resetOnce() {
        lock.lock(); resetPending = true; lock.unlock()
    }
    private func takeReset() -> Bool {
        lock.lock(); defer { lock.unlock() }
        let pending = resetPending
        resetPending = false
        return pending
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
            if takeReset() {
                return try json(["events": [], "next": 0, "reset": true]).decode(R.self)
            }
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

// Readiness waits fail loudly (review r5: an expired waitFolded let a case
// sample geometry from a conversation still catching up). An expired wait
// names itself and its last state on stderr and ends the probe, so pytest
// reports it instead of a position. Deadlines guard hung scenes only: they
// are generous for loaded hosts, and the elapsed time of every wait is kept.
let readinessDeadline = 120.0

@MainActor enum ProbeWaits {
    static var log: [[String: Any]] = []
    static func since(_ mark: Int) -> [[String: Any]] { Array(log[min(mark, log.count)...]) }
    /// A failed wait's whole report goes to this file, when a scene names one: stderr
    /// keeps only the last state, and CI's log keeps only its first characters (CI run
    /// 40303045129 lost the shrink replay's steps that way). `context` adds the
    /// scene's steps and traces so far.
    static var failureFile: URL?
    static var context: (() -> [String: Any])?
}

@MainActor func writeWaitFailure(_ name: String, reason: String, state: [String: Any]) {
    guard let file = ProbeWaits.failureFile else { return }
    var report: [String: Any] = ["wait": name, "reason": reason, "state": state, "waits": ProbeWaits.log,
                                 "layout": ProbeLayout.described]
    if let context = ProbeWaits.context { report["context"] = context() }
    guard JSONSerialization.isValidJSONObject(report),
          let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys]) else { return }
    try? data.write(to: file)
}

@MainActor func readinessExpired(_ name: String, seconds: Double, state: [String: Any]) -> Never {
    let detail = (try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]))
        .map { String(decoding: $0, as: UTF8.self) } ?? String(describing: state)
    writeWaitFailure(name, reason: "expired after \(seconds) s", state: state)
    FileHandle.standardError.write(Data("readiness wait '\(name)' expired after \(seconds) s; last state: \(detail)\n".utf8))
    exit(70)
}

/// A wait whose precondition no longer holds can never be met by waiting longer: it
/// ends the probe at once, naming what is missing (round 8's continuation: CI's
/// shrink replay waited out 20 s for a clamp that rows measured after it had undone).
@MainActor func readinessUnmet(_ name: String, _ missing: String, state: [String: Any]) -> Never {
    let detail = (try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]))
        .map { String(decoding: $0, as: UTF8.self) } ?? String(describing: state)
    writeWaitFailure(name, reason: "cannot be met: \(missing)", state: state)
    FileHandle.standardError.write(Data("readiness wait '\(name)' cannot be met: \(missing); last state: \(detail)\n".utf8))
    exit(71)
}

/// `unmet` names a precondition of the wait that no longer holds, or is nil while all do.
@MainActor func waitReady(_ name: String, seconds: Double = readinessDeadline, view: NSView?, step: Double = 0.02,
                          state: () -> [String: Any] = { [:] }, unmet: () -> String? = { nil },
                          until ready: () -> Bool) async {
    let started = Date()
    while !ready() {
        if let missing = unmet() { readinessUnmet(name, missing, state: state()) }
        if Date().timeIntervalSince(started) >= seconds { readinessExpired(name, seconds: seconds, state: state()) }
        await settle(view ?? NSView(), seconds: step)
    }
    ProbeWaits.log.append(["wait": name, "elapsed": Date().timeIntervalSince(started)])
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

// The model of the scene being mounted, for observers registered in onMount.
@MainActor enum ProbeScene { static var model: UIModel? }

// Each document height change with the model state beside it (round 7): a
// change with no page, history or row change is the lazy stack's measurement.
@MainActor func documentObserver(_ cid: String, host: NSView, started: Date,
                                 record: @escaping ([String: Any]) -> Void) -> NSObjectProtocol? {
    guard let document = timelineScroll(host)?.documentView else { return nil }
    document.postsFrameChangedNotifications = true
    var previous = Double(document.frame.height)
    return NotificationCenter.default.addObserver(forName: NSView.frameDidChangeNotification, object: document,
                                                  queue: nil) { _ in
        MainActor.assumeIsolated {
            let height = Double(document.frame.height)
            guard abs(height - previous) > 0.5 else { return }
            var entry: [String: Any] = ["at": Date().timeIntervalSince(started), "previous": previous,
                                        "content": height, "position": sample(host)]
            previous = height
            if let model = ProbeScene.model, let timeline = model.state.timelines[cid] {
                let layout = model.state.layout(conversationID: cid)
                entry["cursor"] = timeline.cursor
                entry["caught_up"] = timeline.caughtUp
                entry["history_pages"] = timeline.historyPagesLoaded
                entry["resets"] = timeline.resets
                entry["items"] = layout?.items.count ?? -1
                entry["rows"] = WorkPresentation.rows(in: timeline, items: layout?.items).count
            }
            record(entry)
        }
    }
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
         immediateDelivery: Bool = false, onMount: ((NSView) -> Void)? = nil,
         silentFoldDeadline: Double? = nil, holdCatchup: Bool = false, liveState: String = "running") async {
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
                         "state": liveState, "text": liveText])
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
            .environment(\.searchPalette, palette).environment(\.textScale, ProbeLayout.textScale)
            .transaction { transaction in
                if ProbePresentation.natural {
                    ProbePresentation.rootTransactions += 1
                    if transaction.animation != nil { ProbePresentation.animatedRootTransactions += 1 }
                    return
                }
                transaction.animation = nil
                transaction.disablesAnimations = true
            }))
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: ProbeLayout.windowHeight),
                          styleMask: [.borderless],
                          backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        host.frame = window.contentView!.bounds
        mountOffscreen(window)
        ProbeScene.model = model
        onMount?(host)
        let mounted: NSView = host
        if pauseCatchup {
            // Establish the invariant's actual precondition, independent of host scheduling:
            // the opening pin has placed the live row/end on screen, and catch-up is pending.
            await waitReady("scene.catchup_held_at_end", view: mounted, state: { self.readiness() }) {
                self.daemon.catchupPending && (sample(mounted)["from_end"] ?? 1e9) <= 30
            }
            beforeCatchup = sample(mounted)
            heldBeforeReveal = daemon.catchupPending && !caughtUp
            // The scene drives its own schedule with catch-up still held.
            if holdCatchup { return }
        }
        if let revealOnMount { palette.reveal = SearchReveal(conversationID: cid, itemID: revealOnMount) }
        if pauseCatchup {
            await waitReady("scene.reveal_consumed_before_catchup", view: mounted, state: { self.readiness() }) {
                self.palette.reveal == nil
            }
            await settle(mounted, seconds: 0.3)
            afterInitialReveal = sample(mounted)
            daemon.resumeCatchup()
        }
        if let silentFoldDeadline {
            // Checkpoint replay only: the pre-round-6 wait, which carried on
            // silently at its deadline while catch-up was still running.
            let deadline = Date().addingTimeInterval(silentFoldDeadline)
            while ((model.state.timelines[cid]?.cursor ?? 0) < daemon.lastSeq || !caughtUp) && Date() < deadline {
                await settle(mounted, seconds: 0.02)
            }
        } else {
            await waitFolded(daemon.lastSeq, "scene.waitFolded")
        }
        foldedReadiness = readiness()
        await waitReady("scene.reveal_consumed", view: mounted, state: { self.readiness() }) { self.palette.reveal == nil }
        await settle(mounted, seconds: 1.2)
        if revealOnMount == nil {
            // Wait for the opening pin's final layout, not a fixed sampling time.
            await waitReady("scene.opened_at_end", view: mounted, state: { self.readiness() }) {
                (sample(mounted)["from_end"] ?? 1e9) <= 30
            }
        }
    }

    var caughtUp: Bool { model.state.timelines[cid]?.caughtUp == true }
    /// Readiness when the opening's catch-up wait completed (before its settle).
    var foldedReadiness: [String: Any] = [:]

    /// Catch-up/cursor state for readiness checkpoints and expired-wait reports.
    func readiness(target: Int? = nil) -> [String: Any] {
        var state: [String: Any] = ["scene": cid, "cursor": model.state.timelines[cid]?.cursor ?? 0,
                                    "last_seq": daemon.lastSeq, "caught_up": caughtUp,
                                    "catchup_pending": daemon.catchupPending,
                                    "reveal_pending": palette.reveal != nil]
        if let target { state["target"] = target }
        if let host { state["position"] = sample(host) }
        return state
    }

    func waitFolded(_ target: Int, _ name: String = "waitFolded") async {
        await waitReady(name, view: host, state: { self.readiness(target: target) }) {
            (self.model.state.timelines[self.cid]?.cursor ?? 0) >= target && self.caughtUp
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
        await waitReady("activate.reveal_consumed", view: host, state: { self.readiness() }) { self.palette.reveal == nil }
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
            let result = mode == "fix11-live-turn" ? await suspendedLiveTurn(root)
                : await settlingReveal(root, mode: String(mode.dropFirst("fix11-".count)))
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if let mode = CommandLine.arguments.last, mode.hasPrefix("fix12-") {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await fix12Cases(root, mode: String(mode.dropFirst("fix12-".count)))
            print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), as: UTF8.self))
            return
        }
        if let mode = CommandLine.arguments.last, mode.hasPrefix("fix10-shrink-") {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await shrinkReplay(root, mode: String(mode.dropFirst("fix10-shrink-".count)))
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
           mode == "fix10" || mode == "fix10-far-text-50ms" || mode == "fix10-completion-first" {
            let root = URL(fileURLWithPath: CommandLine.arguments[1])
            let result = await fix10Cases(root, only: mode == "fix10" ? nil
                                          : mode == "fix10-completion-first" ? "open_reveal_completion_first"
                                          : "far_text_50ms")
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
                await waitReady("busy.reveal_consumed", view: scene.host, step: 0.05,
                                state: { scene.readiness() }) { scene.palette.reveal == nil }
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
    await waitReady("schedule.reveal_consumed", view: host, step: 0.01,
                    state: { ["schedule": schedule, "rows": rows.ids.count] }) { palette.reveal == nil }
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
    // Readiness flags are sampled with the position they describe.
    let opened = sample(scene.host)
    let openedReadiness = scene.readiness()
    await settle(scene.host, seconds: 1.0)
    let later = sample(scene.host)
    await scene.stream(20)
    NotificationCenter.default.removeObserver(observer)
    let result: [String: Any] = ["after_open": opened, "a_second_later": later, "after_stream": sample(scene.host),
                                 "caught_up": openedReadiness["caught_up"] as? Bool ?? false,
                                 "reveal_consumed": !(openedReadiness["reveal_pending"] as? Bool ?? true),
                                 "readiness_at_open": openedReadiness, "readiness_at_fold": scene.foldedReadiness,
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
                    // Marked as ConversationView marks a reveal's row, for the pin's landing check.
                    Text("The live request").frame(height: 40)
                        .background(RevealTargetMark(conversationID: conversationID, itemID: "person:live"))
                        .id("person:live")
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
    let state = { ["controlled": cid, "reveal_pending": palette.reveal != nil, "position": sample(host),
                   "transition": page.transition] as [String: Any] }
    await waitReady("controlled.reveal_consumed", view: host, state: state) { palette.reveal == nil }
    // Explicitly position at the end after the reveal and before releasing the
    // page. This does not generate a person-scroll/reveal or alter suspension.
    let positioning = Date()
    while true {
        if let scroll = timelineScroll(host), let document = scroll.documentView {
            let clip = scroll.contentView
            clip.scroll(to: NSPoint(x: 0, y: document.isFlipped ? max(0, document.frame.height - clip.bounds.height) : 0))
            scroll.reflectScrolledClipView(clip)
        }
        await settle(host, seconds: 0.02)
        if (sample(host)["from_end"] ?? 1e9) <= 30 { break }
        if Date().timeIntervalSince(positioning) >= readinessDeadline {
            readinessExpired("controlled.positioned_at_end", seconds: readinessDeadline, state: state())
        }
    }
    ProbeWaits.log.append(["wait": "controlled.positioned_at_end", "elapsed": Date().timeIntervalSince(positioning)])
    let before = sample(host)
    page.atBottom = (before["from_end"] ?? 1e9) <= 30
    // Both original schedules are retained as stimuli, never as pass/fail bounds.
    // No page can arrive until all the above state has been established.
    await settle(host, seconds: delay)
    page.loaded = true
    await waitReady("controlled.key_transition", view: host, state: state) { !page.transition.isEmpty }
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
// The away-from-end variant is a behavioral wait: its expiry is a bad landing,
// reported with fewer than three stable samples for Python to reject. The
// plain variant is readiness for a later baseline, so its expiry is loud.
@MainActor func stablePosition(_ host: NSView, awayFromEnd: Bool = false) async
    -> (position: [String: Double], samples: Int) {
    let started = Date()
    let deadline = started.addingTimeInterval(awayFromEnd ? 60 : readinessDeadline)
    var previous: [String: Double] = [:]
    var stable = 0
    defer { ProbeWaits.log.append(["wait": "stablePosition", "elapsed": Date().timeIntervalSince(started),
                                   "away_from_end": awayFromEnd, "stable": stable]) }
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
    if !awayFromEnd {
        readinessExpired("stablePosition", seconds: readinessDeadline,
                         state: ["position": sample(host), "previous": previous, "stable": stable])
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
        await waitReady("far.reveal_consumed", view: scene.host, state: { scene.readiness() }) {
            scene.palette.reveal == nil
        }
        landed = await stablePosition(scene.host, awayFromEnd: true)
    }
    // Readiness is sampled with the landing it qualifies, not after the horizon.
    let beforeOutput: [String: Any] = ["position": landed.position, "stable_samples": landed.samples,
                                      "consumed": scene.palette.reveal == nil, "notices": notices.count,
                                      "readiness": scene.readiness()]
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

// Round 6: deliver each reveal request's completion before its scroll is
// presented, the order a loaded opening recorded (both completions arrived with
// the clip still at the old end). Registered once the view is mounted, after the
// view's own request observer, so the completion matches the current request.
// With `gate` (round 8), the first request's order is established rather than
// hoped for: in the request's own main-actor turn the probe applies pending layout,
// puts the clip at the end as the opening pin does, samples it and completes. The
// SearchRevealer's mount-time 250 ms schedule had let a loaded host's opening pin
// still be settling (69.5 pt from the end in round 7's run, 24 pt in review r6's).
@MainActor func completeBeforeScroll(_ cid: String, started: Date, host: @escaping () -> NSView?, gate: Bool = false,
                                     record: @escaping ([String: Any]) -> Void) -> NSObjectProtocol {
    var gated = false
    return NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil, queue: nil) { note in
        guard note.object as? String == cid, let request = note.userInfo?["request"] as? UUID else { return }
        MainActor.assumeIsolated {
            var entry: [String: Any] = [:]
            if gate && !gated, let mounted = host(), let scroll = timelineScroll(mounted),
               let document = scroll.documentView {
                gated = true
                (scroll.window?.contentView ?? scroll).layoutSubtreeIfNeeded()
                let clip = scroll.contentView
                let end = document.isFlipped ? max(document.bounds.minY, document.bounds.maxY - clip.bounds.height)
                    : document.bounds.minY
                entry["gated_from"] = sample(mounted)
                clip.scroll(to: NSPoint(x: clip.bounds.minX, y: end))
                scroll.reflectScrolledClipView(clip)
            }
            entry.merge(["at": Date().timeIntervalSince(started), "position": host().map { sample($0) } ?? [:]]) { _, new in new }
            record(entry)
            ConversationOpeningPin.didRevealRow(in: cid, request: request)
        }
    }
}

// The unpaused opening reveal also records a failure trace (round 6): every
// reveal request and completion, each follow decision and suspension change,
// every presented clip geometry, and catch-up state at the opening sample.
// Python reports the suspension release and the first unexpected scroll.
@MainActor func farOpening(_ name: String, root: URL, delay: Double, size: Int,
                            completionFirst: Bool = false) async -> [String: Any] {
    var notices = 0
    let started = Date()
    var host: NSView?
    var early: [[String: Any]] = []
    var earlyObserver: NSObjectProtocol?
    var trace: [[String: Any]] = []
    var geometry: [[String: Any]] = []
    var requests: [[String: Any]] = []
    var completions: [[String: Any]] = []
    var documents: [[String: Any]] = []
    var boundsObserver: NSObjectProtocol?
    var documentChanges: NSObjectProtocol?
    func at() -> Double { Date().timeIntervalSince(started) }
    func position() -> [String: Double] { host.map { sample($0) } ?? [:] }
    let observer = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                          queue: .main) { note in
        if note.object as? String == name { notices += 1 }
    }
    // Synchronous observers timestamp each request and completion when posted.
    let requestObserver = NotificationCenter.default.addObserver(forName: .conversationRowScroll, object: nil,
                                                                 queue: nil) { note in
        guard note.object as? String == name else { return }
        MainActor.assumeIsolated { requests.append(["at": at(), "position": position()]) }
    }
    let completionObserver = NotificationCenter.default.addObserver(forName: .conversationRowScrollCompleted,
                                                                    object: nil, queue: nil) { note in
        guard note.object as? String == name else { return }
        MainActor.assumeIsolated { completions.append(["at": at(), "position": position()]) }
    }
    ConversationFollowTest.record = { cid, entry in
        guard cid == name else { return }
        trace.append(entry.merging(["at": at(), "position": position()]) { _, new in new })
    }
    defer {
        ConversationFollowTest.record = nil
        NotificationCenter.default.removeObserver(observer)
        NotificationCenter.default.removeObserver(requestObserver)
        NotificationCenter.default.removeObserver(completionObserver)
        if let boundsObserver { NotificationCenter.default.removeObserver(boundsObserver) }
        if let documentChanges { NotificationCenter.default.removeObserver(documentChanges) }
        if let earlyObserver { NotificationCenter.default.removeObserver(earlyObserver) }
    }
    let scene = await Scene(name, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            pageDelay: delay, pageSize: size, revealOnMount: "person:m000040",
                            onMount: { mounted in
        host = mounted
        if completionFirst {
            earlyObserver = completeBeforeScroll(name, started: started, host: { host }, gate: true) { early.append($0) }
        }
        let clip = timelineScroll(mounted)?.contentView
        clip?.postsBoundsChangedNotifications = true
        boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                object: clip, queue: nil) { _ in
            MainActor.assumeIsolated { geometry.append(["at": at(), "position": position()]) }
        }
        documentChanges = documentObserver(name, host: mounted, started: started) { documents.append($0) }
    })
    defer { scene.close() }
    let opened = sample(scene.host)
    let openedReadiness = scene.readiness()
    let openedAt = at()
    await scene.stream(20)
    return ["after_open": opened, "after_stream": sample(scene.host),
            "caught_up": openedReadiness["caught_up"] as? Bool ?? false,
            "reveal_consumed": !(openedReadiness["reveal_pending"] as? Bool ?? true), "notices": notices,
            "opened_at": openedAt, "readiness_at_open": openedReadiness,
            "readiness_at_fold": scene.foldedReadiness, "trace": trace, "geometry": geometry,
            "requests": requests, "completions": completions, "completion_first": completionFirst,
            "early_completions": early, "documents": documents]
}

// Round 7: CI run 40251214771 failed the completion-first opening another way.
// The reveal's scroll left the end before its row was presented; lazy
// measurement then shrank the document by 6,382 pt under the clip, which clamped
// to the new end. The marker appeared, and the request's own completion measured
// the clamped end: both released suspension, and catch-up's next settling jump
// undid the reveal. This replays that order on a fixed schedule, with catch-up
// held until its settling tick. A test inset above the rows stands in for the
// measurement: shrinking it moves the content up under a clip that stays put. The
// probe posts the request and its completions and applies the first-pass scroll
// itself, so no step waits on lazy-stack timing. Modes: `replay` (the CI order),
// `person` (the person scrolls to the end during the reveal) and `landed` (the
// real reveal lands first, so none is outstanding when the document shrinks).
//
// Round 8's continuation (CI run 40303045129, macOS 15): the shrink was a fixed
// 9,000 pt, and a 20 ms poll waited for the clamp. On CI's layout the clip ended
// 239 pt from the end (offset 8,411 of 9,204, viewport 554), and `shrink.clamped`
// waited out 20 s. At 1.728x text the same failure shows how: the shrink clamped
// the clip, and rows measured in the next pass grew the document 116 pt under it,
// inside one settle step, so neither the poll nor the marker saw the end. The
// shrink is now measured: in one main-actor turn the probe applies pending layout,
// reads the clip's distance from the end, checks the scene's preconditions and
// shrinks by that distance plus two viewports. It shrinks again, measured the same
// way, until the end stays in view.
@MainActor func shrinkReplay(_ root: URL, mode: String) async -> [String: Any] {
    let name = "shrink-" + mode
    let target = "person:m000040"
    // Room above the rows for any measured shrink, and so for any layout: the clip can
    // be as far from the end as the rows are tall, and a landed default run shrank
    // 16,761 pt in all.
    let inset: CGFloat = 40000
    let started = Date()
    func at() -> Double { Date().timeIntervalSince(started) }
    var trace: [[String: Any]] = []
    var geometry: [[String: Any]] = []
    var documents: [[String: Any]] = []
    var steps: [[String: Any]] = []
    var host: NSView?
    var boundsObserver: NSObjectProtocol?
    var documentChanges: NSObjectProtocol?
    func position() -> [String: Double] { host.map { sample($0) } ?? [:] }
    ConversationFollowTest.inset.top = inset
    ConversationFollowTest.record = { cid, entry in
        guard cid == name else { return }
        trace.append(entry.merging(["at": at(), "position": position()]) { _, new in new })
    }
    ProbeWaits.failureFile = root.appendingPathComponent("probe-failure.json")
    var shrinks: [[String: Any]] = []
    ProbeWaits.context = { ["mode": mode, "steps": steps, "trace": trace, "geometry": geometry, "documents": documents,
                            "shrinks": shrinks] }
    defer {
        ConversationFollowTest.record = nil
        ConversationFollowTest.inset.top = 0
        ProbeWaits.failureFile = nil
        ProbeWaits.context = nil
        if let boundsObserver { NotificationCenter.default.removeObserver(boundsObserver) }
        if let documentChanges { NotificationCenter.default.removeObserver(documentChanges) }
    }
    let scene = await Scene(name, root: root, prior: 120, priorText: { "person message \($0) asks something" },
                            tray: [], pageDelay: 0.25, pageSize: 40, pauseCatchup: true,
                            onMount: { mounted in
        host = mounted
        let clip = timelineScroll(mounted)?.contentView
        clip?.postsBoundsChangedNotifications = true
        boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                object: clip, queue: nil) { _ in
            MainActor.assumeIsolated { geometry.append(["at": at(), "position": position()]) }
        }
        documentChanges = documentObserver(name, host: mounted, started: started) { documents.append($0) }
    }, holdCatchup: true)
    defer { scene.close() }
    /// The view's reveal, from its own trace: outstanding while its last request
    /// (`reveal`) has not since landed, been ended by the person's scroll or been
    /// abandoned. The view traces each of these where it sets or clears `revealPending`.
    /// (The readiness `reveal_pending` is the search palette's reveal, which only
    /// `landed` sets: it is false throughout `replay`, as in CI's last state.)
    func reveal() -> [String: Any] {
        guard let index = trace.lastIndex(where: { $0["event"] as? String == "reveal" }) else {
            return ["outstanding": false, "ended_by": "no request"]
        }
        let ended = trace[(index + 1)...].first {
            ["reveal_landed", "person_scrolled", "reveal_abandoned"].contains($0["event"] as? String ?? "")
        }
        return ["outstanding": ended == nil, "requested_at": trace[index]["at"] ?? 0,
                "ended_by": ended?["event"] ?? "", "ended_at": ended?["at"] ?? -1]
    }
    func state() -> [String: Any] {
        scene.readiness().merging(["reveal": reveal(), "inset": Double(ConversationFollowTest.inset.top),
                                   "shrinks": shrinks.count]) { _, new in new }
    }
    /// The state the regression is about, as the shrink comes: in `replay` the reveal is
    /// still outstanding, in `landed` it has landed.
    func revealUnmet() -> String? {
        let now = reveal()
        let outstanding = now["outstanding"] as? Bool ?? false
        if mode == "replay" && !outstanding {
            return "the reveal is no longer outstanding (\(now["ended_by"] ?? "") at \(now["ended_at"] ?? -1) s)"
        }
        if mode == "landed" && (outstanding || now["ended_by"] as? String != "reveal_landed") {
            return "the reveal has not landed (\(now["ended_by"] ?? ""))"
        }
        return nil
    }
    /// One shrink, measured and made in this main-actor turn: pending layout applied,
    /// the clip's distance from the end read, the preconditions checked, and the inset
    /// reduced by that distance plus two viewports. The document's end then lies a
    /// viewport above the clip's top, so the clip must clamp to it; the second viewport
    /// absorbs rows re-measured in the same pass (CI's 239 pt, round 8's 438-462 pt).
    func shrinkUnderClip() {
        let content: NSView = scene.window.contentView ?? scene.host
        content.layoutSubtreeIfNeeded()
        let before = sample(scene.host)
        let fromEnd = CGFloat(before["from_end"] ?? 0), viewport = CGFloat(before["viewport"] ?? 0)
        let amount = (max(0, fromEnd) + 2 * viewport).rounded(.up)
        if let missing = revealUnmet() { readinessUnmet("shrink.clamped", missing, state: state()) }
        if shrinks.isEmpty && fromEnd <= 600 {
            readinessUnmet("shrink.clamped", "the clip is \(fromEnd) pt from the end before the shrink, not away from it",
                           state: state())
        }
        let room = ConversationFollowTest.inset.top
        if amount > room {
            readinessUnmet("shrink.clamped", "the shrink needs \(amount) pt and the inset holds \(room) pt", state: state())
        }
        ConversationFollowTest.inset.top = room - amount
        content.layoutSubtreeIfNeeded()
        shrinks.append(["at": at(), "before": before, "amount": Double(amount), "inset_before": Double(room),
                        "applied": sample(scene.host)])
    }
    func step(_ label: String, _ extra: [String: Any] = [:]) {
        steps.append(extra.merging(["step": label, "at": at(), "position": sample(scene.host),
                                    "readiness": scene.readiness(), "reveal": reveal()]) { _, new in new })
    }
    func clipToEndOffset(_ fromEnd: CGFloat) {
        guard let scroll = timelineScroll(scene.host), let document = scroll.documentView else { return }
        let clip = scroll.contentView
        clip.scroll(to: NSPoint(x: 0, y: max(0, document.frame.height - clip.bounds.height - fromEnd)))
        scroll.reflectScrolledClipView(clip)
    }
    let opened = await stablePosition(scene.host)
    step("opened", ["stable": opened.samples])
    let request = UUID()
    if mode == "landed" {
        // The real reveal, both passes, with catch-up still held. Rows measured after
        // both passes can move the row out of the clip before it is presented (1 of 5
        // runs at 560 pt, load 148), and this control is about a reveal that landed,
        // so reveal it again, up to three times, until it has.
        var reveals = 0
        var landed: (position: [String: Double], samples: Int)
        repeat {
            reveals += 1
            scene.palette.reveal = SearchReveal(conversationID: name, itemID: target)
            await waitReady("shrink.reveal_consumed", view: scene.host, state: state) {
                scene.palette.reveal == nil
            }
            landed = await stablePosition(scene.host)
        } while revealUnmet() != nil && reveals < 3
        step("landed", ["stable": landed.samples, "reveals": reveals])
    } else {
        // The request and its first completion, with the clip still at the old end
        // (CI 0.619 s), then the reveal's scroll as the lazy first pass presented it:
        // away from the end before the row was in the clip (CI 0.626 s). All three in
        // one turn, so the completion's deferred check reads the applied scroll.
        NotificationCenter.default.post(name: .conversationRowScroll, object: name,
                                        userInfo: ["request": request, "target": target])
        step("reveal")
        ConversationOpeningPin.didRevealRow(in: name, request: request)
        step("early_completion")
        clipToEndOffset(2000)
        await settle(scene.host, seconds: 0.3)
        // Rows measured at the new place can pull the end in under the clip (at 560 pt
        // and 1.44x text the document shrank 1,856 pt, leaving it 144 pt from the end
        // before the shrink), so put it back until it stays away from the end.
        var placements = 1
        while (sample(scene.host)["from_end"] ?? 0) <= 600 && placements < 8 {
            clipToEndOffset(2000)
            placements += 1
            await settle(scene.host, seconds: 0.3)
        }
        step("scrolled", ["placements": placements])
    }
    if mode == "person" {
        await scene.personScroll(toEnd: true)
        step("person_at_end")
    } else {
        // The document shrinks under the clip until it clamps to the end, and the
        // marker appears (CI 0.686-0.689 s). Rows measured after a shrink can move the
        // end away again, so shrink again, measured the same way, until it stays in view
        // (a landed default run clamped eight times, each undone by 293 down to 136 pt
        // of rows, before the end stayed). A lost precondition (the reveal ended, no
        // room, sixteen shrinks settled and the end still away) fails at once, not at 20 s.
        step("shrinking")
        var exhausted = false
        await waitReady("shrink.clamped", seconds: 20, view: scene.host, step: 0.3, state: state, unmet: {
            revealUnmet() ?? (exhausted ? "the end did not stay in view after \(shrinks.count) shrinks" : nil)
        }) {
            if !shrinks.isEmpty, (sample(scene.host)["from_end"] ?? 1e9) <= 1 { return true }
            // Each check comes after the last shrink has settled for one step.
            if shrinks.count >= 16 { exhausted = true; return false }
            shrinkUnderClip()
            return false
        }
        step("shrunk", ["shrinks": shrinks])
        if mode == "replay" {
            // The request's own completion, delivered at the clamped end (CI 0.694 s).
            ConversationOpeningPin.didRevealRow(in: name, request: request)
            await settle(scene.host, seconds: 0.3)
            step("late_completion")
        }
        // The document regrows, and the clip is far from the end again.
        ConversationFollowTest.inset.top = inset
        await waitReady("shrink.regrown", seconds: 20, view: scene.host, state: state) {
            (sample(scene.host)["from_end"] ?? 0) > 600
        }
        await settle(scene.host, seconds: 0.3)
        step("regrown")
    }
    // Catch-up's settling tick: its first page changes the follow key (CI 0.909 s).
    let tickFrom = trace.count
    step("tick")
    scene.daemon.resumeCatchup()
    await waitReady("shrink.settling_tick", seconds: 60, view: scene.host, state: state) {
        trace[tickFrom...].contains { $0["event"] as? String == "move" && $0["settling"] as? Bool == true }
    }
    await settle(scene.host, seconds: 1.0)
    step("after_tick")
    await scene.waitFolded(scene.daemon.lastSeq, "shrink.caught_up")
    await settle(scene.host, seconds: 0.5)
    step("caught_up")
    scene.close()
    return ["mode": mode, "target": target, "inset": Double(inset), "steps": steps, "trace": trace,
            "layout": ProbeLayout.described, "shrinks": shrinks,
            "geometry": geometry, "documents": documents, "before_catchup": scene.beforeCatchup,
            "held_before_reveal": scene.heldBeforeReveal, "readiness_waits": ProbeWaits.log,
            "visible_windows": NSApp.windows.filter(\.isVisible).count]
}

// Review r3's settling replay: reveal while catch-up is held, then resume the
// actual paginated model with only marker-disappearance publication delayed.
@MainActor func settlingReveal(_ root: URL, mode: String) async -> [String: Any] {
    let hooked = mode == "stale"
    let early = mode == "near-early"
    let completionFirst = mode == "near-completion-first"
    let reveal: String? = mode == "plain" ? nil
        : mode == "near" || early || completionFirst ? "person:live" : "person:m000040"
    let started = Date()
    var timeline: [[String: Any]] = []
    var earlyCompletions: [[String: Any]] = []
    var earlyObserver: NSObjectProtocol?
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
        if let earlyObserver { NotificationCenter.default.removeObserver(earlyObserver) }
    }
    var mountedHost: NSView?
    let scene = await Scene(mode, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            pageDelay: early ? 0.5 : 0.1, pageSize: 20, revealOnMount: reveal, pauseCatchup: true,
                            onMount: { mounted in
        mountedHost = mounted
        if completionFirst {
            earlyObserver = completeBeforeScroll(mode, started: started, host: { mountedHost }) {
                earlyCompletions.append($0)
            }
        }
    }, silentFoldDeadline: early ? 2 : nil)
    defer { scene.close() }
    // Readiness flags are sampled with the position they describe (review r5:
    // a later caught_up could not show whether after_open read a ready scene).
    let opened = sample(scene.host)
    let openedReadiness = scene.readiness()
    let openedAt = Date().timeIntervalSince(started)
    let watch = PositionWatch(scene.host, baseline: opened)
    await scene.stream(20)
    let afterStream = sample(scene.host)
    // Observe beyond the delayed marker publication as well as later output.
    await settle(scene.host, seconds: 3.2)
    watch.stop()
    let horizon = sample(scene.host)
    // The checkpoint replay also waits out catch-up, to show where the view
    // settles once the scene is actually ready.
    var whenReady: [String: Any] = [:]
    if early {
        await scene.waitFolded(scene.seq, "near_early.caught_up")
        await settle(scene.host, seconds: 1.2)
        whenReady = ["position": sample(scene.host), "readiness": scene.readiness(),
                     "at": Date().timeIntervalSince(started)]
    }
    scene.close()
    return ["mode": mode, "hooked": hooked, "held_before_reveal": scene.heldBeforeReveal, "when_ready": whenReady,
            "before_catchup": scene.beforeCatchup, "after_initial_reveal": scene.afterInitialReveal,
            "after_open": opened, "after_stream": afterStream, "horizon": horizon,
            "max_drift": watch.maxDrift, "caught_up": openedReadiness["caught_up"] as? Bool ?? false,
            "reveal_consumed": !(openedReadiness["reveal_pending"] as? Bool ?? true),
            "opened_at": openedAt, "readiness_at_open": openedReadiness,
            "readiness_at_fold": scene.foldedReadiness, "caught_up_at_end": scene.caughtUp,
            "readiness_waits": ProbeWaits.log, "timeline": timeline, "early_completions": earlyCompletions,
            "visible_windows": NSApp.windows.filter(\.isVisible).count]
}

// Gate a remote turn's arrival on the second reveal's recorded suspension,
// rather than hoping suspension remains active after an arbitrary sleep.
@MainActor func suspendedLiveTurn(_ root: URL) async -> [String: Any] {
    let name = "suspended-live-turn"
    let scene = await Scene(name, root: root, prior: 30,
        priorText: { "person message \($0) asks something" }, tray: [], immediateDelivery: true)
    await scene.stream(30)
    let started = Date()
    var timeline: [[String: Any]] = []
    var reveals = 0
    var injected = false
    var injectionSuspended = false
    ConversationFollowTest.disappearanceDelay = 3
    ConversationFollowTest.record = { cid, entry in
        guard cid == name else { return }
        let live = scene.model.state.timelines[name]?.liveMessageID ?? ""
        timeline.append(entry.merging(["at": Date().timeIntervalSince(started),
            "live_message": live, "position": sample(scene.host)]) { _, new in new })
        if entry["event"] as? String == "reveal" {
            reveals += 1
            if reveals == 2 && !injected {
                injected = true
                injectionSuspended = entry["suspended"] as? Bool == true
                // A provider/event-only turn, not the person's own-send exception.
                let page = [event(scene.seq + 1, "live", "turn.completed", ["state": "complete"]),
                    event(scene.seq + 2, "next-live", "accepted", [:]),
                    event(scene.seq + 3, "next-live", "text.delta", ["block": "next", "text": "new live turn\n"])]
                scene.seq += page.count
                scene.daemon.push(page)
            }
        }
    }
    defer {
        ConversationFollowTest.record = nil
        ConversationFollowTest.disappearanceDelay = 0
        scene.close()
    }
    scene.palette.reveal = SearchReveal(conversationID: name, itemID: "person:m000010")
    await waitReady("live_turn.reveal_consumed", view: scene.host, state: { scene.readiness() }) {
        scene.palette.reveal == nil
    }
    await scene.waitFolded(scene.seq)
    let landed = await stablePosition(scene.host)
    let landedReadiness = scene.readiness()
    let watch = PositionWatch(scene.host, baseline: landed.position)
    await scene.push { seq in
        seq += 1
        return [event(seq, "next-live", "text.delta", ["block": "next", "text": "later new-turn output\n"])]
    }
    await settle(scene.host, seconds: 3.2)
    watch.stop()
    let after = sample(scene.host)
    let live = scene.model.state.timelines[name]?.liveMessageID ?? ""
    scene.close()
    return ["injected": injected, "injection_suspended": injectionSuspended,
        "reveals": reveals, "reveal_consumed": !(landedReadiness["reveal_pending"] as? Bool ?? true),
        "caught_up": landedReadiness["caught_up"] as? Bool ?? false, "readiness_at_landing": landedReadiness,
        "live_message": live,
        "landed": landed.position, "landed_stable_samples": landed.samples,
        "after_stream": after, "max_drift": watch.maxDrift, "timeline": timeline,
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
    if only == "open_reveal_completion_first" {
        out["open_reveal_completion_first"] = await farOpening("open_reveal_completion_first", root: root,
                                                               delay: 0.25, size: 40, completionFirst: true)
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
    private var consumed = false
    private var expired = false
    private var sequences: [Int] = []
    func recordConsumption() { lock.lock(); consumed = true; lock.unlock() }
    var isConsumed: Bool { lock.lock(); defer { lock.unlock() }; return consumed }
    func deadlineExpired() { lock.lock(); expired = true; lock.unlock() }
    func push(_ events: [[String: Any]], to daemon: R4ViewDaemon, at: Double) {
        lock.lock()
        times += Array(repeating: at, count: events.count)
        sequences += events.map { $0["seq"] as! Int }
        pages += 1
        daemon.push(events)
        lock.unlock()
    }
    var snapshot: (times: [Double], pages: Int, sequences: [Int], expired: Bool) {
        lock.lock(); defer { lock.unlock() }
        return (times, pages, sequences, expired)
    }
}

@MainActor func streamingReveal(_ name: String, root: URL, kind: String,
                                delay: Double = 0, interval: Double = 0,
                                boundary: String? = nil, staleMarker: Bool = false,
                                consumptionPause: Double = 0, firstNoticePause: Double = 0,
                                replay: Bool = false) async -> [String: Any] {
    let word = "zebraneedle"
    let scene = await Scene(name, root: root, prior: 30,
                            priorText: { $0 == 10 ? "person message \($0) \(word) here" : "person message \($0) asks something" },
                            tray: [], immediateDelivery: true)
    await scene.stream(30)
    // Exact counts for early/boundary scenes; minimum counts for ongoing streams.
    // The replay is review r5's surviving overlap-loss schedule as fixed counts:
    // 31 events before the first notice, 15 between the notices, 4 after consumption.
    let replayBefore = 31, replayBetween = 15, replayAfter = 4
    let firstSequence = scene.seq + 1
    let count = replay ? replayBefore + replayBetween + replayAfter
        : boundary != nil ? 10 : interval > 0 ? Int((2.5 / interval).rounded(.up)) : 1
    var events: [[String: Any]] = []
    for index in 1...count {
        scene.seq += 1
        // The survivor's later output moved it from 177 to 627 pt from the end
        // before landing was captured; the replay's last four chunks grow as much.
        let text = replay && index > replayBefore + replayBetween
            ? (0..<8).map { "overlap chunk \(index) line \($0)" }.joined(separator: "\n") + "\n"
            : "overlap chunk \(index)\n"
        events.append(event(scene.seq, "live", kind == "text" ? "text.delta" : "tool.started",
                            kind == "text" ? ["block": "live", "text": text]
                            : ["id": "overlap\(index)", "name": "Read", "summary": "file \(index)"]))
    }
    let arrivals = RevealArrivals()
    var folds: [Int: Double] = [:]
    var notices: [Double] = []
    var starts = 0
    var consumed = -1.0
    var boundaryArrival = -1.0
    var started = Date()
    var producer: Task<Void, Never>?
    var timeline: [[String: Any]] = []
    // Every clip-bounds change, delivered synchronously as it happens, and each
    // reveal request's completion. Together with the passive samples below they
    // cover each presented geometry from the first request to landing.
    var geometry: [[String: Any]] = []
    var requests: [String] = []
    var requestedAt: [Double] = []
    var completions: [[String: Any]] = []
    var awaitingPresentation = false
    var injectedJump: [String: Any] = [:]
    var replayStarted = false
    ConversationFollowTest.disappearanceDelay = staleMarker ? 3 : 0
    ConversationFollowTest.record = { cid, entry in
        guard cid == name else { return }
        timeline.append(entry.merging(["at": Date().timeIntervalSince(started), "position": sample(scene.host)]) { _, new in new })
    }
    var previousPosition: [String: Double] = [:]
    let positionTimer = Timer(timeInterval: 0.025, repeats: true) { _ in
        MainActor.assumeIsolated {
            let position = sample(scene.host)
            // The first display tick after a reveal request is recorded even if
            // the request left the geometry unchanged.
            if position != previousPosition || awaitingPresentation {
                timeline.append(["event": awaitingPresentation ? "presented" : "position",
                                 "at": Date().timeIntervalSince(started), "position": position,
                                 "request_index": requests.count])
                previousPosition = position
                awaitingPresentation = false
            }
        }
    }
    RunLoop.main.add(positionTimer, forMode: .common)
    let clip = timelineScroll(scene.host)?.contentView
    clip?.postsBoundsChangedNotifications = true
    let boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                object: clip, queue: nil) { _ in
        MainActor.assumeIsolated {
            geometry.append(["at": Date().timeIntervalSince(started), "position": sample(scene.host),
                             "request_index": requests.count])
        }
    }
    let completionObserver = NotificationCenter.default.addObserver(forName: .conversationRowScrollCompleted,
                                                                    object: nil, queue: .main) { note in
        guard note.object as? String == name else { return }
        completions.append(["at": Date().timeIntervalSince(started), "position": sample(scene.host),
                            "request": (note.userInfo?["request"] as? UUID)?.uuidString ?? ""])
    }
    // The .main observers above may run a turn after the post. This one runs
    // as the reveal posts its request, before that request's scroll.
    let requestObserver = NotificationCenter.default.addObserver(forName: .conversationRowScroll,
                                                                 object: nil, queue: nil) { note in
        guard note.object as? String == name else { return }
        MainActor.assumeIsolated { requestedAt.append(Date().timeIntervalSince(started)) }
    }
    let folded = scene.model.$state.sink { state in
        let cursor = state.timelines[name]?.cursor ?? 0
        if cursor >= firstSequence {
            for seq in firstSequence...cursor where folds[seq] == nil {
                folds[seq] = Date().timeIntervalSince(started)
            }
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
        requests.append((note.userInfo?["request"] as? UUID)?.uuidString ?? "")
        awaitingPresentation = true
        if (boundary == "first_notice" && notices.count == 1)
            || (boundary == "second_notice" && notices.count == 2) { burst(at: at) }
        if replay && notices.count == 1 {
            for next in events[replayBefore..<(replayBefore + replayBetween)] {
                arrivals.push([next], to: scene.daemon, at: Date().timeIntervalSince(started))
            }
        }
        // Model a slow presentation turn after the second notice. The detached
        // producer keeps its cadence throughout this pause and after consumption.
        if notices.count == 2 && consumptionPause > 0 { Thread.sleep(forTimeInterval: consumptionPause) }
    }
    let startObserver = NotificationCenter.default.addObserver(forName: .init("subfleet.test.searchRevealStarted"),
                                                               object: nil, queue: .main) { note in
        guard note.object as? String == name else { return }
        starts += 1
        if replay {
            guard !replayStarted else { return }
            replayStarted = true
            for next in events[0..<replayBefore] {
                arrivals.push([next], to: scene.daemon, at: Date().timeIntervalSince(started))
            }
            return
        }
        guard boundary == nil && producer == nil else { return }
        let daemon = scene.daemon
        let epoch = started
        let page = events
        producer = Task.detached {
            // Each arrival has an absolute cadence deadline. Neither notice nor
            // consumption releases an event: consumption only bounds termination.
            // Keep the original minimum and at least three independent ticks after
            // consumption. A missing consumption expires loudly after 60 seconds.
            var index = 0
            var afterConsumption = 0
            while index < page.count || (interval > 0 && afterConsumption < 3) {
                let deadline = epoch.addingTimeInterval(delay + Double(index) * interval)
                let wait = deadline.timeIntervalSinceNow
                if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1e9)) }
                if Task.isCancelled { return }
                if Date().timeIntervalSince(epoch) >= 60 { arrivals.deadlineExpired(); return }
                let next: [String: Any]
                if index < page.count { next = page[index] }
                else {
                    let seq = firstSequence + index
                    next = event(seq, "live", kind == "text" ? "text.delta" : "tool.started",
                        kind == "text" ? ["block": "live", "text": "overlap chunk \(index + 1)\n"]
                        : ["id": "overlap\(index + 1)", "name": "Read", "summary": "file \(index + 1)"])
                }
                let consumedBeforeTick = arrivals.isConsumed
                arrivals.push([next], to: daemon, at: Date().timeIntervalSince(epoch))
                if consumedBeforeTick { afterConsumption += 1 }
                index += 1
            }
        }
    }
    let consumption = scene.palette.$reveal.dropFirst().sink { value in
        if value == nil && consumed < 0 {
            consumed = Date().timeIntervalSince(started)
            arrivals.recordConsumption()
            if boundary == "consumption" { burst(at: consumed) }
            if replay {
                // The survivor's loss, injected by the probe after consumption:
                // a jump from the landed reveal to 177 pt short of the end (r5:
                // offset 2,928 -> 10,702), then its four later arrivals.
                DispatchQueue.main.async {
                    if let scroll = timelineScroll(scene.host), let document = scroll.documentView {
                        let clip = scroll.contentView
                        let before = sample(scene.host)
                        let end = max(0, document.frame.height - clip.bounds.height)
                        clip.scroll(to: NSPoint(x: 0, y: document.isFlipped ? max(0, end - 177) : 177))
                        scroll.reflectScrolledClipView(clip)
                        injectedJump = ["at": Date().timeIntervalSince(started), "from": before,
                                        "to": sample(scene.host)]
                    }
                    for next in events[(replayBefore + replayBetween)...] {
                        arrivals.push([next], to: scene.daemon, at: Date().timeIntervalSince(started))
                    }
                }
            }
        }
    }
    defer {
        producer?.cancel()
        positionTimer.invalidate()
        ConversationFollowTest.record = nil
        ConversationFollowTest.disappearanceDelay = 0
        folded.cancel()
        consumption.cancel()
        NotificationCenter.default.removeObserver(observer)
        NotificationCenter.default.removeObserver(startObserver)
        NotificationCenter.default.removeObserver(boundsObserver)
        NotificationCenter.default.removeObserver(completionObserver)
        NotificationCenter.default.removeObserver(requestObserver)
        scene.close()
    }
    let results = SearchIndex(scene.model.state.searchCandidates()).search(word)?.results ?? []
    let match = results.first { $0.snippet?.itemID == "person:m000010" }
    started = Date()
    if let match { scene.palette.activate(match) }
    let deadline = Date().addingTimeInterval(readinessDeadline)
    if interval == 0 && boundary == nil && !replay {
        // The early-input scenes need UIModel's two main-queue round trips to
        // fold the page before the first notice. Repeated forced AppKit layout
        // here can monopolize that queue for longer than the reveal's sleep.
        // Pump delivery without forcing presentation until the input folds;
        // the real reveal task and both of its timers continue running.
        while folds.count < count {
            if Date() >= deadline {
                readinessExpired("streaming.early_input_folded", seconds: readinessDeadline,
                                 state: scene.readiness().merging(["folds": folds.count, "count": count]) { _, new in new })
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.001))
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
    }
    await waitReady("streaming.reveal_consumed", view: scene.host, state: { scene.readiness() }) {
        scene.palette.reveal == nil
    }
    // The survivor's later output folded before its landing was captured.
    if replay {
        await waitReady("streaming.replay_folded", view: scene.host,
                        state: { scene.readiness().merging(["folds": folds.count, "count": count]) { _, new in new } }) {
            folds.count >= count
        }
    }
    // Do not require distance inside the wait: a bad landing must be reported
    // promptly and rejected by Python, rather than hidden behind a timeout.
    let landed = await stablePosition(scene.host)
    let landedAt = Date().timeIntervalSince(started)
    let landedReadiness = scene.readiness()
    let watch = PositionWatch(scene.host, baseline: landed.position)
    defer { watch.stop() }
    await producer?.value
    let delivered = arrivals.snapshot
    scene.seq = delivered.sequences.last ?? scene.seq
    if match != nil { await scene.waitFolded(scene.seq) }
    let quiet = await stablePosition(scene.host)
    let lastArrival = Date().timeIntervalSince(started)
    await scene.stream(20)
    let afterMore = await stablePosition(scene.host)
    let horizon = started.addingTimeInterval(max(6.3, lastArrival + 2.5))
    while Date() < horizon { await settle(scene.host, seconds: 0.02) }
    let final = sample(scene.host)
    return ["found": match != nil, "caught_up": scene.caughtUp, "reveal_consumed": scene.palette.reveal == nil,
            "notices_at": notices, "reveal_starts": starts, "consumed_at": consumed,
            "stale_marker": staleMarker, "consumption_pause": consumptionPause,
            "first_notice_pause": firstNoticePause, "timeline": timeline,
            "pushed": delivered.times.count, "arrival_pages": delivered.pages,
            "producer_deadline_exceeded": delivered.expired,
            "arrival_sequences": delivered.sequences, "arrivals_at": delivered.times,
            "folds_at": delivered.sequences.compactMap { folds[$0] },
            "boundary_arrival_at": boundaryArrival,
            "folded_seq": scene.model.state.timelines[name]?.cursor ?? 0, "target_seq": scene.seq,
            "landed": landed.position, "landed_stable_samples": landed.samples, "landed_at": landedAt,
            "geometry": geometry, "requests": requests, "requested_at": requestedAt, "completions": completions,
            "readiness_at_landing": landedReadiness,
            "replay": replay, "injected_jump": injectedJump,
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
        if kind == "text" && only == "replayed_jump" {
            out["replayed_jump"] = await streamingReveal("replayed_jump", root: root, kind: kind, replay: true)
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

// MARK: - Round 8: three ways the view stayed behind the end (review r6 of round 7)

/// One round-8 scene's record: the view's follow trace, the clip's geometry and
/// named steps, each with its position, readiness and whether the model holds the
/// reveal's target.
@MainActor final class Fix12Record {
    let name: String
    let started = Date()
    var trace: [[String: Any]] = []
    var geometry: [[String: Any]] = []
    var steps: [[String: Any]] = []
    // Armed after the setup diagnostic injects its input. Count at the write
    // itself, independently of the barrier's corrected-gap trace.
    var fixtureClipWrites: [[String: Double]]?
    var scene: Scene?
    var host: NSView?
    private var boundsObserver: NSObjectProtocol?

    init(_ name: String) {
        self.name = name
        ConversationFollowTest.record = { [weak self] cid, entry in
            guard let self, cid == self.name else { return }
            self.trace.append(entry.merging(["at": self.at(), "position": self.position()]) { _, new in new })
        }
    }

    func at() -> Double { Date().timeIntervalSince(started) }
    func position() -> [String: Double] { host.map { sample($0) } ?? [:] }

    func mounted(_ view: NSView) {
        host = view
        let clip = timelineScroll(view)?.contentView
        clip?.postsBoundsChangedNotifications = true
        boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                object: clip, queue: nil) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.geometry.append(["at": self.at(), "position": self.position()])
            }
        }
    }

    func step(_ label: String, target: String? = nil, _ extra: [String: Any] = [:]) {
        var entry = extra.merging(["step": label, "at": at(), "position": position()]) { _, new in new }
        if let scene {
            entry["readiness"] = scene.readiness()
            let timeline = scene.model.state.timelines[name]
            entry["resets"] = timeline?.resets ?? -1
            let items = scene.model.state.layout(conversationID: name)?.items ?? []
            if let target { entry["target_in_model"] = items.contains { $0.id == target } }
            entry["last_row"] = items.last?.id ?? ""
            entry["own_send_last"] = items.last.flatMap { last in
                last.messageID.map { timeline?.turn($0)?.locallySent == true } } ?? false
        }
        steps.append(entry)
    }

    func clipTo(offset: CGFloat? = nil, fromEnd: CGFloat? = nil) {
        guard let host, let scroll = timelineScroll(host), let document = scroll.documentView else { return }
        let clip = scroll.contentView
        let y = offset ?? max(0, document.frame.height - clip.bounds.height - (fromEnd ?? 0))
        fixtureClipWrites?.append(["at": at(), "gap": position()["from_end"] ?? 1e9])
        clip.scroll(to: NSPoint(x: 0, y: y))
        scroll.reflectScrolledClipView(clip)
    }

    func finish(_ extra: [String: Any] = [:]) -> [String: Any] {
        ConversationFollowTest.record = nil
        if let boundsObserver { NotificationCenter.default.removeObserver(boundsObserver) }
        scene?.close()
        return extra.merging(["steps": steps, "trace": trace, "geometry": geometry,
                              "fixture_clip_writes": fixtureClipWrites ?? [],
                              "readiness_waits": ProbeWaits.log,
                              "visible_windows": NSApp.windows.filter(\.isVisible).count]) { _, new in new }
    }
}

/// A reveal request the probe posts itself, as a requester does: the request, the
/// scroll's first pass (if any) and the completion, all in one turn.
@MainActor func postReveal(_ record: Fix12Record, target: String, offset: CGFloat? = nil, fromEnd: CGFloat? = nil) -> UUID {
    let request = UUID()
    NotificationCenter.default.post(name: .conversationRowScroll, object: record.name,
                                    userInfo: ["request": request, "target": target])
    record.step("reveal", target: target)
    if offset != nil || fromEnd != nil { record.clipTo(offset: offset, fromEnd: fromEnd) }
    ConversationOpeningPin.didRevealRow(in: record.name, request: request)
    return request
}

// P2-1, the review's input: a pending reveal to `text:m000040:b` (its scroll had
// left the end before the row was in the clip), then an event-log reset that took
// that row out of the model. Catch-up is held across the reset, so the row stays
// out until the re-read. The clip clamps to the shrunk end, and the next reply
// must be followed. `away` is the control: the clip sits 3,000 pt down a
// 9,000 pt test inset, so the end stays out of view after the reset; suspension
// must hold until the person scrolls to the end.
@MainActor func removedTarget(_ root: URL, away: Bool) async -> [String: Any] {
    let record = Fix12Record(away ? "removed-away" : "removed")
    let target = "text:m000040:b"
    if away { ConversationFollowTest.inset.top = 9000 }
    defer { ConversationFollowTest.inset.top = 0 }
    let scene = await Scene(record.name, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            onMount: { record.mounted($0) })
    record.scene = scene
    _ = await stablePosition(scene.host)
    record.step("opened", target: target)
    scene.daemon.pauseCatchup()
    _ = away ? postReveal(record, target: target, offset: 3000) : postReveal(record, target: target, fromEnd: 2000)
    await settle(scene.host, seconds: 0.5)
    // Gate: lazy measurement can shrink the document under the clip and clamp it to
    // the end meanwhile (round 7). Put the clip back where the reveal's first pass
    // left it, and take the step and the reset's request in that one turn.
    away ? record.clipTo(offset: 3000) : record.clipTo(fromEnd: 2000)
    record.step("pending", target: target)
    scene.daemon.resetOnce()
    await waitReady("removed.reset", seconds: 30, view: scene.host, state: { scene.readiness() }) {
        (scene.model.state.timelines[record.name]?.resets ?? 0) >= 1 && scene.daemon.catchupPending
    }
    await settle(scene.host, seconds: 1.0)
    record.step("reset", target: target)
    scene.daemon.resumeCatchup()
    await scene.waitFolded(scene.daemon.lastSeq, "removed.caught_up")
    await settle(scene.host, seconds: 1.0)
    record.step("caught_up", target: target)
    if away {
        await scene.stream(20)
        record.step("reply_held", target: target)
        record.step("person_scrolling", target: target)
        await scene.personScroll(toEnd: true)
        record.step("person_at_end", target: target)
    }
    await scene.stream(20)
    record.step("reply", target: target)
    return record.finish(["target": target, "away": away])
}

// P2-1's second way: a request whose target the model does not hold (the search
// revealer's second pass after its row went, or any stale id). Nothing presents
// such a row, so it must be abandoned at once. With `away`, the person has scrolled
// 2,000 pt up when the request comes, so nothing releases then; the end is then
// brought into view with no person input (as a shrink or SwiftUI's own scroll
// does), and the marker's appearance must release suspension.
@MainActor func missingTarget(_ root: URL, away: Bool) async -> [String: Any] {
    let record = Fix12Record(away ? "missing-away" : "missing")
    let target = "text:gone:b"
    let scene = await Scene(record.name, root: root, prior: 30,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            onMount: { record.mounted($0) })
    record.scene = scene
    _ = await stablePosition(scene.host)
    record.step("opened", target: target)
    if away, let scroll = timelineScroll(scene.host) {
        // The person reads 2,000 pt up (which stops the opening pin); wait until lazy
        // measurement there has settled and the marker has gone.
        NotificationCenter.default.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
        record.clipTo(fromEnd: 2000)
        NotificationCenter.default.post(name: NSScrollView.didEndLiveScrollNotification, object: scroll)
        _ = await stablePosition(scene.host, awayFromEnd: true)
        await waitReady("missing.marker_gone", seconds: 20, view: scene.host, state: { scene.readiness() }) {
            record.trace.last { $0["at_bottom"] != nil }?["at_bottom"] as? Bool == false
        }
        record.clipTo(fromEnd: 2000)
    }
    _ = postReveal(record, target: target)
    await settle(scene.host, seconds: 0.5)
    record.step("requested", target: target)
    if away {
        // Rows measured on the way move the end, so keep moving until it stays in view.
        record.step("showing", target: target)
        for _ in 0..<12 {
            record.clipTo(fromEnd: 0)
            await settle(scene.host, seconds: 0.25)
            if (sample(scene.host)["from_end"] ?? 1e9) <= 1 { break }
        }
        await settle(scene.host, seconds: 0.3)
        record.step("end_shown", target: target)
    }
    await scene.stream(20)
    record.step("reply", target: target)
    return record.finish(["target": target, "away": away])
}

/// How far above the new end `stale-glide` lands the glide's last frame: the old end, the
/// send's 30 paragraphs up (in 40 local runs the document grew 1,413-1,954 pt across the send).
let staleGlideFromEnd: CGFloat = 1_680

// P2-2: the person's own send (`UIModel.send`) during a pending reveal, with
// catch-up held (`catchup`) or after it (`ordinary`, the control). The live
// message has ended, so the send is the timeline's last row, not a tray row.
//
// Review r7's landings send a 30-paragraph message: `short` (the glide scrolls to
// the old end), `regrow` (the rows above the clip grow 600 pt at the marker's first
// appearance) and `person` (the person then scrolls 1,500 pt up). Review r8's
// `quiet-regrow` lets `regrow` land: after `own_send_landed`, establish an exact-zero
// end through a quiet frame, then another 50 ms settle before the rows above the
// clip grow 600 pt again before the reply. `-late` grows them on a main-queue timer
// 350 ms after the landing callback instead.
// A `-natural` scene removes both animation controls. Round 10's `stale-glide` lands
// the glide's last frame on its stale target inside the landing check's quiet window.
@MainActor func ownSendDuringReveal(_ root: URL, catchup: Bool, landing: String? = nil) async -> [String: Any] {
    let record = Fix12Record(landing ?? (catchup ? "send-catchup" : "send-ordinary"))
    let natural = landing?.hasSuffix("-natural") == true
    let quiet = landing?.hasPrefix("quiet-") == true
    ProbePresentation.natural = natural
    defer {
        ProbePresentation.afterAnimation = nil
        ProbePresentation.natural = false
        ConversationFollowTest.inset.top = 0
    }
    let target = "person:m000040"
    let scene = await Scene(record.name, root: root, prior: 120,
                            priorText: { "person message \($0) asks something" }, tray: [],
                            pageDelay: catchup ? 0.25 : 0, pageSize: catchup ? 40 : 200, pauseCatchup: catchup,
                            onMount: { record.mounted($0) }, holdCatchup: catchup, liveState: "complete")
    record.scene = scene
    _ = await stablePosition(scene.host)
    record.step("opened", target: target)
    _ = postReveal(record, target: target, fromEnd: 2000)
    await settle(scene.host, seconds: 0.5)
    // Gate, as in `removedTarget`: the clip where the first pass left it, the step and
    // the send in one turn.
    record.clipTo(fromEnd: 2000)
    record.step("pending", target: target)
    if landing == "short" || landing == "person" {
        let oldEnd = (record.position()["content"] ?? 0) - (record.position()["viewport"] ?? 0)
        ProbePresentation.afterAnimation = { record.clipTo(offset: oldEnd) }
    }
    var staleGlide: QuietMove?
    if landing == "stale-glide", let scroll = timelineScroll(scene.host) {
        // The glide's last frame lands on its stale target, the old end: 150 ms (the
        // glide's length) after the clip came to rest at the new end, inside the landing
        // check's 300 ms quiet window. Place the old end from the new end, not at its offset
        // before the send: the lazy stack re-estimates the rows above as the send lands. On
        // CI the document lost 2,777-3,499 pt across the send, which put that offset past
        // the new end, so the frame never took the clip above it (runs 40812690386,
        // 40813021187, 40813080519).
        staleGlide = QuietMove(scroll: scroll, after: 0.15) {
            record.step("stale_glide", target: target)
            record.clipTo(fromEnd: staleGlideFromEnd)
        }
    }
    defer { staleGlide?.stop() }
    let sentAt = record.at()
    /// The send's first measured landing.
    func landed() -> [String: Any]? {
        record.trace.first { $0["event"] as? String == "own_send_landed" && ($0["at"] as? Double ?? 0) > sentAt }
    }
    let text = landing == nil ? "my own reply" : (1...30).map {
        "Paragraph \($0): my own message takes more than one viewport while the lazy rows are measured."
    }.joined(separator: "\n\n")
    scene.model.send(conversationID: record.name, text: text, staged: [],
                     settings: ConversationSettings(model: "claude-opus-5-5"))
    if landing?.contains("regrow") == true {
        // Grow above the clip on the marker's first appearance, before a settled
        // end can be measured. A lazy marker can appear over 100 pt from the end.
        await waitReady("send.marker_appeared", view: scene.host, state: { scene.readiness() }) {
            record.trace.contains { $0["event"] as? String == "marker_appeared"
                && ($0["at"] as? Double ?? 0) > (record.steps.last?["at"] as? Double ?? 0) }
        }
        record.step("growing", target: target)
        ConversationFollowTest.inset.top += 600
    }
    if landing == "stale-glide" {
        await waitReady("send.stale_landed", view: scene.host, state: { scene.readiness() }) {
            landed() != nil && record.steps.contains { $0["step"] as? String == "stale_glide" }
        }
        await settle(scene.host, seconds: 0.5)
        record.step("rested", target: target)
    }
    if quiet {
        await waitReady("send.landed", view: scene.host, state: { scene.readiness() }) { landed() != nil }
        let landedAt = landed()?["at"] as? Double ?? 0
        guard let scroll = timelineScroll(scene.host) else {
            readinessUnmet("send.zero_end", "timeline scroll missing", state: scene.readiness())
        }
        // The product may validly call back at column + 1 (CI measured 24 pt).
        // This scene needs zero before its delayed growth. Drive that setup explicitly,
        // without a person-scroll notification or either animation control, and wait
        // for 50 ms without a document/clip change at zero. Leave the original callback
        // in the trace: its tolerance and timing are independent of this scene's barrier.
        // Match scheduleLandingCheck() in ConversationOpeningPin.swift: a valid
        // landing is within Theme.space.column + 1. Larger gaps belong to the
        // product to recover within waitReady's existing budget, never the fixture.
        let correctionTolerance = Double(Theme.space.column + 1)
        var zeroEndFirstGap: Double?
        var zeroEndCorrectedGaps: [Double] = []
        let zeroEnd = QuietMove(scroll: scroll, after: 0.05, within: 0.5) {
            record.step("landed", target: target, ["landed": landed() ?? [:],
                "zero_end_first_gap": zeroEndFirstGap ?? 1e9,
                "zero_end_corrected_gaps": zeroEndCorrectedGaps])
        }
        defer { zeroEnd.stop() }
        await waitReady("send.zero_end", view: scene.host, state: {
            scene.readiness().merging([
                "zero_end_first_gap": zeroEndFirstGap ?? 1e9,
                "zero_end_gap": record.position()["from_end"] ?? 1e9,
                "zero_end_corrected_gaps": zeroEndCorrectedGaps,
                "zero_end_correction_tolerance": correctionTolerance
            ]) { _, new in new }
        }) {
            let gap = record.position()["from_end"] ?? 1e9
            if zeroEndFirstGap == nil {
                zeroEndFirstGap = gap
                record.trace.append(["event": "zero_end_first_look", "at": record.at(),
                                     "gap": gap, "position": record.position()])
            }
            if record.steps.last?["step"] as? String == "landed" { return true }
            if abs(gap) > 0.5 && abs(gap) <= correctionTolerance {
                zeroEndCorrectedGaps.append(gap)
                record.trace.append(["event": "zero_end_correction", "at": record.at(),
                                     "gap": gap, "position": record.position()])
                record.clipTo(fromEnd: 0)
            }
            return false
        }
        if landing == "quiet-regrow-late" {
            DispatchQueue.main.asyncAfter(deadline: .now() + max(0, landedAt + 0.35 - record.at())) {
                MainActor.assumeIsolated {
                    record.step("regrowing", target: target, ["after_landing": record.at() - landedAt])
                    ConversationFollowTest.inset.top += 600
                }
            }
            await waitReady("send.regrown_late", view: scene.host, state: { scene.readiness() }) {
                record.steps.last?["step"] as? String == "regrowing"
            }
        } else {
            await settle(scene.host, seconds: 0.05)
            record.step("regrowing", target: target, ["after_landing": record.at() - landedAt])
            ConversationFollowTest.inset.top += 600
        }
    }
    await settle(scene.host, seconds: 1.0)
    record.step("sent", target: target)
    ProbePresentation.afterAnimation = nil
    if landing == "person" {
        await scene.personScroll(toEnd: false, offset: max(0, (record.position()["offset"] ?? 0) - 1500))
        record.step("person_scrolled", target: target)
    }
    if catchup {
        scene.daemon.resumeCatchup()
        await scene.waitFolded(scene.daemon.lastSeq, "send.caught_up")
        await settle(scene.host, seconds: 1.0)
        record.step("caught_up", target: target)
    }
    await scene.stream(20)
    record.step("reply", target: target)
    return record.finish(["target": target, "catchup": catchup, "landing": landing ?? "ordinary",
                          "held_before_reveal": scene.heldBeforeReveal,
                          "presentation": ["natural": natural, "animation_calls": ProbePresentation.animationCalls,
                                           "forwarded_animations": ProbePresentation.forwardedAnimations,
                                           "root_transactions": ProbePresentation.rootTransactions,
                                           "animated_root_transactions": ProbePresentation.animatedRootTransactions]])
}

/// Runs `move` once, the first time the clip has rested at the end for `after` seconds
/// without a document or clip change. By default, use the landing check's `column + 1`
/// tolerance; `within` instead requires an absolute gap. The same geometry changes
/// restart this timer and the own-send landing check's quiet window.
@MainActor final class QuietMove {
    private var observers: [NSObjectProtocol] = []
    private var pending: DispatchWorkItem?
    private var move: (() -> Void)?

    init(scroll: NSScrollView, after: Double, within: CGFloat? = nil, move: @escaping () -> Void) {
        self.move = move
        guard let document = scroll.documentView else { return }
        let center = NotificationCenter.default
        for (name, object) in [(NSView.frameDidChangeNotification, document as NSView),
                               (NSView.boundsDidChangeNotification, scroll.contentView as NSView)] {
            observers.append(center.addObserver(forName: name, object: object, queue: nil) { [weak self] _ in
                MainActor.assumeIsolated { self?.rearm(scroll, after: after, within: within) }
            })
        }
        rearm(scroll, after: after, within: within)
    }

    private func rearm(_ scroll: NSScrollView, after: Double, within: CGFloat?) {
        pending?.cancel()
        guard move != nil else { return }
        let item = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated {
                guard let self, let document = scroll.documentView else { return }
                let clip = scroll.contentView.bounds
                let gap = document.frame.height - clip.maxY
                let atEnd = within.map { abs(gap) <= $0 } ?? (gap <= Theme.space.column + 1)
                guard atEnd, let move = self.move else { return }
                self.move = nil
                move()
            }
        }
        pending = item
        DispatchQueue.main.asyncAfter(deadline: .now() + after, execute: item)
    }

    func stop() {
        pending?.cancel()
        move = nil
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
    }
}

// P2-3: the pin's mark check must run whenever the mark's place in the document
// changes. Three scenes, each with a 2,000 pt document under a 550 pt clip and a
// reveal outstanding for `target`:
// - `swiftui`: a SwiftUI layout redistributes row heights, moving the row's
//   AppKitPlatformViewHost from y=300 to y=1,700 with the clip at the end (y=1,450);
//   the mark's own frame, the clip and the document do not change. The row and the
//   end are then in the clip together: landed, and suspension released.
// - `parent`: an AppKit parent view moves the mark from y=300 to y=1,200 under a
//   clip at y=1,000, its own frame unchanged: landed, with the end out of view.
// - `identity`: a reused mark already in the clip at the end takes the target's id
//   (`updateNSView` on reuse): landed, and suspension released.
@MainActor final class MarkRows: ObservableObject { @Published var heights: [CGFloat] = [300, 100, 1600] }

struct MarkRowsView: View {
    @ObservedObject var rows: MarkRows
    let cid: String
    let landed: (UUID) -> Void
    let endVisible: (UUID) -> Void
    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(Array(rows.heights.enumerated()), id: \.offset) { index, height in
                    Color.clear.frame(height: height).background(mark(index == 1))
                }
            }
            .background(ConversationOpeningPin(conversationID: cid, onRevealEndVisible: endVisible,
                                               onRevealLanded: landed))
        }
    }
    @ViewBuilder private func mark(_ isTarget: Bool) -> some View {
        if isTarget { RevealTargetMark(conversationID: cid, itemID: "target") }
    }
}

final class FlippedDocument: NSView { override var isFlipped: Bool { true } }

@MainActor func markScene(_ kind: String) async -> [String: Any] {
    let cid = "mark-" + kind
    let started = Date()
    func at() -> Double { Date().timeIntervalSince(started) }
    var trace: [[String: Any]] = []
    var landed: [Double] = []
    var endVisible: [Double] = []
    ConversationFollowTest.record = { id, entry in
        guard id == cid else { return }
        trace.append(entry.merging(["at": at()]) { _, new in new })
    }
    defer { ConversationFollowTest.record = nil }
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 550), styleMask: [.borderless],
                          backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    let rows = MarkRows()
    var parent: NSView?
    var appKitMark: RevealTargetView?
    var appKitPin: OpeningPinView?
    if kind == "swiftui" {
        let host = NSHostingView(rootView: MarkRowsView(rows: rows, cid: cid, landed: { _ in landed.append(at()) },
                                                        endVisible: { _ in endVisible.append(at()) })
            .transaction { $0.animation = nil; $0.disablesAnimations = true })
        window.contentView = host
    } else {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 400, height: 550))
        let document = FlippedDocument(frame: NSRect(x: 0, y: 0, width: 400, height: 2000))
        scroll.documentView = document
        let pin = OpeningPinView(frame: NSRect(x: 0, y: 0, width: 1, height: 1))
        pin.conversationID = cid
        pin.onRevealLanded = { _ in landed.append(at()) }
        pin.onRevealEndVisible = { _ in endVisible.append(at()) }
        document.addSubview(pin)
        // `parent`: the mark's row container far above the clip. `identity`: a
        // reused mark, still carrying another row's id, already in the clip.
        let holder = NSView(frame: NSRect(x: 0, y: kind == "parent" ? 300 : 1600, width: 400, height: 100))
        let mark = RevealTargetView(frame: holder.bounds)
        mark.conversationID = cid
        mark.itemID = kind == "parent" ? "target" : "another-row"
        holder.addSubview(mark)
        document.addSubview(holder)
        window.contentView = scroll
        parent = holder
        appKitMark = mark
        appKitPin = pin
    }
    mountOffscreen(window)
    let content = window.contentView!
    await settle(content, seconds: 0.5)
    func marks() -> [RevealTargetView] { descendants(content).compactMap { $0 as? RevealTargetView } }
    func snapshot() -> [String: Any] {
        var out: [String: Any] = sample(content)
        if let mark = marks().first, let document = timelineScroll(content)?.documentView {
            let local = mark.frame
            let placed = mark.convert(mark.bounds, to: document)
            out["mark_frame"] = ["x": Double(local.minX), "y": Double(local.minY), "height": Double(local.height)]
            out["mark_in_document"] = ["y": Double(placed.minY), "height": Double(placed.height)]
            out["mark_id"] = mark.itemID ?? ""
        }
        out["landed"] = landed.count
        out["end_visible"] = endVisible.count
        return out
    }
    // The opening pin has put the clip at the end; the request stops it.
    let opened = snapshot()
    let request = ConversationOpeningPin.revealRow(in: cid, target: "target")
    if kind == "parent", let scroll = timelineScroll(content) {
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 1000))
        scroll.reflectScrolledClipView(scroll.contentView)
    }
    ConversationOpeningPin.didRevealRow(in: cid, request: request)
    await settle(content, seconds: 0.5)
    let before = snapshot()
    let movedAt = at()
    switch kind {
    case "swiftui": rows.heights = [1700, 100, 200]
    case "parent": parent?.setFrameOrigin(NSPoint(x: 0, y: 1200))
    default: appKitMark?.itemID = "target"
    }
    await settle(content, seconds: 0.8)
    let after = snapshot()
    appKitPin?.detach()
    window.contentView = nil
    window.close()
    return ["kind": kind, "opened": opened, "before": before, "after": after, "moved_at": movedAt,
            "landed_at": landed, "end_visible_at": endVisible, "trace": trace]
}

@MainActor func fix12Cases(_ root: URL, mode: String) async -> [String: Any] {
    switch mode {
    case "removed": return await removedTarget(root, away: false)
    case "removed-away": return await removedTarget(root, away: true)
    case "missing": return await missingTarget(root, away: false)
    case "missing-away": return await missingTarget(root, away: true)
    case "send-catchup": return await ownSendDuringReveal(root, catchup: true)
    case "send-ordinary": return await ownSendDuringReveal(root, catchup: false)
    case "short", "regrow", "person", "regrow-natural", "quiet-regrow", "quiet-regrow-natural", "quiet-regrow-late",
         "stale-glide":
        return await ownSendDuringReveal(root, catchup: false, landing: mode)
    case "mark":
        var out: [String: Any] = [:]
        for kind in ["swiftui", "parent", "identity"] { out[kind] = await markScene(kind) }
        out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        return out
    default:
        FileHandle.standardError.write(Data("unknown fix12 mode \(mode)\n".utf8))
        exit(64)
    }
}
