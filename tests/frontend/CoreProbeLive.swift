// `live <app-dir> <workspace> <native-session-id> <image.png> <exchanges.jsonl>`
//
// Drives a real development daemon (SUBFLEET_HOME, fake providers) through the
// app's own engine, outbox and store state, the way the UI will: create a
// conversation with a queued follow-up, follow its events, stop a turn, answer
// a tool approval and a question as the development app, retry and withdraw
// through the outbox, send an image, notify for an unfocused conversation, and
// continue a native session from the catalog. Every exchange is written to
// the exchanges file for the test to check against the daemon's own JSON. A daemon
// that advertises `steer.v1` is also steered into a running turn (step 14).
//
// A step the later ones wait behind (a turn left running, a card left
// unanswered) is `require`d: when it fails the run stops there, with the app
// core's state and the daemon's view of it in `notes`, instead of every later
// step waiting out its own timeout (a 300 s cascade that hid the first failure).
import Foundation

/// A required step failed: the run stops after recording it.
struct LiveStop: Error {
    let check: String
}

final class LiveRun {
    var checks: [[String: Any]] = []
    var notes: [String: Any] = [:]
    let started = Date()
    /// What the app core holds, and what the daemon says now, for a run that stopped.
    var dump: (() -> Any)?

    func check(_ name: String, _ passed: Bool, _ detail: Any = NSNull()) {
        checks.append(["name": name, "passed": passed, "detail": detail,
                       "at": Date().timeIntervalSince(started)])
    }

    /// A check the rest of the run depends on: a failure stops it.
    func require(_ name: String, _ passed: Bool, _ detail: Any = NSNull()) throws {
        check(name, passed, detail)
        if !passed { throw LiveStop(check: name) }
    }
}

/// A journaled steer as a check's detail, every field with a missing one as null: a failed
/// steer check then says whether its steer was refused, is waiting to be asked again, or
/// was never journaled (its receipt alone read null for all three, D-STEER-PUMP1).
func steerDetail(_ steer: OutboxSteer?) -> Any {
    guard let steer else { return "no steer journaled" }
    return project(steer)
}

/// Step 14's check (C-24.9): the daemon took the steer. The outbox holds its receipt and
/// the message is steering or steered; a steer refused or given up never passes.
func steerTaken(_ steer: OutboxSteer?) -> Bool {
    steer?.state == .acknowledged && ["steering", "steered"].contains(steer?.receipt?.state ?? "")
}

/// What waiting for a steer's answer came to: the steer then, how many pumps it took, and
/// how long it waited on its clock.
struct SteerSettling {
    var steer: OutboxSteer?
    var pumps: Int
    var waited: TimeInterval
}

/// How long step 14 may take, from its start to judging its steer: an elapsed bound,
/// counting everything the step does first. With the client's 15 s answer deadline:
/// - 15 s for the prerequisite: the host's submit and the wait for its turn to run and
///   be accepted (a second or two on CI; run 40273357312 reached step 14 within 15 s of
///   the probe's start);
/// - 15 s for the steered message's own submit, which the step's first pump sends ahead
///   of its steer;
/// - 82.5 s for the steer itself: at most `Outbox.steerAttempts` (5) tries, 0.5 s doubling
///   between them (7.5 s), each waiting up to the answer deadline (75 s);
/// - 7.5 s of margin for the pumps' journal writes.
/// 120 s. Inside it the outbox gets every try it is entitled to whenever the prerequisite
/// takes 15 s or less; a slower one leaves the steer less, and the check's detail says how
/// many tries it had. A steer still open at the deadline is stuck, not slow; one answered
/// for good ends the wait as soon as the pump that answered it returns.
func steerStepDeadline(answerTimeout: TimeInterval) -> TimeInterval {
    let backoffs = (1..<Outbox.steerAttempts).map { Outbox.backoff(attempts: $0) }.reduce(0, +)
    let margin: TimeInterval = 7.5
    return answerTimeout + answerTimeout + backoffs + Double(Outbox.steerAttempts) * answerTimeout + margin
}

/// A transport that, while `deadline` is set, lets no exchange wait past it: each one waits
/// for its answer at most the client's own timeout or the time left, whichever is less,
/// and one that would begin at or after the deadline is not sent and reads as unanswered.
/// A pump through it therefore ends by the deadline however many calls it makes, give or
/// take its own journal writes (step 14's hard bound, #265 review r1).
final class DeadlineTransport: DaemonTransport {
    let inner: DaemonTransport
    let now: () -> Date
    var deadline: Date?

    init(_ inner: DaemonTransport, now: @escaping () -> Date = Date.init) {
        self.inner = inner
        self.now = now
    }

    func exchange(_ line: Data, timeout: TimeInterval) throws -> Data {
        guard let deadline else { return try inner.exchange(line, timeout: timeout) }
        let left = deadline.timeIntervalSince(now())
        guard left > 0 else { throw DaemonClientError.timedOut(op: "", seconds: 0) }
        return try inner.exchange(line, timeout: min(timeout, left))
    }
}

/// Pump until the steer of `messageID` is answered for good (taken, or closed: refused,
/// given up or withdrawn) or `deadline` passes, as the app's pump timer would (C-29.7: a
/// steer that names its turn and hears `not-steerable` is asked again briefly). Between
/// pumps it waits until the steer's own retry time (`nextAttemptAt`), or 0.5 s (the
/// outbox's shortest backoff) while none is due, so it never spins (D-STEER-PUMP1).
///
/// The deadline is checked before every pump and before every wait, and each wait is cut
/// to the time left, so nothing starts at or after it. `pump` is given the deadline and
/// must not run past it (the live step pumps through a `DeadlineTransport`): the wait then
/// ends by the deadline, a hard bound, give or take the pump's own journal writes.
func settleSteer(_ messageID: String, outbox: Outbox, deadline: Date, now: () -> Date,
                 pause: (TimeInterval) -> Void, pump: (_ deadline: Date) -> Void) -> SteerSettling {
    let started = now()
    var pumps = 0
    while now() < deadline {
        pump(deadline)
        pumps += 1
        guard let open = outbox.steer(messageID), open.isOpen else { break }
        let left = deadline.timeIntervalSince(now())
        guard left > 0 else { break }
        let retry = (open.nextAttemptAt ?? 0) - now().timeIntervalSince1970
        pause(min(left, retry > 0 ? retry : Outbox.backoff(attempts: 1)))
    }
    return SteerSettling(steer: outbox.steer(messageID), pumps: pumps, waited: now().timeIntervalSince(started))
}

func runLive(_ arguments: [String]) throws -> Any {
    let run = LiveRun()
    do {
        try driveLive(arguments, run)
    } catch let stop as LiveStop {
        run.notes["stopped_at"] = stop.check
        run.notes["state"] = run.dump?() ?? NSNull()
    } catch {
        run.check("the run ends without an error", false, describe(error))
        run.notes["stopped_at"] = "the run ends without an error"
        run.notes["state"] = run.dump?() ?? NSNull()
    }
    return ["checks": run.checks, "notes": run.notes]
}

private func driveLive(_ arguments: [String], _ run: LiveRun) throws {
    let appDirectory = URL(fileURLWithPath: arguments[2])
    let workspace = arguments[3]
    let nativeSession = arguments[4]
    let image = readFile(arguments[5])
    let exchanges = FileHandle(forWritingAtPath: arguments[6]) ?? {
        FileManager.default.createFile(atPath: arguments[6], contents: nil)
        return FileHandle(forWritingAtPath: arguments[6])!
    }()

    // The development build's endpoint: SUBFLEET_HOME, never ~/.subfleet.
    guard case .ready(let endpoint) = resolveDaemonEndpoint(flavor: .development) else {
        try run.require("endpoint resolves for the development build", false)
        return
    }
    run.check("endpoint resolves for the development build", true, endpoint.root.path)
    // Step 14 bounds its pumps' exchanges by its deadline; elsewhere this passes them through.
    let bounded = DeadlineTransport(UnixSocketTransport(path: endpoint.socketURL.path))
    let client = DaemonClient(transport: bounded)
    let exchangeLock = NSLock()
    client.onExchange = { op, request, response in
        var row: [String: Any] = ["op": op, "request": String(decoding: request, as: UTF8.self)]
        row["response"] = response.map { String(decoding: $0, as: UTF8.self) } ?? NSNull()
        let data = try! JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
        exchangeLock.lock()
        exchanges.write(data + Data("\n".utf8))
        exchangeLock.unlock()
    }
    let paths = AppPaths.rooted(at: appDirectory)
    var outbox = try Outbox(url: paths.outboxURL)
    var engine = ConversationEngine(client: client, outbox: outbox)
    var state = ConversationStoreState()
    let settings = ConversationSettings(model: "opus[1m]", permission: "ask")

    // A stopped run's state: what the app core held, and the daemon's view of the same
    // turns now (an approval listed now that a card lacked was read too early).
    run.dump = {
        var daemon: [String: Any] = [:]
        for (id, timeline) in state.timelines {
            let turns = timeline.order.filter { $0 != Timeline.conversationKey }
            let approvals = try? engine.approvals(conversationID: id)
            let receipts = try? engine.status(turns)
            daemon[id] = ["approvals": approvals.map { $0.map { jsonObject($0) } } as Any? ?? NSNull(),
                          "messages": receipts.map { $0.map { jsonObject($0) } } as Any? ?? NSNull()]
        }
        return ["app": ["timelines": state.timelines.mapValues { project($0) }, "pending_approvals": state.pendingApprovals,
                        "focused": state.focusedConversationID ?? NSNull(), "watch_cursor": state.watchCursor],
                "daemon": daemon]
    }

    func follow(_ cid: String, timeout: TimeInterval = 60, until: (Timeline) -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            try? engine.catchUp(&state, conversationID: cid)
            if let timeline = state.timelines[cid], until(timeline) { return true }
            if let page = try? engine.events(conversationID: cid, after: state.timelines[cid]?.cursor ?? 0, wait: 1) {
                state.apply(events: page, conversationID: cid)
            }
            if let timeline = state.timelines[cid], until(timeline) { return true }
        }
        return false
    }

    func refresh(_ ids: [String]) {
        for receipt in (try? engine.status(ids)) ?? [] { state.apply(receipt: receipt) }
    }

    func untilState(_ id: String, _ states: Set<String>, timeout: TimeInterval = 60) -> Receipt? {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let receipt = try? engine.status([id]).first {
                state.apply(receipt: receipt)
                if states.contains(receipt.state) { return receipt }
            }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return nil
    }

    /// What one `approval.list` returned, for a check on the card it should have joined:
    /// a card left without an id shows what the daemon listed when the app read it.
    func listing(_ approvals: [ApprovalView]) -> [[String: Any]] {
        approvals.map { ["approval_id": $0.approval_id, "message_id": $0.message_id,
                         "request_id": $0.requestID ?? NSNull(), "kind": $0.kind, "state": $0.state] }
    }

    func finalText(_ timeline: Timeline, _ messageID: String) -> String? {
        timeline.turn(messageID)?.items.reversed().compactMap { item -> String? in
            if case .text(let text, true) = item.content { return text }
            return nil
        }.first
    }

    // 1. Discovery.
    state.availability = engine.checkAvailability()
    try run.require("capabilities: the daemon speaks conversations.v1", state.availability.isReady,
                    state.availability.capabilities?.capabilities ?? [])
    run.notes["codex_writable"] = state.availability.capabilities?.codex_writable ?? NSNull()
    if let models = try? engine.models(provider: "claude") {
        state.apply(models: models, provider: "claude")
        run.check("models.list decodes", !models.models.isEmpty, models.models.map { $0.short })
    }
    try engine.drainWatch(&state)
    run.check("watch baseline drained without notifications", state.watchBaselined && state.notifications.isEmpty)

    // 2. A new conversation with its first message and a follow-up, journaled at once.
    let requestID = "app-" + UUID().uuidString.lowercased()
    let draft = try engine.createConversation(provider: "claude", workspace: workspace, settings: settings,
                                              title: "Probe conversation", requestID: requestID)
    let first = try engine.send(conversation: draft, text: "hello there", settings: settings)
    let slow = try engine.send(conversation: draft, text: "count [fake:slow]", settings: settings)
    let report = engine.pump()
    run.check("the outbox sent create, first and follow-up in order",
              report.acknowledged == [requestID, first.key, slow.key], report.acknowledged)
    guard let cid = outbox.entry(requestID)?.conversationID else {
        try run.require("conversation created", false, [project(report), outbox.entry(requestID).map(project) as Any? ?? NSNull()])
        return
    }
    run.notes["conversation_id"] = cid
    state.apply(outbox: report, outbox: outbox)
    let firstReceipt = outbox.entry(first.key)?.receipt
    let slowReceipt = outbox.entry(slow.key)?.receipt
    run.check("receipts are in sequence", firstReceipt?.seq == 1 && slowReceipt?.seq == 2,
              [firstReceipt?.seq ?? -1, slowReceipt?.seq ?? -1])
    run.check("the follow-up is queued behind the running turn", slowReceipt?.state == "queued", slowReceipt?.state ?? "")
    run.check("the chain names the follow-up's predecessor", outbox.entry(slow.key)?.lastAfterMessageID == first.key
              && outbox.entry(first.key)?.lastAfterMessageID == nil)

    state.focus(cid)
    let open = try engine.open(.conversation(cid))
    state.apply(open: open)
    for key in [first.key, slow.key] {
        if let text = outbox.text(of: key) { state.timelines[cid]?.setPersonText(text, for: key) }
    }

    // 3. Follow the events: the first turn completes, the follow-up streams.
    let streamed = follow(cid) { timeline in
        timeline.turn(first.key)?.outcome != nil && (timeline.turn(slow.key)?.isStreaming ?? false)
    }
    let timeline1 = state.timelines[cid]
    try run.require("the first turn completed with its text; the follow-up streams", streamed)
    run.check("delta then text: the first turn's final text",
              timeline1.map { finalText($0, first.key) } == "Fake Claude read 11 characters.",
              timeline1.flatMap { finalText($0, first.key) } ?? NSNull())
    run.check("status phases in order", timeline1?.turn(first.key)?.phases.map(\.phase) == ["starting-provider", "sent", "accepted"],
              timeline1?.turn(first.key)?.phases.map(\.phase) ?? [])
    run.check("served facts from events", timeline1?.turn(first.key)?.served.account != nil,
              timeline1.map { jsonObject($0.turn(first.key)?.served ?? Served()) } ?? NSNull())

    // Another events poll from this app supersedes a waiting one (C-29.9).
    var superseded: EventsPage?
    let background = Thread {
        superseded = try? engine.events(conversationID: cid, after: 1_000_000, wait: 20)
    }
    background.start()
    Thread.sleep(forTimeInterval: 1.0)
    _ = try? engine.events(conversationID: cid, after: state.timelines[cid]?.cursor ?? 0, wait: 0)
    let waitStart = Date()
    while superseded == nil && Date().timeIntervalSince(waitStart) < 10 { Thread.sleep(forTimeInterval: 0.1) }
    var probe = Timeline(conversationID: cid)
    run.check("a newer poll supersedes the waiting one", superseded.map { probe.apply(page: $0) } == .superseded,
              superseded.map { $0.superseded ?? false } ?? NSNull())

    // 4. Stop the follow-up: it is running, so Stop interrupts it.
    refresh([slow.key])
    let action = stopAction(for: slow.key, state: state.timelines[cid]?.turn(slow.key)?.state,
                            outboxEntry: outbox.entry(slow.key))
    run.check("Stop on a running message interrupts it", action == .interrupt(messageID: slow.key), "\(action)")
    let stopReceipt = try engine.stop(action)
    run.check("the interrupt is recorded", stopReceipt?.stop_requested == true)
    let stopped = untilState(slow.key, ["interrupted", "failed", "complete"])
    try run.require("the follow-up ends interrupted (stopped)", stopped?.state == "interrupted" && stopped?.state_reason == "stopped",
                    [stopped?.state ?? "", stopped?.state_reason ?? ""])
    _ = follow(cid, timeout: 20) { $0.turn(slow.key)?.outcome != nil }
    run.check("the stopped turn's text is final", state.timelines[cid]?.turn(slow.key)?.isStreaming == false)
    let afterStop = try engine.open(.conversation(cid))
    state.apply(open: afterStop)
    run.check("an interrupt leaves the conversation unblocked", afterStop.conversation.blocked_by == nil,
              afterStop.conversation.blocked_by ?? NSNull())

    // 5. A tool approval, answered as the development app (person-only).
    let approvalMessage = try engine.send(conversation: cid, text: "run something [fake:approval]", settings: settings)
    _ = engine.pump()
    let carded = follow(cid) { !($0.turn(approvalMessage.key)?.pendingApprovals.isEmpty ?? true) }
    try run.require("the approval card appears from approval.requested", carded)
    // Read at once: the daemon commits an approval with its event (C-27.1), so the list
    // read right after the card appears already has it, and the card's id comes from it.
    let pending = try engine.approvals(conversationID: cid)
    state.apply(approvals: pending, conversationID: cid)
    let card = state.timelines[cid]?.turn(approvalMessage.key)?.pendingApprovals.first
    run.check("the Dock badge counts it", state.pendingApprovalCount == 1, state.pendingApprovalCount)
    try run.require("the card joins its approval id", card?.approvalID != nil && card?.approvalID == pending.first?.approval_id,
                    ["card": card.map(project) ?? NSNull(), "listed": listing(pending)] as [String: Any])
    if let approvalID = card?.approvalID {
        do {
            let detail = try engine.approvalDetail(approvalID)
            run.check("approval.get answers the development app", !detail.nonce.isEmpty && !detail.request_sha256.isEmpty)
            run.check("nothing is masked in this request", detail.masked.isEmpty, detail.masked.count)
            run.check("the card shows the tool and its input", detail.approval.display.tool == "Bash"
                      && (detail.approval.display.input ?? "").contains("echo approved-by-person"),
                      jsonObject(detail.approval.display))
            let answer = try engine.respond(to: detail, decision: "allow")
            try run.require("approval.respond accepted", answer.approval.state == "answered", answer.approval.state)
            let again = try engine.respond(to: detail, decision: "allow")
            run.check("a repeated answer is recognised", again.duplicate == true)
        } catch let stop as LiveStop {
            throw stop
        } catch {
            try run.require("approval.get and approval.respond as the app", false, describe(error))
        }
    }
    let approved = untilState(approvalMessage.key, ["complete", "failed"])
    try run.require("the approved turn completes", approved?.state == "complete", approved?.state ?? "")
    _ = follow(cid, timeout: 20) { $0.turn(approvalMessage.key)?.outcome != nil }
    let approvalTurn = state.timelines[cid]?.turn(approvalMessage.key)
    let resolvedCard = approvalTurn?.items.compactMap { item -> ApprovalCard? in
        if case .approval(let card) = item.content { return card }
        return nil
    }.first
    run.check("the card resolves from approval.resolved", resolvedCard?.state == .answered("allow"),
              resolvedCard.map(project) ?? NSNull())
    let tools = approvalTurn?.items.compactMap { item -> ToolActivity? in
        if case .tool(let tool) = item.content { return tool }
        return nil
    } ?? []
    run.check("the tool call is one activity row, completed", tools.count == 1 && tools.first?.state == .succeeded
              && tools.first?.preview == "approved-by-person", tools.map { ["\($0.name)", $0.state.rawValue, $0.preview ?? ""] })
    run.check("the approved turn's text", state.timelines[cid].flatMap { finalText($0, approvalMessage.key) } == "The command ran.")

    // 6. A question, answered with a chosen label.
    let question = try engine.send(conversation: cid, text: "[fake:question]", settings: settings)
    _ = engine.pump()
    let asked = follow(cid) { !($0.turn(question.key)?.pendingApprovals.isEmpty ?? true) }
    try run.require("the question card appears from approval.requested", asked)
    // Read at once, as for the tool approval (C-27.1).
    let questions = try engine.approvals(conversationID: cid)
    state.apply(approvals: questions, conversationID: cid)
    let questionCard = state.timelines[cid]?.turn(question.key)?.pendingApprovals.first
    try run.require("the question card joins its approval id", questionCard?.approvalID != nil
                    && questionCard?.approvalID == questions.first?.approval_id,
                    ["card": questionCard.map(project) ?? NSNull(), "listed": listing(questions)] as [String: Any])
    try run.require("the question card lists its questions", questionCard?.kind == "question"
              && questionCard?.questions.first?.question == "Which color?"
              && questionCard?.questions.first?.options?.map(\.label) == ["Blue", "Red"], questionCard.map(project) ?? NSNull())
    if let id = questionCard?.approvalID {
        do {
            let detail = try engine.approvalDetail(id)
            let answer = try engine.respond(to: detail, decision: "answer", answers: ["Which color?": "Blue"])
            try run.require("the question is answered as the app", answer.approval.state == "answered", answer.approval.state)
        } catch let stop as LiveStop {
            throw stop
        } catch {
            try run.require("the question is answered as the app", false, describe(error))
        }
    }
    _ = untilState(question.key, ["complete", "failed"])
    _ = follow(cid, timeout: 20) { $0.turn(question.key)?.outcome != nil }
    try run.require("the answer reaches the provider", state.timelines[cid].flatMap { finalText($0, question.key) }
                    == "You chose {\"Which color?\": \"Blue\"}.", state.timelines[cid].flatMap { finalText($0, question.key) } ?? NSNull())

    // 7. Idempotent retry: the app stopped after sending, before the receipt.
    let retried = try engine.send(conversation: cid, text: "idempotent retry", settings: settings)
    guard case .submit(let lost) = try outbox.begin(retried.key) else { throw OutboxError.invalidState("not a submit") }
    let landed = try client.call(Ops.messageSubmit, lost)
    let reopened = try Outbox(url: paths.outboxURL)
    let reopenedEngine = ConversationEngine(client: client, outbox: reopened)
    // From here on the app is the restarted one.
    outbox = reopened
    engine = reopenedEngine
    run.check("after a restart the unanswered send is queued again", reopened.entry(retried.key)?.state == .queued
              && reopened.entry(retried.key)?.attempts == 1)
    let resend = reopenedEngine.pump()
    let resent = reopened.entry(retried.key)?.receipt
    run.check("the resend returns the stored receipt", resend.acknowledged.contains(retried.key) && resent?.created == false
              && resent?.seq == landed.seq, [resent?.created ?? true, resent?.seq ?? -1, landed.seq ?? -2] as [Any])

    // 8. Out of order: the app's predecessor is stale; it re-reads the conversation.
    try reopened.knowChain(cid, lastPersonMessageID: UUID().uuidString.lowercased())
    let ordered = try reopenedEngine.send(conversation: cid, text: "after a stale predecessor", settings: settings)
    let firstTry = reopenedEngine.pump()
    let refused = reopened.entry(ordered.key)
    run.check("out-of-order is kept and resynced", refused?.state == .queued && refused?.failure?.reason == "out-of-order"
              && firstTry.resynced.contains(cid), refused.map(project) ?? NSNull())
    Thread.sleep(forTimeInterval: 0.6)
    _ = reopenedEngine.pump()
    let accepted = reopened.entry(ordered.key)
    run.check("resent after the right predecessor", accepted?.state == .acknowledged
              && accepted?.lastAfterMessageID == retried.key, accepted.map(project) ?? NSNull())

    // 9. Withdraw before a receipt: a send that never reached the daemon.
    let withdrawn = try reopenedEngine.send(conversation: cid, text: "never mind", settings: settings)
    _ = try reopened.begin(withdrawn.key)
    try reopened.finish(withdrawn.key, .failed(.timedOut(op: "message.submit", seconds: 15)))
    let outcome = try reopenedEngine.withdraw(withdrawn.key)
    if case .withdrawn(let tomb) = outcome {
        run.check("withdrawn after message.status found nothing, leaving a tombstone",
                  tomb?.state == "cancelled" && tomb?.state_reason == "withdrawn-before-receipt", tomb.map(jsonObject) ?? NSNull())
    } else {
        run.check("withdrawn after message.status found nothing, leaving a tombstone", false, "\(outcome)")
    }
    let late = try client.call(Ops.messageSubmit, MessageSubmitArgs(
        conversation_id: cid, message_id: withdrawn.key, after_message_id: ordered.key, text: "never mind", settings: settings))
    run.check("a late copy of the withdrawn send cannot land", late.state == "cancelled" && late.created == false,
              jsonObject(late))
    let never = try reopenedEngine.send(conversation: cid, text: "not sent", settings: settings)
    let exchangesBefore = try? String(contentsOfFile: arguments[6]).split(separator: "\n").count
    let local = try reopenedEngine.withdraw(never.key)
    let exchangesAfter = try? String(contentsOfFile: arguments[6]).split(separator: "\n").count
    run.check("a never-sent message is withdrawn locally, with no daemon call", local == .withdrawn(nil)
              && exchangesBefore == exchangesAfter)

    // 10. An image: staged under the app's caches, registered, then sent.
    let staged = try AttachmentStager.stage(image, in: paths.attachmentsDirectory)
    let pictured = try reopenedEngine.send(conversation: cid, text: "look at this", staged: [staged], settings: settings)
    let pictureReport = reopenedEngine.pump()
    try run.require("the image message is accepted after the withdrawn one", pictureReport.acknowledged.contains(pictured.key)
                    && reopened.entry(pictured.key)?.lastAfterMessageID == ordered.key, project(pictureReport))
    _ = untilState(pictured.key, ["complete", "failed"])
    _ = follow(cid, timeout: 30) { $0.turn(pictured.key)?.outcome != nil }
    try run.require("the provider received the image", state.timelines[cid].flatMap { finalText($0, pictured.key) }
                    == "Fake Claude read 12 characters and 1 image(s).", state.timelines[cid].flatMap { finalText($0, pictured.key) } ?? NSNull())

    // 11. History: the transcript's rows of Subfleet turns stay out; their user rows give text.
    state.apply(open: try engine.open(.conversation(cid)))
    let history = try engine.history(conversationID: cid, before: nil, limit: 200)
    state.apply(history: history, conversationID: cid)
    let historyItems = state.timelines[cid]?.items.filter { if case .history = $0.content { return true }; return false } ?? []
    run.check("no Subfleet turn repeats as history", historyItems.isEmpty && !history.items.isEmpty,
              ["history_rows": history.items.count, "shown": historyItems.count])
    let people = state.timelines[cid]?.items.compactMap { item -> String? in
        if case .person(let text, _, _) = item.content { return text ?? "(none)" }
        return nil
    } ?? []
    run.check("the person's messages read in order", Array(people.prefix(2)) == ["hello there", "count [fake:slow]"], people)

    // 12. Notifications for a conversation that is not focused.
    try engine.drainWatch(&state)
    _ = state.drainNotifications()
    let otherRequest = "app-" + UUID().uuidString.lowercased()
    let otherDraft = try engine.createConversation(provider: "claude", workspace: workspace, settings: settings,
                                                   title: "Unfocused", requestID: otherRequest)
    let otherFirst = try engine.send(conversation: otherDraft, text: "[fake:approval]", settings: settings)
    let otherReport = engine.pump()
    state.apply(outbox: otherReport, outbox: outbox)
    let otherID = outbox.entry(otherRequest)?.conversationID ?? ""
    var intents: [NotificationIntent] = []
    let notifyDeadline = Date().addingTimeInterval(60)
    while Date() < notifyDeadline && !intents.contains(where: { $0.kind == .approval }) {
        if let page = try? engine.watch(after: state.watchCursor, wait: 2) { state.apply(watch: page) }
        intents += state.drainNotifications()
    }
    try run.require("an approval in an unfocused conversation notifies",
                    intents.contains { $0.kind == .approval && $0.conversationID == otherID }, intents.map { $0.id })
    run.check("the badge counts the unfocused approval", (state.pendingApprovals[otherID] ?? 0) == 1, state.pendingApprovalCount)
    if let pendingOther = try engine.approvals(conversationID: otherID).first {
        let detail = try engine.approvalDetail(pendingOther.approval_id)
        _ = try engine.respond(to: detail, decision: "deny", message: "not now")
    }
    let completeDeadline = Date().addingTimeInterval(60)
    while Date() < completeDeadline && !intents.contains(where: { $0.kind == .completed && $0.messageID == otherFirst.key }) {
        if let page = try? engine.watch(after: state.watchCursor, wait: 2) { state.apply(watch: page) }
        intents += state.drainNotifications()
    }
    run.check("its completion notifies", intents.contains { $0.kind == .completed && $0.messageID == otherFirst.key },
              intents.map { $0.id })
    run.check("the focused conversation never notified", !intents.contains { $0.conversationID == cid })
    run.check("the badge clears", (state.pendingApprovals[otherID] ?? 0) == 0)

    // 13. The catalog: a native Claude session appears under its prompt and continues here.
    _ = try? engine.refreshCatalog()
    var nativeEntry: SidebarEntry?
    let catalogDeadline = Date().addingTimeInterval(45)
    while Date() < catalogDeadline && nativeEntry == nil {
        if let list = try? engine.list(query: "native probe") {
            state.apply(list: list)
            state.searchQuery = "native probe"
            nativeEntry = state.sidebarEntries().first { if case .native(let ref) = $0.target { return ref.session_id == nativeSession }; return false }
        }
        if nativeEntry == nil { Thread.sleep(forTimeInterval: 1) }
    }
    state.searchQuery = ""
    run.check("the catalog lists the native session under its first prompt", nativeEntry?.title.hasPrefix("native probe") == true,
              nativeEntry.map(project) ?? NSNull())
    if let nativeEntry {
        let nativeOpen = try engine.open(nativeEntry.target)
        state.apply(open: nativeOpen)
        let nativeID = nativeOpen.conversation.conversation_id
        run.check("opening it makes a native conversation bound to the session", nativeOpen.conversation.origin == "native"
                  && nativeOpen.conversation.native_session_id == nativeSession, jsonObject(nativeOpen.conversation))
        let nativeSettings = nativeOpen.conversation.settings
        let continued = try engine.send(conversation: nativeID, text: "continue here", settings: nativeSettings)
        _ = engine.pump()
        let done = untilState(continued.key, ["complete", "failed"])
        run.check("a message continues the native session", done?.state == "complete", done.map(jsonObject) ?? NSNull())
        state.focus(nativeID)
        _ = follow(nativeID, timeout: 20) { $0.turn(continued.key)?.outcome != nil }
        let nativeHistory = try engine.history(conversationID: nativeID, before: nil)
        state.apply(history: nativeHistory, conversationID: nativeID)
        let older = state.timelines[nativeID]?.items.compactMap { item -> String? in
            if case .history(let role, let text, _) = item.content { return "\(role): \(text)" }
            return nil
        } ?? []
        run.check("the native session's earlier turns show as history, before the Subfleet turn",
                  older.first?.hasPrefix("user: native probe") == true && older.count == 2, older)
    }

    // 14. Steer (C-24.9), when the daemon advertises it for Claude: a message sent while a
    // turn runs goes into that turn. Whether the provider takes it at a step or it misses
    // the turn and runs next (DESIGN.md section 4), it settles once and is drawn once.
    if state.availability.capabilities?.canSteer("claude") == true {
        // The step's deadline counts from here: the prerequisite and the steer's submit
        // are inside it (`steerStepDeadline`, #265 review r1).
        let stepStarted = Date()
        let stepBudget = steerStepDeadline(answerTimeout: client.baseTimeout)
        state.focus(cid)
        let host = try engine.send(conversation: cid, text: "steer into me [fake:slow]", settings: settings)
        _ = engine.pump()
        let running = untilState(host.key, ["running", "approval-needed"])
        state.apply(open: try engine.open(.conversation(cid)))
        _ = follow(cid, timeout: 20) { $0.turn(host.key)?.accepted == true }
        let composer = state.conversation(cid).flatMap {
            composerSteerHost(conversation: $0, timeline: state.timelines[cid], capabilities: state.availability.capabilities)
        }
        run.check("steer: the composer steers into the running turn", running != nil && composer?.messageID == host.key,
                  composer?.messageID ?? NSNull())
        // Sent as the app sends it, naming the turn it steers into (C-29.7), and pumped until
        // answered for good. A turn whose runner cannot take a steer yet (still catching up
        // under load, or the provider's capabilities not read yet) answers `not-steerable`: a
        // steer that names its turn is asked again at 0.5 s doubling, where one sent without
        // `into` and read after one pump was refused at once (D-STEER-PUMP1).
        let steered = try engine.send(conversation: cid, text: "also say steered", settings: settings, steer: true,
                                      into: host.key)
        let settling = settleSteer(steered.key, outbox: outbox, deadline: stepStarted.addingTimeInterval(stepBudget),
                                   now: Date.init, pause: { Thread.sleep(forTimeInterval: $0) },
                                   pump: { until in
                                       bounded.deadline = until
                                       defer { bounded.deadline = nil }
                                       state.apply(outbox: engine.pump(), outbox: outbox)
                                   })
        run.check("steer: the daemon takes the steer", steerTaken(settling.steer),
                  ["steer": steerDetail(settling.steer), "pumps": settling.pumps, "waited_s": settling.waited,
                   "step_s": Date().timeIntervalSince(stepStarted), "deadline_s": stepBudget])
        // A slow turn ends only when stopped; a steer it has not taken by then runs next.
        Thread.sleep(forTimeInterval: 3)
        _ = try? engine.stop(.interrupt(messageID: host.key))
        let settled = untilState(steered.key, ["steered", "complete", "failed", "cancelled", "delivery-unknown"],
                                 timeout: 90)
        run.check("steer: the steered message settles once, in the turn or as the next one",
                  ["steered", "complete"].contains(settled?.state ?? ""), settled.map(jsonObject) ?? NSNull())
        run.notes["steer"] = settled?.state ?? "unsettled"
        _ = follow(cid, timeout: 30) { $0.turn(host.key)?.outcome != nil }
        state.apply(open: try engine.open(.conversation(cid)))
        try? engine.catchUp(&state, conversationID: cid)
        let drawn = state.timelines[cid]?.items.filter { $0.id == "person:\(steered.key)" }.count ?? 0
        run.check("steer: the steered message is drawn once", drawn == 1, drawn)
        if settled?.state == "steered" {
            run.check("steer: it is drawn inside the turn that took it",
                      state.timelines[cid]?.placedSteers.contains(steered.key) == true
                      || state.timelines[cid]?.turn(steered.key)?.steeredInto == host.key,
                      state.timelines[cid]?.turn(steered.key).map(project) ?? NSNull())
        }
    } else {
        run.notes["steer"] = "skipped: the daemon does not advertise steer.v1 for claude"
    }
}
