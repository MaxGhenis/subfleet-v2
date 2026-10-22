import Foundation

// Review r4 of #219: the engine against a scripted daemon whose capabilities
// change across restarts, whose status op fails, and whose conversations are long.

func json(_ any: Any) -> JSONValue { try! JSONValue.parse(try! JSONSerialization.data(withJSONObject: any)) }

final class R4Daemon: DaemonCalling {
    var statusSupported: Bool
    var statusError: String?
    var dropNextWatch = false
    let conversation: [String: Any]
    let receipts: [[String: Any]]
    var watchSeq = 0
    var ops: [String] = []
    var statusIDs: [[String]] = []

    init(conversation: [String: Any], receipts: [[String: Any]], statusSupported: Bool) {
        self.conversation = conversation
        self.receipts = receipts
        self.statusSupported = statusSupported
    }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let value = try JSONValue.from(args)
        ops.append(op.name)
        let out: Any
        switch op.name {
        case "capabilities":
            out = ["protocol": 1, "daemon_version": "probe", "conversation_schema": 1,
                   "capabilities": statusSupported ? ["conversations.v1", "conversation.status.v1"] : ["conversations.v1"]]
        case "conversation.open":
            out = ["conversation": conversation, "messages": Array(receipts.suffix(50)), "events_cursor": 0,
                   "pending_approvals": []]
        case "conversation.status":
            guard statusSupported else { throw DaemonClientError.unavailable("unknown op: conversation.status") }
            if let statusError { throw DaemonClientError.unavailable(statusError) }
            out = ["conversation": conversation, "message_ids": receipts.map { $0["message_id"]! }]
        case "message.status":
            let ids = value["message_ids"]?.array?.compactMap(\.string) ?? []
            statusIDs.append(ids)
            let wanted = Set(ids)
            out = ["messages": receipts.filter { wanted.contains($0["message_id"] as! String) }]
        case "conversation.watch":
            if dropNextWatch {
                dropNextWatch = false
                throw DaemonClientError.unavailable("connection reset (daemon restarting)")
            }
            let after = value["after"]?.int ?? 0
            watchSeq = max(watchSeq, after) + 1
            let mid = receipts.last!["message_id"]!
            out = ["changes": [["seq": watchSeq, "conversation_id": conversation["conversation_id"]!, "message_id": mid,
                                "state": "queued", "pending_approvals": 0]], "next": watchSeq]
        default:
            throw DaemonClientError.unavailable("probe has no \(op.name)")
        }
        return try json(out).decode(R.self)
    }
}

func conversationJSON(_ cid: String, blocked: Bool) -> [String: Any] {
    var out: [String: Any] = ["conversation_id": cid, "provider": "claude", "workspace": "/tmp/project",
                              "workspace_kind": "in-place", "allow_main": false,
                              "settings": ["model": "claude-opus-5-5", "fast": false, "permission": "ask", "auto_continue": true], "origin": "person",
                              "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z",
                              "pending_approvals": 0, "active": false]
    if blocked { out["blocked_by"] = "unfinished-turn" }
    return out
}

func receiptsJSON(_ cid: String, history: Int, queued: Int, textChars: Int = 200) -> [[String: Any]] {
    var out: [[String: Any]] = []
    for index in 0..<(history + queued) {
        let done = index < history
        out.append(["message_id": String(format: "m%06d", index), "conversation_id": cid, "seq": index + 1,
                    "origin": "person", "state": done ? "complete" : "queued",
                    "text": "message \(index) " + String(repeating: "x", count: textChars),
                    "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"])
    }
    return out
}

/// Run the real WatchLoop for `attempts` answers. `lost` re-checks capabilities
/// as UIModel.lostDaemon does (on the outbox queue, before the 2 s backoff ends).
func watch(_ engine: ConversationEngine, _ state: inout ConversationStoreState, cid: String, attempts: Int)
    -> (delivered: Int, errors: [String], recheck: Int) {
    var seen = 0, delivered = 0, recheck = 0
    var errors: [String] = []
    var local = state
    let loop = WatchLoop(engine: engine,
        cursor: { seen < attempts ? local.watchCursor : nil },
        deliver: { update in seen += 1; delivered += 1; local.apply(update: update) },
        lost: { error in
            seen += 1
            errors.append(String(describing: error))
            recheck += 1
            local.availability = engine.checkAvailability()
        },
        regained: {}, pause: { _ in },
        focusedConversationID: { cid }, knownMessageIDs: { local.knownMessageIDs(in: $0) })
    loop.run()
    state = local
    return (delivered, errors, recheck)
}

func ms(_ start: DispatchTime) -> Double { Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1e6 }

@main struct R4EngineProbe {
    static func main() throws {
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var results: [String: Any] = [:]

        // 5. Cost of hydration on open, and on opening the same conversation again.
        var cost: [[String: Any]] = []
        for history in input["histories"]?.array?.compactMap(\.int) ?? [] {
            let cid = "cost-\(history)"
            let daemon = R4Daemon(conversation: conversationJSON(cid, blocked: true),
                                  receipts: receiptsJSON(cid, history: history, queued: 3), statusSupported: true)
            let engine = ConversationEngine(client: daemon, outbox: try Outbox(url: nil))
            var state = ConversationStoreState()
            state.availability = engine.checkAvailability()
            var start = DispatchTime.now()
            let first = try engine.open(.conversation(cid))
            let openMS = ms(start)
            let firstReads = daemon.statusIDs.reduce(0) { $0 + $1.count }
            let firstCalls = daemon.statusIDs.count
            start = DispatchTime.now()
            state.apply(open: first)
            let applyMS = ms(start)
            state.focus(cid)
            let tray = state.layout(conversationID: cid)!.tray.count
            let known = state.knownMessageIDs(in: cid).count
            // The first watch change for the focused conversation (where round 3 hydrated).
            daemon.statusIDs = []
            var watchMS = 0.0
            var watchState = state
            var answered = 0
            var watchErrors: [String] = []
            let loop = WatchLoop(engine: engine, cursor: { answered == 0 ? watchState.watchCursor : nil },
                deliver: { update in
                    answered += 1
                    let begin = DispatchTime.now()
                    watchState.apply(update: update)
                    watchMS = ms(begin)
                },
                lost: { error in answered += 1; watchErrors.append(String(describing: error)) },
                regained: {}, pause: { _ in },
                focusedConversationID: { cid }, knownMessageIDs: { watchState.knownMessageIDs(in: $0) })
            loop.run()
            state = watchState
            let watchIDs = daemon.statusIDs.reduce(0) { $0 + $1.count }
            // Switch away and back: the engine opens again, whatever the app already knows.
            daemon.statusIDs = []
            start = DispatchTime.now()
            let again = try engine.open(.conversation(cid))
            let reopenMS = ms(start)
            start = DispatchTime.now()
            state.apply(open: again)
            let reapplyMS = ms(start)
            cost.append(["history": history, "first_open_ms": openMS, "first_apply_ms": applyMS,
                         "first_status_calls": firstCalls, "first_status_ids": firstReads, "tray": tray,
                         "known_after_open": known, "reopen_ms": reopenMS, "reapply_ms": reapplyMS,
                         "first_watch_apply_ms": watchMS, "first_watch_status_ids": watchIDs,
                         "first_watch_errors": watchErrors,
                         "reopen_status_calls": daemon.statusIDs.count,
                         "reopen_status_ids": daemon.statusIDs.reduce(0) { $0 + $1.count }])
        }
        var queues: [[String: Any]] = []
        for count in input["queues"]?.array?.compactMap(\.int) ?? [] {
            let cid = "queue-\(count)"
            let daemon = R4Daemon(conversation: conversationJSON(cid, blocked: true),
                                  receipts: receiptsJSON(cid, history: 17, queued: count), statusSupported: true)
            let engine = ConversationEngine(client: daemon, outbox: try Outbox(url: nil))
            var state = ConversationStoreState()
            state.availability = engine.checkAvailability()
            state.apply(open: try engine.open(.conversation(cid)))
            let first = state.layout(conversationID: cid)!.tray.map(\.id)
            daemon.statusIDs = []
            state.apply(open: try engine.open(.conversation(cid)))
            queues.append(["count": count, "first": first, "again": state.layout(conversationID: cid)!.tray.map(\.id),
                           "reads": daemon.statusIDs.flatMap { $0 }, "batch_sizes": daemon.statusIDs.map(\.count)])
        }
        results["queues"] = queues
        // A reverse-sequence batch catches an implementation that omits sorting.
        let receipts = try json(receiptsJSON("batch", history: input["batch"]?.int ?? 0, queued: 3)).decode([Receipt].self)
        var timeline = Timeline(conversationID: "batch")
        let batchStart = DispatchTime.now()
        timeline.apply(receipts: receipts.reversed())
        results["batch"] = ["apply_ms": ms(batchStart), "order": timeline.order,
                            "expected": receipts.map(\.message_id)]
        results["cost"] = cost
        print(String(decoding: try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), as: UTF8.self))
    }
}
