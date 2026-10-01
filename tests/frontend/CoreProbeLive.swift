// `live <app-dir> <workspace> <native-session-id> <image.png> <exchanges.jsonl>`
//
// Drives a real development daemon (SUBFLEET_HOME, fake providers) through the
// app's own engine, outbox and store state, the way the UI will: create a
// conversation with a queued follow-up, follow its events, stop a turn, answer
// a tool approval and a question as the development app, retry and withdraw
// through the outbox, send an image, notify for an unfocused conversation, and
// continue a native session from the catalog. Every exchange is written to
// the exchanges file for the test to check against the daemon's own JSON.
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
    let client = DaemonClient(endpoint: endpoint)
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
    let pending = try engine.approvals(conversationID: cid)
    state.apply(approvals: pending, conversationID: cid)
    let card = state.timelines[cid]?.turn(approvalMessage.key)?.pendingApprovals.first
    run.check("the Dock badge counts it", state.pendingApprovalCount == 1, state.pendingApprovalCount)
    try run.require("the card joins its approval id", card?.approvalID != nil && card?.approvalID == pending.first?.approval_id,
                    card.map(project) ?? NSNull())
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
    let questions = try engine.approvals(conversationID: cid)
    state.apply(approvals: questions, conversationID: cid)
    let questionCard = state.timelines[cid]?.turn(question.key)?.pendingApprovals.first
    try run.require("the question card joins its approval id", questionCard?.approvalID != nil
                    && questionCard?.approvalID == questions.first?.approval_id, questionCard.map(project) ?? NSNull())
    run.check("the question card lists its questions", questionCard?.kind == "question"
              && questionCard?.questions.first?.question == "Which color?"
              && questionCard?.questions.first?.options?.map(\.label) == ["Blue", "Red"], questionCard.map(project) ?? NSNull())
    if let id = questionCard?.approvalID {
        let detail = try engine.approvalDetail(id)
        _ = try engine.respond(to: detail, decision: "answer", answers: ["Which color?": "Blue"])
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
}
