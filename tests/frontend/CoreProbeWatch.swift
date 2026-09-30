// Watch-feed subcommands of the core probe (C-29.9): which messages the feed
// has the app fetch with `message.status`, and the app's feed against a socket
// while another client submits.
import Foundation

func project(_ fetches: MessageFetches) -> [String: Any] {
    ["wanted": fetches.wanted, "asked": fetches.asked, "stale": fetches.stale.sorted()]
}

/// A turn as the fetch tests read it: what the receipt said it is, where it stands.
func projectFetched(_ turn: TurnTimeline) -> [String: Any] {
    ["seq": turn.seq as Any? ?? NSNull(), "origin": turn.origin as Any? ?? NSNull(),
     "continues": turn.continues as Any? ?? NSNull(), "state": turn.state,
     "state_reason": turn.stateReason as Any? ?? NSNull(), "person_text": turn.personText as Any? ?? NSNull(),
     "stop_requested": turn.stopRequested, "lacks_receipt": turn.lacksReceipt, "status_text": turn.statusText]
}

func projectFetched(_ timeline: Timeline) -> [String: Any] {
    var turns: [String: Any] = [:]
    for id in timeline.order where id != Timeline.conversationKey {
        if let turn = timeline.turn(id) { turns[id] = projectFetched(turn) }
    }
    let shown = timeline.items.compactMap { item -> [String: Any]? in
        switch item.content {
        case .person(let text, _, let state):
            return ["message_id": item.messageID as Any? ?? NSNull(), "person": text as Any? ?? NSNull(), "state": state]
        case .notice(let text) where item.id.hasPrefix("person:"):
            return ["message_id": item.messageID as Any? ?? NSNull(), "notice": text]
        default:
            return nil
        }
    }
    return ["order": timeline.order.filter { $0 != Timeline.conversationKey }, "turns": turns, "shown": shown,
            "display": timeline.displayOrder.filter { $0 != Timeline.conversationKey }]
}

func projectFetchState(_ state: ConversationStoreState) -> [String: Any] {
    ["fetches": project(state.messageFetches), "watch_cursor": state.watchCursor,
     "timelines": state.timelines.mapValues(projectFetched)]
}

/// `watch-fetch <input.json>`: `{"steps": [...]}`, each one of
/// `{"focus": cid}`, `{"open": ConversationOpenResult}`, `{"receipts": [Receipt]}`,
/// `{"local": {conversation_id, message_id, text}}`, `{"events": page, "conversation_id"}`,
/// `{"watch": WatchPage}`, `{"take": limit}`, `{"answer": [Receipt]}` or
/// `{"fail": true}` (either for the batch out); the state after each step.
func runWatchFetch(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var state = ConversationStoreState()
    var batch: [String] = []
    var snapshots: [[String: Any]] = []
    for step in input["steps"]?.array ?? [] {
        var taken: Any = NSNull()
        if step["focus"] != nil { state.focus(step["focus"]?.string) }
        if let open = step["open"] { state.apply(open: try open.decode(ConversationOpenResult.self)) }
        if let receipts = step["receipts"] {
            for receipt in try receipts.decode([Receipt].self) { state.apply(receipt: receipt) }
        }
        if let local = step["local"] {
            state.addLocalMessage(conversationID: local["conversation_id"]?.string ?? "",
                                  messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "")
        }
        if let page = step["events"], let cid = step["conversation_id"]?.string {
            state.apply(events: try page.decode(EventsPage.self), conversationID: cid)
        }
        if let page = step["watch"] { state.apply(watch: try page.decode(WatchPage.self)) }
        if let limit = step["take"]?.double {
            let got = state.takeMessageFetch(limit: Int(limit))
            if !got.isEmpty { batch = got }
            taken = got
        }
        if let receipts = step["answer"] {
            state.apply(fetched: try receipts.decode([Receipt].self), asked: batch)
            batch = []
        }
        if step["fail"]?.bool == true {
            state.messageFetchFailed(batch)
            batch = []
        }
        var snapshot = projectFetchState(state)
        snapshot["taken"] = taken
        snapshots.append(snapshot)
    }
    return ["snapshots": snapshots]
}

/// `app-session <socket> <journal>`: the app's engine and store state against a
/// daemon socket, one command per stdin line, one JSON answer per stdout line,
/// so a test can act as another client between commands. Commands (`do`):
/// `connect` (UIModel.connect: capabilities, the list, the watch baseline, then
/// the fetch), `focus` (`conversation_id`: UIModel.focus and open), `watch` (one
/// page of the feed, then the fetch, as UIModel.fold does), `events` (catch the
/// focused conversation up), `status` (`ids`: `ConversationEngine.status`),
/// `send` (`text`: the composer's send and a pump).
func runAppSession(socket: String, journal: String) throws -> Any {
    let client = DaemonClient(transport: try probeTransport(socket))
    client.baseTimeout = 5
    let callsLock = NSLock()
    var calls: [[String: Any]] = []
    client.onExchange = { op, request, _ in
        let args = (try? JSONValue.parse(request))?["args"]
        var call: [String: Any] = ["op": op]
        if let ids = args?["message_ids"]?.array { call["ids"] = ids.compactMap(\.string) }
        callsLock.lock()
        calls.append(call)
        callsLock.unlock()
    }
    let outbox = try Outbox(url: URL(fileURLWithPath: journal))
    let engine = ConversationEngine(client: client, outbox: outbox)
    engine.pollWait = 0
    var state = ConversationStoreState()
    let settings = ConversationSettings(model: "opus[1m]")

    func answer(_ value: [String: Any]) {
        let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
        FileHandle.standardOutput.write(data + Data("\n".utf8))
    }

    while let line = readLine() {
        let command = try JSONValue.parse(Data(line.utf8))
        callsLock.lock()
        calls = []
        callsLock.unlock()
        var failure: Any = NSNull()
        var extra: [String: Any] = [:]
        do {
            switch command["do"]?.string ?? "" {
            case "connect":
                state.availability = engine.checkAvailability()
                state.apply(list: try engine.list())
                try engine.drainWatch(&state)
                state.watchBaselined = true
                try engine.fetchNamedMessages(&state)
            case "focus":
                let cid = command["conversation_id"]?.string ?? ""
                state.focus(cid)
                state.apply(open: try engine.open(.conversation(cid)))
                try engine.catchUp(&state, conversationID: cid)
            case "watch":
                let page = try engine.watch(after: state.watchCursor, wait: 0)
                state.apply(watch: page)
                extra["rows"] = page.changes.count
                try engine.fetchNamedMessages(&state)
            case "events":
                if let cid = state.focusedConversationID { try engine.catchUp(&state, conversationID: cid) }
            case "status":
                // `ConversationEngine.status` for any number of ids.
                let ids = command["ids"]?.array?.compactMap(\.string) ?? []
                extra["receipts"] = try engine.status(ids).map(\.message_id)
            case "send":
                guard let cid = state.focusedConversationID else { break }
                let messageID = Outbox.newMessageID()
                let text = command["text"]?.string ?? ""
                state.addLocalMessage(conversationID: cid, messageID: messageID, text: text, settings: settings)
                _ = try engine.send(conversation: cid, text: text, settings: settings, messageID: messageID)
                state.apply(outbox: engine.pump(), outbox: outbox)
                extra["message_id"] = messageID
            case "quit":
                return ["done": true]
            default:
                failure = "unknown command"
            }
        } catch {
            failure = describe(error)
        }
        callsLock.lock()
        let made = calls
        callsLock.unlock()
        var out = projectFetchState(state)
        out.merge(extra) { _, new in new }
        out["calls"] = made
        out["error"] = failure
        out["focused"] = state.focusedConversationID as Any? ?? NSNull()
        answer(out)
    }
    return ["done": true]
}

func watchCommand(_ arguments: [String]) throws -> Any? {
    switch arguments[1] {
    case "watch-fetch":
        return try runWatchFetch(readFile(arguments[2]))
    case "app-session":
        return try runAppSession(socket: arguments[2], journal: arguments[3])
    default:
        return nil
    }
}
