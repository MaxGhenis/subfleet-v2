// The core probe's queue-tray engine and store commands (design §12, C-29.7):
// Withdraw's daemon calls against a scripted daemon or a daemon socket, and the
// store state the app keeps around them. Foundation only; part of the core
// probe (conftest.CORE_PROBE).
import Foundation

// MARK: - Engine

/// A daemon call as the probes record it: the op, the message ids it names, and
/// the conversation a cancel names (a tombstone's).
func callWords(op: String, args: JSONValue?) -> String {
    var words = [op]
    if let id = args?["message_id"]?.string { words.append(id) }
    if let ids = args?["message_ids"]?.array { words.append(ids.compactMap(\.string).joined(separator: ",")) }
    if op == Ops.messageCancel.name, let cid = args?["conversation_id"]?.string { words.append("conversation=" + cid) }
    return words.joined(separator: " ")
}

/// A daemon that answers each op from its own queue of scripted answers, in
/// order: `{"result": <the op's result>}`, `{"error": {"code", "message", "fix"}}`
/// (a refusal, worded `"<reason>: <text>"` as the daemon words it) or
/// `{"timeout": true}` (no answer in time). An op whose queue is empty is
/// answered as a daemon that is not listening. Every call is recorded.
final class ScriptedDaemon: DaemonCalling {
    private var answers: [String: [JSONValue]]
    private(set) var calls: [String] = []

    init(answers: [String: [JSONValue]]) {
        self.answers = answers
    }

    /// The answers no call used, by op.
    var unused: [String: Int] { answers.mapValues(\.count).filter { $0.value > 0 } }

    func call<Args: Encodable, Result: Decodable>(_ op: DaemonOperation<Args, Result>, _ args: Args) throws -> Result {
        calls.append(callWords(op: op.name, args: try JSONValue.from(args)))
        guard var queue = answers[op.name], !queue.isEmpty else {
            throw DaemonClientError.unavailable("the script has no answer left for \(op.name)")
        }
        let answer = queue.removeFirst()
        answers[op.name] = queue
        if let refusal = answer["error"] {
            throw DaemonClientError.daemon(try refusal.decode(DaemonError.self))
        }
        if answer["timeout"]?.bool == true {
            throw DaemonClientError.timedOut(op: op.name, seconds: op.timeout(for: args))
        }
        guard let result = answer["result"] else {
            throw DaemonClientError.malformed("\(op.name): the scripted answer has no result")
        }
        do {
            return try result.decode(Result.self)
        } catch {
            throw DaemonClientError.malformed("\(op.name): result does not match \(Result.self): \(error)")
        }
    }
}

/// Holds each of the engine's pauses at a gate directory until the test lets it
/// go: pause `n` writes `paused-<n>` and waits for `go-<n>` (at most 30 s).
func holdAtGate(_ directory: String, pause: Int) {
    let manager = FileManager.default
    manager.createFile(atPath: directory + "/paused-\(pause)", contents: nil)
    let deadline = Date().addingTimeInterval(30)
    while !manager.fileExists(atPath: directory + "/go-\(pause)") && Date() < deadline {
        usleep(5_000)
    }
}

func decodeStopAction(_ value: JSONValue) -> StopAction {
    let id = value["message_id"]?.string ?? ""
    switch value["action"]?.string {
    case "withdraw": return .withdraw(messageID: id)
    case "cancel": return .cancel(messageID: id)
    case "interrupt": return .interrupt(messageID: id)
    default: return .none
    }
}

func project(_ result: ConversationEngine.WithdrawResult) -> [String: Any] {
    switch result {
    case .withdrawn(let receipt):
        return ["outcome": "withdrawn", "receipt": receipt.map(jsonObject) as Any? ?? NSNull()]
    case .stopped(let receipt):
        return ["outcome": "stopped", "receipt": receipt.map(jsonObject) as Any? ?? NSNull()]
    case .inFlight: return ["outcome": "in-flight"]
    }
}

/// `queue-engine <input.json>`: Stop and Withdraw through the app's
/// `ConversationEngine`, over a scripted daemon (`"answers": {<op>: [answer]}`,
/// see `ScriptedDaemon`) or a daemon socket (`"socket": <path>`). The outbox is
/// in memory. `"dispatching_retries"` replaces the engine's waits between
/// cancels answered `dispatching`. The engine's pauses are recorded, never
/// slept; with `"gate": <directory>` each is held there (`holdAtGate`) so a test
/// can move the daemon's state in between. Steps:
/// - `{"do": "stop", "action": {"action", "message_id"}}`: `engine.stop` of that action;
/// - `{"do": "stop", "message_id", "state"}`: the action `stopAction` picks from the
///   state the app saw and the message's outbox entry, and its label, then `engine.stop`;
///   with `"outbox_entry": false` it is asked with no entry, as the app's controls ask it
///   (the timeline's status line, the live turn's strip and the composer's Stop);
/// - `{"do": "withdraw_send", "message_id"}`: `engine.withdrawSend`, as the tray's
///   Withdraw of a row with no receipt runs it;
/// - `{"do": "journal", "conversation", "message_id", "text"}`: journal a send;
/// - `{"do": "begin", "message_id"}`: the send is under way, not answered yet;
/// - `{"do": "lose_answer", "message_id"}`: the send went and its answer was lost.
/// Each step reports the calls it made and the pauses it asked for.
/// `{"runs": [<input>, ...]}` runs each input with its own daemon and engine.
func runQueueEngine(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    if let runs = input["runs"]?.array {
        return ["runs": try runs.map(runQueueEngine)]
    }
    return try runQueueEngine(input)
}

func runQueueEngine(_ input: JSONValue) throws -> [String: Any] {
    var calls: [String] = []
    let scripted: ScriptedDaemon?
    let client: DaemonCalling
    if let socket = input["socket"]?.string {
        let daemon = DaemonClient(transport: UnixSocketTransport(path: socket))
        daemon.baseTimeout = 3
        daemon.onExchange = { op, request, _ in
            calls.append(callWords(op: op, args: (try? JSONValue.parse(request))?["args"]))
        }
        scripted = nil
        client = daemon
    } else {
        let answers = (input["answers"]?.object ?? [:]).mapValues { $0.array ?? [] }
        scripted = ScriptedDaemon(answers: answers)
        client = scripted!
    }
    func madeCalls() -> [String] { scripted?.calls ?? calls }

    let outbox = try Outbox(url: nil)
    let engine = ConversationEngine(client: client, outbox: outbox)
    if let retries = input["dispatching_retries"]?.array {
        engine.dispatchingRetries = retries.compactMap(\.double)
    }
    var pauses: [Double] = []
    let gate = input["gate"]?.string
    engine.pause = { seconds in
        pauses.append(seconds)
        if let gate { holdAtGate(gate, pause: pauses.count) }
    }
    let settings = ConversationSettings(model: "opus[1m]")
    var results: [Any] = []
    for step in input["steps"]?.array ?? [] {
        let action = step["do"]?.string ?? ""
        let callsBefore = madeCalls().count, pausesBefore = pauses.count
        var result: [String: Any] = ["do": action]
        do {
            switch action {
            case "stop":
                let stop: StopAction
                if let explicit = step["action"] {
                    stop = decodeStopAction(explicit)
                } else {
                    let id = step["message_id"]?.string ?? ""
                    let entry = step["outbox_entry"]?.bool == false ? nil : outbox.entry(id)
                    stop = stopAction(for: id, state: step["state"]?.string, outboxEntry: entry)
                }
                result["action"] = project(stop)
                result["label"] = stopLabel(stop) as Any? ?? NSNull()
                result["receipt"] = try engine.stop(stop).map(jsonObject) as Any? ?? NSNull()
            case "withdraw_send":
                result["result"] = project(try engine.withdrawSend(step["message_id"]?.string ?? ""))
            case "journal":
                let cid = step["conversation"]?.string ?? ""
                if outbox.journal.chains[cid] == nil { try outbox.knowChain(cid, lastPersonMessageID: nil) }
                let entry = try engine.send(conversation: cid, text: step["text"]?.string ?? "", settings: settings,
                                            messageID: step["message_id"]?.string ?? Outbox.newMessageID())
                result["key"] = entry.key
            case "begin":
                _ = try outbox.begin(step["message_id"]?.string ?? "")
            case "lose_answer":
                let key = step["message_id"]?.string ?? ""
                _ = try outbox.begin(key)
                try outbox.finish(key, .failed(.timedOut(op: Ops.messageSubmit.name, seconds: 15)))
            default:
                result["error"] = "unknown step"
            }
        } catch {
            result["error"] = describe(error)
        }
        result["calls"] = Array(madeCalls()[callsBefore...])
        result["pauses"] = Array(pauses[pausesBefore...])
        results.append(result)
    }
    return ["results": results, "calls": madeCalls(), "pauses": pauses, "unused": scripted?.unused ?? [:],
            "entries": outbox.entries.map(project)]
}

// MARK: - Store state

/// `queue-state <input.json>`: the store state around the tray, as the app
/// folds it. Steps: `{"focus": <conversation id>|null}`, `{"receipts": [...]}`
/// (each through `apply(receipt:)`), `{"local": {conversation_id, message_id,
/// text}}` (the composer's row before its receipt), `{"withdraw_local": <message
/// id>}` (withdrawn before the daemon had it), and `{"page": <events page>,
/// "conversation_id"}` (the events loop). After each step, for the focused
/// conversation: whether its timeline has read its log (`caught_up`), its
/// layout's tray and timeline rows, and what a conversation view showing it
/// does: the move of the follow key (`"appear"` when the view is new: the app
/// makes one for each conversation it shows) and the approval follower's
/// target. At the end, every timeline's layout and message states.
func runQueueState(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var state = ConversationStoreState()
    var reports: [Any] = []
    var shown: String?
    var follower = ApprovalFollower()
    var key = FollowKey.empty
    for step in input["steps"]?.array ?? [] {
        var did = ""
        if let focus = step["focus"] {
            state.focus(focus.string)
            did = "focus"
        } else if let receipts = step["receipts"] {
            for receipt in try receipts.decode([Receipt].self) { state.apply(receipt: receipt) }
            did = "receipts"
        } else if let local = step["local"] {
            state.addLocalMessage(conversationID: local["conversation_id"]?.string ?? "",
                                  messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "")
            did = "local"
        } else if let id = step["withdraw_local"]?.string {
            state.withdrawLocal(messageID: id)
            did = "withdraw_local"
        } else if let page = step["page"], let cid = step["conversation_id"]?.string {
            did = pageResult(state.apply(events: try page.decode(EventsPage.self), conversationID: cid))
        }
        var report: [String: Any] = ["did": did, "focused": state.focusedConversationID as Any? ?? NSNull()]
        if let id = state.focusedConversationID, let timeline = state.timelines[id] {
            let layout = timeline.layout(held: step["held"]?.bool == true)
            let next = layout.followKey(length: queueProbeLength)
            if shown != id {
                // A new view (`.id(conversation_id)`): it starts at the end, and nothing has moved yet.
                shown = id
                follower = ApprovalFollower()
                report["move"] = "appear"
            } else {
                report["move"] = moveWord(FollowKey.move(from: key, to: next, atBottom: true))
            }
            key = next
            report["scroll"] = follower.target(in: timeline, reveal: false) as Any? ?? NSNull()
            report["caught_up"] = timeline.caughtUp
            report["tray"] = layout.tray.map(\.id)
            report["items"] = layout.items.map(\.id)
            report["title"] = layout.trayTitle as Any? ?? NSNull()
        } else {
            shown = nil
        }
        reports.append(report)
    }
    var timelines: [String: Any] = [:]
    for (id, timeline) in state.timelines {
        timelines[id] = [
            "caught_up": timeline.caughtUp, "layout": project(timeline.layout(held: false)),
            "turns": Dictionary(uniqueKeysWithValues: timeline.order.compactMap { mid in
                timeline.turn(mid).map { turn in
                    (mid, ["state": turn.state, "state_reason": turn.stateReason as Any? ?? NSNull()])
                }
            }),
        ]
    }
    return ["steps": reports, "timelines": timelines, "focused": state.focusedConversationID as Any? ?? NSNull()]
}
