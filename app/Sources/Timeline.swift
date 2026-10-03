// Subfleet: one conversation's timeline, folded from what the daemon reports.
//
// Three sources meet here: receipts (`conversation.open`, `message.submit`,
// `message.status`) say which messages exist and their states; the event log
// (`conversation.events`, design §5) says what each turn did; the native
// transcript (`conversation.history`) holds what came before the conversation's
// first Subfleet turn. Folding rules, per event kind:
//
// - `text.delta` appends to its block; `text` replaces the block and makes it final
//   (a delta after that is ignored). `thinking.delta`/`thinking` likewise; a
//   Codex `thinking` for item B replaces its per-summary blocks `B:<n>` too.
// - `tool.started`/`tool.completed` make one activity row per tool call.
// - `status` phases and `accepted` feed the turn's status strip; `served` merges
//   into the turn's served facts; `limits`, `diff` are kept per turn.
// - `approval.requested` adds a card (it carries the daemon's approval id once
//   `attach` has seen that approval, also when a reset makes the card again);
//   `approval.resolved` answers or withdraws it;
//   `turn.completed` records the outcome and withdraws what is still pending
//   (the driver withdraws pending requests when a turn ends without an event).
// - `steer.delivered` (on the host turn, `data.message_id` the steered message,
//   C-24.9) marks where the provider read a steered message: its bubble, drawn in
//   send order until then, is anchored there, once, and reads "Read".
//   `steer.missed` says the turn ended before reading it (it runs next);
//   `steer.sent` and `steer.refused` change nothing shown.
// - `reset: true` means compacted deltas were missed: the event-derived state is
//   dropped and the log is read again from 0 (C-25.4).
//
// Receipts own message states; events only move them forward optimistically
// (`accepted` → running, `approval.requested` → approval-needed). Foundation only.

import Foundation

struct PhaseStamp: Equatable {
    var phase: String
    var ts: String?
}

struct ToolActivity: Equatable {
    enum State: String { case running, succeeded, failed, unfinished }
    var toolID: String?
    var name: String
    var summary: String
    var hidden: Bool
    var state: State
    var preview: String?
}

struct ApprovalCard: Equatable {
    enum State: Equatable {
        case pending
        case answered(String?)
        case withdrawn
    }
    /// The provider's request id, from the `approval.requested` event.
    var requestID: String?
    /// The daemon's approval id, from `approval.list` or `conversation.open`
    /// (the event does not carry it; docs/desktop/app-needs.md).
    var approvalID: String?
    var kind: String
    var display: ApprovalDisplay
    var options: [String]
    var state: State

    var isPending: Bool { state == .pending }
    /// An event summary alone cannot identify the approval the person answers.
    var isActionable: Bool { isPending && approvalID != nil }
    var questions: [ApprovalQuestion] { display.questions }
}

struct TurnOutcome: Equatable {
    var state: String
    var reason: String?
    var detail: String?
    var servedModel: String?
    var data: JSONValue
}

enum TimelineContent: Equatable {
    /// A native transcript row from before the conversation's Subfleet turns.
    case history(role: String, text: String, tool: String?)
    /// The person's message (text unknown when neither this app nor the transcript has it).
    case person(text: String?, attachments: [String], state: String)
    case text(String, final: Bool)
    case thinking(String, final: Bool)
    case tool(ToolActivity)
    case approval(ApprovalCard)
    case error(message: String, kind: String?, willRetry: Bool)
    case notice(String)
    /// Where the provider took a steered message into this turn (`steer.delivered`).
    /// `Timeline.items` draws that message's bubble here instead, so it is never shown.
    case steered(messageID: String)
}

struct TimelineItem: Identifiable, Equatable {
    var id: String
    var messageID: String?
    var content: TimelineContent
    var ts: String?

    /// The card, when this row is an approval still waiting for the person.
    var pendingCard: ApprovalCard? {
        if case .approval(let card) = content, card.isPending { return card }
        return nil
    }

    /// The card, when this row is an approval in any state.
    var card: ApprovalCard? {
        if case .approval(let card) = content { return card }
        return nil
    }
}

/// The pinned strip's Review button: "Review", or "Review (N)" while several
/// cards wait; nil, and no button, while none does.
func reviewButtonLabel(pending: Int) -> String? {
    pending <= 0 ? nil : pending == 1 ? "Review" : "Review (\(pending))"
}

/// Whether the strip's Review opens the request sheet for `card`, besides
/// bringing it into view: only for a tool request. A question is answered on its
/// own inline card, and the sheet has no form for its answers (C-27.2, C-27.5).
func reviewOpensRequestSheet(_ card: ApprovalCard) -> Bool {
    card.kind != "question"
}

/// The sidebar's hand badge, spoken: "1 approval waiting", "3 approvals waiting".
func approvalsWaitingWords(_ count: Int) -> String {
    "\(count) approval\(count == 1 ? "" : "s") waiting"
}

/// One message and its turn.
struct TurnTimeline: Equatable {
    var messageID: String
    var seq: Int?
    var origin: String?
    var continues: String?
    var personText: String?
    var attachments: [String] = []
    /// A message state, or `sending` before the first receipt.
    var state: String
    var stateReason: String?
    var settings: ConversationSettings?
    var stopRequested = false
    var phases: [PhaseStamp] = []
    /// When the person last answered an approval: a tool waiting on it starts then.
    var answeredTS: String?
    var accepted = false
    var eventServed = Served()
    var receiptServed: Served?
    var outcome: TurnOutcome?
    var limits: JSONValue?
    var diff: String?
    var items: [TimelineItem] = []
    /// C-24.9: the host this message was steered into (`steered_into`, or its
    /// `state_reason`), while it is steering or steered.
    var steeredInto: String?
    /// The host whose `steer.delivered` placed this message: the provider read it.
    var steerDeliveredIn: String?
    /// The host whose `steer.missed` said its turn ended before reading this
    /// message. It describes that steer only: steered again into another turn,
    /// the message is unread there until that turn says otherwise.
    var steerMissedIn: String?
    /// This app journaled a steer of it and the daemon has not answered yet.
    var steerRequested = false
    /// The daemon's refusal of this app's last steer of it (the message stayed queued).
    var steerRefusal: OutboxFailure?
    /// The sequence number of the turn's first event: when it began running.
    /// Compaction removes only deltas (C-25.4), so a re-read finds the same one.
    var firstEventSeq: Int?

    /// Events first, then the settled receipt's facts (lane, served model).
    var served: Served { eventServed.merging(receiptServed) }
    var messageState: MessageState? { MessageState(rawValue: state) }
    var pendingApprovals: [ApprovalCard] {
        items.compactMap { if case .approval(let card) = $0.content, card.isPending { return card } else { return nil } }
    }
    var isStreaming: Bool {
        items.contains {
            switch $0.content {
            case .text(_, let final), .thinking(_, let final): return !final
            default: return false
            }
        }
    }

    /// The newest tool call still running, if any.
    var runningTool: ToolActivity? {
        for item in items.reversed() {
            if case .tool(let tool) = item.content, tool.state == .running { return tool }
        }
        return nil
    }

    /// When the status strip's current words started: the newest phase stamp,
    /// or the running tool's row. A live strip counts up from here.
    /// A stop was asked for: every later phase is the provider winding down.
    var stopping: Bool { stopRequested || phases.contains { $0.phase == "stopping" } }

    /// It follows the same order as `statusText`'s words; nil where no clock
    /// fits (the provider has answered, or the turn is not live).
    var statusSince: Date? {
        if state == MessageState.approvalNeeded.rawValue {
            return items.last(where: { if case .approval(let card) = $0.content { return card.isPending } else { return false } })?
                .ts.flatMap(parseTimestamp)
        }
        if stopping {
            return phases.last(where: { $0.phase == "stopping" })?.ts.flatMap(parseTimestamp)
        }
        if outcome != nil { return nil }
        if let item = items.last(where: { if case .tool(let t) = $0.content { return t.state == .running } else { return false } }) {
            let started = item.ts.flatMap(parseTimestamp)
            let answered = answeredTS.flatMap(parseTimestamp)
            return [started, answered].compactMap { $0 }.max()
        }
        return phases.last?.ts.flatMap(parseTimestamp)
    }

    /// Drawn inside its host turn: the host's `steer.delivered` placed it, and it
    /// is steering or steered (or has no receipt yet). One sent back to the queue
    /// is drawn in its own place, where it runs next.
    var isPlacedSteer: Bool {
        steerDeliveredIn != nil && (messageState == .steering || messageState == .steered || state == "sending")
    }

    /// A steer the provider has not read: asked for, handed over, or back in the
    /// queue after the turn ended without reading it (it runs next).
    var isUnreadSteer: Bool {
        guard steerDeliveredIn == nil else { return false }
        switch messageState {
        case .steering: return true
        case .queued: return steerRequested || missedSteer
        case nil: return state == "sending" && steerRequested
        default: return false
        }
    }

    /// The turn ended before reading it: `steer-missed:` on its receipt, or the
    /// `steer.missed` of the turn it is (or was last) steered into.
    var missedSteer: Bool {
        if stateReason?.hasPrefix("steer-missed:") == true { return true }
        guard let missedIn = steerMissedIn else { return false }
        return messageState == .steering ? steeredInto == missedIn : messageState == .queued && !steerRequested
    }

    /// Read by the provider: its `steer.delivered`, or settled `steered`.
    var isReadSteer: Bool {
        messageState == .steered || (steerDeliveredIn != nil && (messageState == .steering || state == "sending"))
    }

    /// The status strip's words for where this turn is (design §12).
    var statusText: String { statusText(host: nil) }

    /// The words, with a steered message's read state taken from the turn it
    /// joins (C-24.9; DESIGN.md sections 8 and 9: Claude Code's words).
    func statusText(host: TurnTimeline?, assistant: String = "Claude") -> String {
        switch messageState {
        case .queued:
            if steerRequested { return TurnTimeline.unreadWords(host: host, assistant: assistant) }
            if missedSteer { return TurnTimeline.unreadUntilTurnEnds }
            if let refusal = steerRefusal { return "Queued, not steered: " + steerRefusalWords(refusal) }
            return "Queued behind the current turn"
        case .steering:
            if steerDeliveredIn != nil { return TurnTimeline.read }
            if missedSteer { return TurnTimeline.unreadUntilTurnEnds }
            return TurnTimeline.unreadWords(host: host, assistant: assistant)
        case .steered:
            // Codex recorded it after the model's last step: nothing answered it.
            return stateReason?.hasPrefix("steered-unanswered:") == true
                ? "Read after the turn's last step; ask again for a reply" : TurnTimeline.read
        case .waiting:
            if let reason = stateReason, reason.contains("external-writer") {
                // C-26.3, D-17: another Claude process holds the session.
                return "Waiting: open in the Claude app or a terminal; close it there to continue here"
            }
            if let reason = stateReason, !reason.isEmpty { return "Waiting: \(reason)" }
            return "Waiting for capacity"
        case .starting, .running, .approvalNeeded:
            if state == MessageState.approvalNeeded.rawValue { return "Needs your approval" }
            if stopping { return "Stopping" }
            // The provider has answered; its process is still winding down.
            if outcome != nil { return "Finishing" }
            // A tool the provider is running outranks the block that asked for it.
            if let tool = runningTool { return tool.hidden ? "Running a tool" : "Running \(tool.name)" }
            switch phases.last?.phase {
            case "starting-provider": return "Starting the provider"
            case "opening-thread": return "Opening the thread"
            case "sent": return "Sent; waiting for the provider"
            case "accepted": return "Running"
            case "requesting", "compacted": return "Waiting for the model"
            case "compacting": return "Compacting the conversation"
            case "thinking": return "Thinking"
            case "writing": return "Writing"
            case "preparing-tool": return "Preparing a tool call"
            case "tool": return "Running"
            default: return state == "starting" ? "Starting" : "Running"
            }
        case .complete: return stateReason == "stop-too-late" ? "Completed before the stop took effect" : "Completed"
        case .failed: return "Failed" + (stateReason.map { ": \($0)" } ?? "")
        case .interrupted: return "Stopped"
        case .cancelled: return "Withdrawn"
        case .deliveryUnknown: return "Delivery unknown: choose whether it was delivered"
        case .unknown: return "Not received by the daemon"
        case nil:
            guard state == "sending" else { return state }
            if steerDeliveredIn != nil { return TurnTimeline.read }
            return steerRequested ? TurnTimeline.unreadWords(host: host, assistant: assistant) : "Sending"
        }
    }

    static let read = "Read"
    static let unreadUntilTurnEnds = "Unread until the current turn ends."

    /// An unread steer waits for what the running turn is doing: an approval, a
    /// tool that is running, or else the model's next step.
    static func unreadWords(host: TurnTimeline?, assistant: String) -> String {
        if let host, host.messageState == .approvalNeeded || !host.pendingApprovals.isEmpty {
            return "Unread. \(assistant) needs your approval first."
        }
        if host?.runningTool != nil { return "Unread until the current step finishes." }
        return "Unread until \(assistant)'s next step."
    }
}

/// Why the daemon would not steer a message, as its status line says it (design §6).
func steerRefusalWords(_ failure: OutboxFailure) -> String {
    switch failure.reason {
    case "not-queued": return "it had already left the queue"
    case "not-next": return "a recovery message goes first"
    case "no-live-turn": return "the turn it was sent to had ended"
    case "settings-narrower": return "it asks for a narrower permission than the running turn has"
    case "not-steerable": return "the running turn could not take it"
    case "unsupported": return "this daemon cannot steer"
    case "no-answer": return "the daemon did not answer"
    default: return failure.message
    }
}

struct Timeline: Equatable {
    /// Events whose message id is null are kept under this key.
    static let conversationKey = "(conversation)"

    let conversationID: String
    /// The last event sequence folded in; the next poll's `after`.
    private(set) var cursor = 0
    private(set) var turns: [String: TurnTimeline] = [:]
    /// Message ids in display order: by daemon sequence, then arrival.
    private(set) var order: [String] = []
    private var arrival: [String: Int] = [:]
    /// Transcript items older than the first Subfleet turn, oldest first.
    private(set) var history: [TimelineItem] = []
    private(set) var historyBefore: Int?
    private(set) var historyComplete = false
    private(set) var historyPagesLoaded = 0
    /// When the conversation's first event was written: the transcript at or
    /// after it belongs to Subfleet turns, which the events already show.
    private(set) var firstEventTS: String?
    private(set) var resets = 0
    /// Re-reading the log from 0 after a reset, until the cursor reaches the floor.
    private(set) var rebuilding = false
    private(set) var unknownKinds: [String: Int] = [:]
    /// Every approval `attach` has seen, by approval id. A card the events make
    /// again (after a reset re-reads the log) gets its approval id back from here.
    private var knownApprovals: [String: ApprovalView] = [:]
    /// Whether the log has been read to its end since the timeline began or last
    /// reset (a page that added nothing): until then rows still arrive above
    /// whatever the view scrolled to.
    private(set) var caughtUp = false

    init(conversationID: String) {
        self.conversationID = conversationID
    }

    enum PageResult: Equatable {
        case applied(Int)
        /// The cursor fell behind the compaction floor: poll again from 0.
        case reset
        /// A newer poll from this app replaced this one; stop this loop.
        case superseded
    }

    // MARK: Events

    /// A new read of the log begins (the conversation opened again): until it
    /// reaches the end, rows may still arrive above what the view shows.
    mutating func startReading() {
        caughtUp = false
    }

    @discardableResult
    mutating func apply(page: EventsPage) -> PageResult {
        if page.superseded == true { return .superseded }
        // C-25.4: `reset` is true exactly while the cursor is below the floor, so
        // the re-read from 0 answers `reset` too until it reaches the floor. Only
        // the first one drops what the events produced; the pages of the re-read
        // are the surviving log and are applied.
        if page.reset && !rebuilding {
            resetEvents()
            rebuilding = true
            return .reset
        }
        let count = apply(events: page.events)
        cursor = max(cursor, page.next)
        if rebuilding && (page.events.isEmpty || !page.reset || page.floor.map { cursor >= $0 } == true) {
            rebuilding = false
        }
        if count == 0 && !rebuilding && !page.reset { caughtUp = true }
        return .applied(count)
    }

    /// Drop everything the event log produced; receipts and the person's text stay.
    mutating func resetEvents() {
        cursor = 0
        resets += 1
        caughtUp = false
        firstEventTS = nil
        for id in order {
            guard var turn = turns[id] else { continue }
            turn.phases = []
            turn.answeredTS = nil
            turn.accepted = false
            turn.eventServed = Served()
            turn.outcome = nil
            turn.limits = nil
            turn.diff = nil
            turn.items = []
            turn.steerDeliveredIn = nil
            turn.steerMissedIn = nil
            turn.firstEventSeq = nil
            turns[id] = turn
        }
        order.removeAll { $0 == Timeline.conversationKey }
        turns[Timeline.conversationKey] = nil
    }

    @discardableResult
    mutating func apply(events: [ConversationEvent]) -> Int {
        var applied = 0
        for event in events.sorted(by: { $0.seq < $1.seq }) where event.seq > cursor {
            fold(event)
            cursor = event.seq
            applied += 1
        }
        return applied
    }

    private mutating func ensureTurn(_ id: String) {
        guard turns[id] == nil else { return }
        turns[id] = TurnTimeline(messageID: id, state: "sending")
        arrival[id] = arrival.count
        order.append(id)
        sortOrder()
    }

    private mutating func sortOrder() {
        order.sort { a, b in
            let sa = turns[a]?.seq ?? Int.max, sb = turns[b]?.seq ?? Int.max
            if sa != sb { return sa < sb }
            return (arrival[a] ?? 0) < (arrival[b] ?? 0)
        }
    }

    private mutating func fold(_ event: ConversationEvent) {
        let id = event.message_id ?? Timeline.conversationKey
        ensureTurn(id)
        guard var turn = turns[id] else { return }
        turn.firstEventSeq = turn.firstEventSeq ?? event.seq
        if firstEventTS == nil, let ts = event.ts {
            firstEventTS = ts
            trimHistory()
        }
        let data = event.data
        // A steered message this event says the provider read, or did not (`steer.*`).
        var delivered: String?
        var missed: String?
        switch event.kind {
        case "status":
            // A new attempt starts its own clock, even after one that never got further.
            if let phase = data["phase"]?.string, turn.phases.last?.phase != phase || phase == "starting-provider" {
                turn.phases.append(PhaseStamp(phase: phase, ts: event.ts))
            }
            if data["phase"]?.string == "compacted" {
                // The provider replaced the earlier conversation with a summary.
                var words = "Compacted the conversation"
                if let before = data["pre_tokens"]?.double, let after = data["post_tokens"]?.double {
                    words += " (\(tokenWords(before)) → \(tokenWords(after)) tokens)"
                }
                turn.items.append(TimelineItem(id: "compacted:\(event.seq)", messageID: id,
                                               content: .notice(words + "; the model now works from a summary of it"),
                                               ts: event.ts))
            }
        case "accepted":
            turn.accepted = true
            if turn.phases.last?.phase != "accepted" { turn.phases.append(PhaseStamp(phase: "accepted", ts: event.ts)) }
            if ["sending", "queued", "waiting", "starting"].contains(turn.state) {
                turn.state = MessageState.running.rawValue
            }
        case "served":
            let served = Served(fields: data.object ?? [:])
            turn.eventServed = turn.eventServed.merging(served)
            if let warning = served.fast_warning {
                turn.items.append(TimelineItem(id: "notice:\(id):\(event.seq)", messageID: id,
                                               content: .notice("Fast is off for this turn: \(warning)"), ts: event.ts))
            }
        case "text.delta", "text":
            stream(&turn, kind: "text", block: data["block"]?.displayText ?? "", text: data["text"]?.string ?? "",
                   final: event.kind == "text", ts: event.ts)
        case "thinking.delta", "thinking":
            stream(&turn, kind: "thinking", block: data["block"]?.displayText ?? "", text: data["text"]?.string ?? "",
                   final: event.kind == "thinking", ts: event.ts)
        case "tool.started":
            let toolID = data["id"]?.string
            let itemID = "tool:\(id):" + (toolID ?? "seq\(event.seq)")
            let activity = ToolActivity(toolID: toolID, name: data["name"]?.string ?? "tool",
                                        summary: data["summary"]?.string ?? "", hidden: data["hidden"]?.bool ?? false,
                                        state: .running, preview: nil)
            if let index = turn.items.firstIndex(where: { $0.id == itemID }), case .tool(var existing) = turn.items[index].content {
                existing.name = activity.name
                existing.summary = activity.summary
                existing.hidden = activity.hidden
                turn.items[index].content = .tool(existing)
            } else {
                turn.items.append(TimelineItem(id: itemID, messageID: id, content: .tool(activity), ts: event.ts))
            }
        case "tool.completed":
            let toolID = data["id"]?.string
            let itemID = "tool:\(id):" + (toolID ?? "seq\(event.seq)")
            let failed = data["is_error"]?.bool ?? false
            let hidden = data["hidden"]?.bool ?? false
            if let index = turn.items.firstIndex(where: { $0.id == itemID }), case .tool(var existing) = turn.items[index].content {
                existing.state = failed ? .failed : .succeeded
                existing.preview = data["preview"]?.string
                existing.hidden = existing.hidden || hidden
                turn.items[index].content = .tool(existing)
            } else {
                let activity = ToolActivity(toolID: toolID, name: "tool", summary: "", hidden: hidden,
                                            state: failed ? .failed : .succeeded, preview: data["preview"]?.string)
                turn.items.append(TimelineItem(id: itemID, messageID: id, content: .tool(activity), ts: event.ts))
            }
        case "approval.requested":
            var fields = data.object ?? [:]
            let requestID = fields.removeValue(forKey: "request_id")?.displayText
            let kind = fields.removeValue(forKey: "kind")?.string ?? "tool"
            let options = fields.removeValue(forKey: "options")?.array?.compactMap(\.string) ?? []
            let display = ApprovalDisplay(fields: fields)
            // Known already from a view that carries its request id: the card moves to where
            // the request came, below what the turn did before asking. A view's request id
            // joins only its own request: a replacement with identical display text never
            // takes a stale card's place (C-27.1).
            if let requestID, let index = turn.items.firstIndex(where: { $0.card?.requestID == requestID }) {
                // This event read again: nothing to do.
                guard turn.items[index].id != "approval:\(id):\(requestID)" else { break }
                var row = turn.items.remove(at: index)
                row.ts = event.ts ?? row.ts
                turn.items.append(row)
            } else {
                let card = ApprovalCard(requestID: requestID,
                                        approvalID: knownApprovalID(in: turn, requestID: requestID),
                                        kind: kind, display: display, options: options, state: .pending)
                turn.items.append(TimelineItem(id: "approval:\(id):\(requestID ?? "seq\(event.seq)")", messageID: id,
                                               content: .approval(card), ts: event.ts))
            }
            // Only forward: a message the daemon has settled (ended, delivery unknown) stays so.
            if !(turn.messageState.map { $0.isTerminal || $0 == .deliveryUnknown } ?? false) {
                turn.state = MessageState.approvalNeeded.rawValue
            }
        case "approval.resolved":
            let requestID = data["request_id"]?.displayText
            let decision = data["decision"]?.string
            for index in turn.items.indices {
                guard case .approval(var card) = turn.items[index].content, card.requestID == requestID else { continue }
                card.state = decision == "withdrawn" ? .withdrawn : .answered(decision)
                turn.items[index].content = .approval(card)
            }
            turn.answeredTS = event.ts ?? turn.answeredTS
            if turn.pendingApprovals.isEmpty && turn.state == MessageState.approvalNeeded.rawValue {
                turn.state = MessageState.running.rawValue
            }
        case "turn.completed":
            turn.outcome = TurnOutcome(state: data["state"]?.string ?? "unknown", reason: data["reason"]?.string,
                                       detail: data["detail"]?.string, servedModel: data["served_model"]?.string,
                                       data: data)
            if let model = data["served_model"]?.string, turn.eventServed.model == nil {
                turn.eventServed.fields["model"] = .string(model)
            }
            for index in turn.items.indices {
                switch turn.items[index].content {
                case .approval(var card) where card.isPending:
                    card.state = .withdrawn
                    turn.items[index].content = .approval(card)
                case .tool(var tool) where tool.state == .running:
                    tool.state = .unfinished
                    turn.items[index].content = .tool(tool)
                case .text(let text, false):
                    turn.items[index].content = .text(text, final: true)
                case .thinking(let text, false):
                    turn.items[index].content = .thinking(text, final: true)
                default:
                    break
                }
            }
        case "error":
            turn.items.append(TimelineItem(id: "error:\(event.seq)", messageID: id,
                                           content: .error(message: data["message"]?.string ?? "error",
                                                           kind: data["kind"]?.string,
                                                           willRetry: data["will_retry"]?.bool ?? false),
                                           ts: event.ts))
        case "limits":
            turn.limits = data
            if data["status"]?.string == "rejected" {
                var words = "Usage limit reached"
                if let type = data["type"]?.string { words += " (\(type.replacingOccurrences(of: "_", with: " ")))" }
                if let resets = data["resets_at"]?.double {
                    words += "; resets " + ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: resets))
                }
                turn.items.append(TimelineItem(id: "limits:\(event.seq)", messageID: id, content: .notice(words),
                                               ts: event.ts))
            }
        case "diff":
            turn.diff = data["diff"]?.string
        case "steer.delivered":
            // C-24.9: the provider took the steered message at this point of the turn.
            guard id != Timeline.conversationKey, let steered = data["message_id"]?.string, steered != id,
                  steered != Timeline.conversationKey else { break }
            let itemID = "steer:\(steered)"
            if !turn.items.contains(where: { $0.id == itemID }) {
                turn.items.append(TimelineItem(id: itemID, messageID: steered, content: .steered(messageID: steered),
                                               ts: event.ts))
            }
            delivered = steered
        case "steer.missed":
            // The turn ended before reading it: it goes back to the queue and runs next.
            if id != Timeline.conversationKey, let steered = data["message_id"]?.string, steered != id { missed = steered }
        case "steer.sent", "steer.refused":
            // The steered message's receipts say the rest.
            break
        default:
            unknownKinds[event.kind, default: 0] += 1
        }
        Timeline.withdrawIfEnded(&turn)
        turns[id] = turn
        if let delivered {
            ensureTurn(delivered)
            turns[delivered]?.steerDeliveredIn = id
        }
        if let missed, turns[missed]?.steerDeliveredIn == nil {
            ensureTurn(missed)
            turns[missed]?.steerMissedIn = id
        }
    }

    /// A turn whose message has ended has no pending card: the daemon withdraws
    /// an attempt's approvals before it settles the message (C-27.3), and a turn
    /// that ends without `result` writes no event withdrawing its requests. A
    /// message whose delivery is unknown was settled the same way.
    private static func withdrawIfEnded(_ turn: inout TurnTimeline) {
        guard let state = turn.messageState, state.isTerminal || state == .deliveryUnknown else { return }
        for index in turn.items.indices {
            if case .approval(var card) = turn.items[index].content, card.isPending {
                card.state = .withdrawn
                turn.items[index].content = .approval(card)
            }
        }
    }

    private func stream(_ turn: inout TurnTimeline, kind: String, block: String, text: String, final: Bool, ts: String?) {
        let itemID = "\(kind):\(turn.messageID):\(block)"
        func content(_ value: String, _ done: Bool) -> TimelineContent {
            kind == "text" ? .text(value, final: done) : .thinking(value, final: done)
        }
        func current(_ item: TimelineItem) -> (String, Bool)? {
            switch item.content {
            case .text(let value, let done) where kind == "text": return (value, done)
            case .thinking(let value, let done) where kind == "thinking": return (value, done)
            default: return nil
            }
        }
        if final {
            // Codex reasoning streams per summary part (`B:<n>`) and completes as `B`.
            let prefix = itemID + ":"
            let matches = turn.items.indices.filter { turn.items[$0].id == itemID || turn.items[$0].id.hasPrefix(prefix) }
            if let first = matches.first {
                turn.items[first].id = itemID
                turn.items[first].content = content(text, true)
                for index in matches.dropFirst().reversed() { turn.items.remove(at: index) }
            } else {
                turn.items.append(TimelineItem(id: itemID, messageID: turn.messageID, content: content(text, true), ts: ts))
            }
            return
        }
        if let index = turn.items.firstIndex(where: { $0.id == itemID }), let (value, done) = current(turn.items[index]) {
            if !done { turn.items[index].content = content(value + text, false) }
        } else {
            turn.items.append(TimelineItem(id: itemID, messageID: turn.messageID, content: content(text, false), ts: ts))
        }
    }

    // MARK: Receipts and the person's own messages

    mutating func apply(receipts: [Receipt]) {
        for receipt in receipts { apply(receipt: receipt) }
    }

    mutating func apply(receipt: Receipt) {
        guard receipt.conversation_id == nil || receipt.conversation_id == conversationID else { return }
        ensureTurn(receipt.message_id)
        guard var turn = turns[receipt.message_id] else { return }
        if receipt.messageState != .unknown {
            turn.state = receipt.state
            turn.stateReason = receipt.state_reason
            turn.steeredInto = receipt.steered_into ?? steeredInto(stateReason: receipt.state_reason)
            // A queued receipt may be its submit's, ahead of its steer's answer.
            if receipt.messageState != .queued { turn.steerRequested = false }
            if receipt.messageState == .steering || receipt.messageState == .steered { turn.steerRefusal = nil }
        }
        turn.seq = receipt.seq ?? turn.seq
        turn.origin = receipt.origin ?? turn.origin
        turn.continues = receipt.continues ?? turn.continues
        turn.settings = receipt.settings ?? turn.settings
        turn.stopRequested = receipt.stop_requested ?? turn.stopRequested
        if let served = receipt.served { turn.receiptServed = served }
        if turn.personText == nil, let text = receipt.text {
            turn.personText = text + (receipt.text_truncated == true ? "\n…" : "")
        }
        Timeline.withdrawIfEnded(&turn)
        turns[receipt.message_id] = turn
        sortOrder()
    }

    /// A message this app is sending: shown at once, before its receipt (the
    /// composer's optimistic row). With `steer`, sent to steer the running turn.
    mutating func addLocal(messageID: String, text: String, attachments: [String] = [],
                           settings: ConversationSettings? = nil, steer: Bool = false) {
        ensureTurn(messageID)
        guard var turn = turns[messageID] else { return }
        turn.personText = text
        turn.attachments = attachments
        turn.origin = turn.origin ?? "person"
        turn.settings = turn.settings ?? settings
        if steer { turn.steerRequested = true }
        turns[messageID] = turn
    }

    /// The person asked to steer this message (C-24.9): its status line says so
    /// until the daemon answers.
    mutating func requestSteer(messageID: String) {
        guard var turn = turns[messageID] else { return }
        turn.steerRequested = true
        turn.steerRefusal = nil
        turns[messageID] = turn
    }

    /// The daemon answered this app's steer: `refusal` nil when it took the
    /// message (or the steer was taken back), else why not (the message stays queued).
    mutating func noteSteer(messageID: String, refusal: OutboxFailure?) {
        guard var turn = turns[messageID] else { return }
        turn.steerRequested = false
        turn.steerRefusal = refusal
        turns[messageID] = turn
    }

    /// The person's text from somewhere other than the event log (the outbox).
    mutating func setPersonText(_ text: String, for messageID: String) {
        guard var turn = turns[messageID], turn.personText == nil else { return }
        turn.personText = text
        turns[messageID] = turn
    }

    /// Mark a local message the person withdrew before the daemon had it.
    mutating func withdrawLocal(messageID: String) {
        guard var turn = turns[messageID] else { return }
        turn.state = MessageState.cancelled.rawValue
        turn.stateReason = "withdrawn-before-receipt"
        turns[messageID] = turn
    }

    // MARK: Approvals

    /// Join the daemon's approvals (which carry `approval_id`) to the cards the
    /// events made (which carry the provider's request id): by message and exact
    /// request id. Summaries cannot identify a request (C-27.1). A legacy view
    /// without that id gets its own card, keyed by its immutable approval id.
    mutating func attach(approvals: [ApprovalView]) {
        for approval in approvals where approval.conversation_id == conversationID {
            knownApprovals[approval.approval_id] = approval
            ensureTurn(approval.message_id)
            guard var turn = turns[approval.message_id] else { continue }
            let state: ApprovalCard.State = approval.state == "pending" ? .pending
                : approval.state == "withdrawn" ? .withdrawn : .answered(nil)
            if let index = turn.items.firstIndex(where: {
                if case .approval(let card) = $0.content { return card.approvalID == approval.approval_id }
                return false
            }), case .approval(var card) = turn.items[index].content {
                if card.requestID == nil { card.requestID = approval.requestID }
                if card.isPending { card.state = state }
                turn.items[index].content = .approval(card)
            } else if let index = turn.items.firstIndex(where: { item in
                guard case .approval(let card) = item.content, card.approvalID == nil else { return false }
                guard let requestID = approval.requestID else { return false }
                return card.requestID == requestID && card.kind == approval.kind && card.display == approval.display
                    && (card.isPending || approval.state != "pending")
            }), case .approval(var card) = turn.items[index].content {
                card.approvalID = approval.approval_id
                card.requestID = card.requestID ?? approval.requestID
                if card.isPending { card.state = state }
                turn.items[index].content = .approval(card)
            } else if approval.state == "pending" {
                let card = ApprovalCard(requestID: approval.requestID, approvalID: approval.approval_id,
                                        kind: approval.kind, display: approval.display, options: approval.options,
                                        state: .pending)
                let rowID = "approval:\(approval.message_id):\(approval.approval_id)"
                turn.items.append(TimelineItem(id: rowID, messageID: approval.message_id, content: .approval(card),
                                               ts: approval.created_at))
            }
            Timeline.withdrawIfEnded(&turn)
            turns[approval.message_id] = turn
        }
    }

    /// The daemon's whole pending set for this conversation (`approval.list`,
    /// `conversation.open`): a card joined to an approval it no longer lists is
    /// withdrawn, since an approval never returns to pending. An event card that
    /// the list cannot identify is withdrawn too: legacy views remain actionable
    /// on their own cards, never on an old event card's drafted answers. That covers
    /// an attempt that ended without `result`, or was admitted again.
    mutating func reconcile(pending approvals: [ApprovalView]) {
        let listed = Set(approvals.filter { $0.conversation_id == conversationID && $0.state == "pending" }
            .map(\.approval_id))
        for id in order {
            guard var turn = turns[id] else { continue }
            var changed = false
            for index in turn.items.indices {
                guard case .approval(var card) = turn.items[index].content, card.isPending,
                      card.approvalID.map({ !listed.contains($0) }) ?? true else { continue }
                card.state = .withdrawn
                turn.items[index].content = .approval(card)
                changed = true
            }
            if changed { turns[id] = turn }
        }
    }

    /// The id of a known approval for an event card: same message and exact
    /// request id, not already held by another card. Display text is not identity.
    private func knownApprovalID(in turn: TurnTimeline, requestID: String?) -> String? {
        guard let requestID else { return nil }
        let held = Set(turn.items.compactMap { item -> String? in
            if case .approval(let card) = item.content { return card.approvalID }
            return nil
        })
        return knownApprovals.values.first {
            $0.message_id == turn.messageID && $0.requestID == requestID && !held.contains($0.approval_id)
        }?.approval_id
    }

    /// Every card still waiting for the person.
    var pendingApprovalCards: [ApprovalCard] { order.flatMap { turns[$0]?.pendingApprovals ?? [] } }

    // MARK: Pending approvals within reach

    /// Every pending card's row, oldest asked first (by its time, then where it
    /// sits; a row with no time after those with one): the rows the pinned
    /// strip's Review opens in turn, and the ones the view scrolls to.
    var pendingApprovalItems: [TimelineItem] {
        let rows = displayOrder.flatMap { id in (turns[id]?.items ?? []).filter { $0.pendingCard != nil } }
        return rows.enumerated()
            .map { (row: $0.element, place: $0.offset, at: $0.element.ts.flatMap(parseTimestamp)) }
            .sorted { a, b in
                switch (a.at, b.at) {
                case let (x?, y?) where x != y: return x < y
                case (.some, nil): return true
                case (nil, .some): return false
                default: return a.place < b.place
                }
            }
            .map(\.row)
    }

    /// The turn the strip pinned above the composer shows: the newest live turn
    /// the provider has not answered, or else the turn of the oldest pending
    /// card, so a card waiting on the person always has the strip's Review.
    var pinnedTurn: TurnTimeline? {
        if let live = liveMessageID, let turn = turns[live], turn.outcome == nil { return turn }
        return pendingApprovalItems.first?.messageID.flatMap { turns[$0] }
    }

    /// The row the conversation scrolls to for an approval: the oldest pending
    /// card not yet brought into view (`shown`), so each card is scrolled to
    /// once, when it appears, wherever the person was reading; or, when the
    /// person asked (`reveal`: the strip's Review, the sidebar badge), the
    /// oldest pending card. Nil when no card waits.
    func approvalScrollTarget(shown: Set<String>, reveal: Bool) -> String? {
        let pending = pendingApprovalItems.map(\.id)
        return reveal ? pending.first : pending.first { !shown.contains($0) }
    }

    // MARK: History

    var needsHistory: Bool { !historyComplete }

    /// How many rows the last history page added: none when it held only rows the
    /// timeline leaves out (this conversation's own turns) or nothing to show.
    private(set) var historyAddedByLastPage = 0

    /// Whether "Load earlier" should fetch the next page at once: the page just
    /// folded added nothing, and it handed on a cursor below the one it was
    /// asked with (C-29.8: a page may be empty and still hand on a cursor, as when
    /// a large tool result fills its read cap).
    func shouldFollowHistory(askedBefore: Int?) -> Bool {
        guard historyAddedByLastPage == 0, !historyComplete, let next = historyBefore else { return false }
        return askedBefore.map { next < $0 } ?? true
    }

    /// Fold one page of the native transcript (newest first). Rows of the
    /// conversation's Subfleet turns are left out, since the events show them:
    /// everything at or after the oldest user row that carries one of this
    /// conversation's message ids (a Claude user row's uuid is the message id),
    /// and anything stamped at or after the conversation's first event. Such a
    /// user row gives its message the person's text.
    mutating func apply(history page: HistoryPage) {
        historyPagesLoaded += 1
        historyBefore = page.next_before
        historyComplete = page.next_before == nil
        let known = Set(order)
        let lastSubfleetRow = page.items.lastIndex {
            $0.role == "user" && $0.kind == "text" && $0.id.map(known.contains) == true
        }
        var older: [TimelineItem] = []
        var perCursor: [Int: Int] = [:]
        for (index, item) in page.items.enumerated() {
            if item.role == "user", item.kind == "text", let id = item.id, known.contains(id) {
                setPersonText(item.text, for: id)
                continue
            }
            if let lastSubfleetRow, index <= lastSubfleetRow { continue }
            if afterBoundary(item.ts) { continue }
            let cursor = item.cursor ?? -1
            let index = perCursor[cursor, default: 0]
            perCursor[cursor] = index + 1
            let itemID = "history:\(cursor):\(index)"
            guard !history.contains(where: { $0.id == itemID }) else { continue }
            older.append(TimelineItem(id: itemID, messageID: nil, content: Timeline.content(of: item), ts: item.ts))
        }
        history = older.reversed() + history
        historyAddedByLastPage = older.count
    }

    /// A history row drawn as the live row of its kind: a tool call with its
    /// result and outcome, a thought, an answer; the person's words as a bubble.
    static func content(of item: HistoryItem) -> TimelineContent {
        switch item.kind {
        case "tool":
            let state: ToolActivity.State = item.is_error == true ? .failed : item.preview != nil ? .succeeded : .unfinished
            return .tool(ToolActivity(toolID: item.tool_id, name: item.tool ?? "tool", summary: item.text,
                                      hidden: item.hidden ?? false, state: state, preview: item.preview))
        case "thinking":
            return .thinking(item.text, final: true)
        default:
            return .history(role: item.role, text: item.text, tool: nil)
        }
    }

    private func afterBoundary(_ ts: String?) -> Bool {
        Timeline.after(ts, boundary: firstEventTS)
    }

    private static func after(_ ts: String?, boundary: String?) -> Bool {
        guard let boundary = boundary.flatMap(parseTimestamp), let value = ts.flatMap(parseTimestamp) else { return false }
        return value >= boundary
    }

    private mutating func trimHistory() {
        let boundary = firstEventTS
        history.removeAll { Timeline.after($0.ts, boundary: boundary) }
    }

    // MARK: Display

    /// Everything in display order: older transcript rows, then each message
    /// followed by what its turn did. A steered message's bubble is drawn once:
    /// inside its host turn where the provider took it, or else in its own place.
    var items: [TimelineItem] {
        var out = history
        let placed = placedSteers
        var drawn: Set<String> = []
        for id in displayOrder {
            guard let turn = turns[id] else { continue }
            if !placed.contains(id), let person = personItem(turn) { out.append(person) }
            for item in turn.items {
                guard case .steered(let steered) = item.content else {
                    out.append(item)
                    continue
                }
                guard placed.contains(steered), turns[steered]?.steerDeliveredIn == id, drawn.insert(steered).inserted,
                      let message = turns[steered], let person = personItem(message) else { continue }
                out.append(person)
            }
        }
        return out
    }

    /// Messages drawn inside a host turn rather than in their own place: at the
    /// host that last took them (`steerDeliveredIn`, whose words the status line
    /// shows), and only when that host was sent before them, so the host's bubble
    /// always comes first and no two messages can hold each other.
    var placedSteers: Set<String> {
        let place = Dictionary(uniqueKeysWithValues: order.enumerated().map { ($0.element, $0.offset) })
        var out: Set<String> = []
        for (index, id) in order.enumerated() {
            for item in turns[id]?.items ?? [] {
                if case .steered(let steered) = item.content, let turn = turns[steered], turn.isPlacedSteer,
                   turn.steerDeliveredIn == id, (place[steered] ?? -1) > index {
                    out.insert(steered)
                }
            }
        }
        return out
    }

    /// Message ids in the order the timeline shows them: the turns in the order
    /// they began running (their first event), then the messages still waiting
    /// in the queue. Sequence order put a failover continuation (D-6) last,
    /// under the messages queued before the limit and the next turn's approval
    /// card, although C-26.7 runs it first (2026-09-27), and an unblock note
    /// (C-24.8) below the person's turn that ran after it. A message that never
    /// began (withdrawn, refused, or a continuation not yet sent) sits right
    /// after the latest turn that began among the messages before it. The queue
    /// shows in dispatch order: repair messages, missed steers, then ordinary
    /// messages, retaining sequence order within each group (C-24.9).
    var displayOrder: [String] {
        let queue = order.filter { turns[$0].map(Timeline.waitsInQueue) ?? false }
        func priority(_ id: String) -> Int {
            if Timeline.repairOrigins.contains(turns[id]?.origin ?? "") { return 0 }
            if turns[id]?.stateReason?.hasPrefix("steer-missed:") == true { return 1 }
            return 2
        }
        let waiting = queue.enumerated().sorted {
            (priority($0.element), $0.offset) < (priority($1.element), $1.offset)
        }.map(\.element)
        let queued = Set(waiting)
        var latest = 0
        var placed: [(id: String, key: (Int, Int, Int))] = []
        for (place, id) in order.enumerated() where !queued.contains(id) {
            if id == Timeline.conversationKey {
                placed.append((id, (Int.max, 1, place)))
            } else if let began = turns[id]?.firstEventSeq {
                placed.append((id, (began, 0, place)))
                latest = max(latest, began)
            } else {
                placed.append((id, (latest, 1, place)))
            }
        }
        return placed.sorted { $0.key < $1.key }.map(\.id) + waiting
    }

    /// The origins the daemon sends ahead of queued person messages
    /// (`REPAIR_ORIGINS`, `next_dispatchable`).
    static let repairOrigins: Set<String> = ["unblock-note", "failover"]

    /// A message not yet sent to the provider: still queued (or not yet
    /// received by the daemon), nothing done for it. A continuation belongs
    /// with the message it continues, which the daemon sends next.
    static func waitsInQueue(_ turn: TurnTimeline) -> Bool {
        turn.messageID != conversationKey && turn.continues == nil && turn.items.isEmpty && turn.firstEventSeq == nil
            && (turn.state == MessageState.queued.rawValue || turn.state == "sending")
    }

    /// The newest row of the turn that began last: what the view follows while
    /// the end is on screen. Queued messages sit below it, and a message
    /// withdrawn from the queue may too; neither changes while a turn streams.
    var followedItem: TimelineItem? {
        let begun = turns.values.filter { $0.messageID != Timeline.conversationKey && $0.firstEventSeq != nil }
        if let turn = begun.max(by: { ($0.firstEventSeq ?? 0) < ($1.firstEventSeq ?? 0) }),
           let last = turn.items.last ?? personItem(turn) {
            return last
        }
        // Before any turn began: the last row above the queue.
        for id in displayOrder.reversed() {
            guard let turn = turns[id], !Timeline.waitsInQueue(turn) else { continue }
            if let last = turn.items.last ?? personItem(turn) { return last }
        }
        return history.last
    }

    private func personItem(_ turn: TurnTimeline) -> TimelineItem? {
        guard turn.messageID != Timeline.conversationKey else { return nil }
        let id = "person:\(turn.messageID)"
        switch turn.origin {
        case "tombstone":
            return nil
        case "failover":
            var words = "Continued after a usage limit"
            if let account = turn.served.account ?? turn.served.lane_id { words += " on \(account)" }
            return TimelineItem(id: id, messageID: turn.messageID, content: .notice(words), ts: nil)
        case "unblock-note":
            return TimelineItem(id: id, messageID: turn.messageID,
                                content: .notice("The stopped turn was left; the next turn starts without resuming it"),
                                ts: nil)
        default:
            return TimelineItem(id: id, messageID: turn.messageID,
                                content: .person(text: turn.personText, attachments: turn.attachments, state: turn.state),
                                ts: nil)
        }
    }

    func turn(_ messageID: String) -> TurnTimeline? { turns[messageID] }

    /// A message's status words; a steered one's read state follows the turn it
    /// joins (the turn it was steered into, or else the live one).
    func statusText(of messageID: String, assistant: String = "Claude") -> String? {
        guard let turn = turns[messageID] else { return nil }
        var host = (turn.steeredInto ?? turn.steerDeliveredIn).flatMap { turns[$0] }
        if host == nil, let live = liveMessageID, live != messageID { host = turns[live] }
        return turn.statusText(host: host, assistant: assistant)
    }

    /// Messages the person meant to steer that the provider has not read, oldest
    /// first: unread steers, and ones the daemon refused (they wait in the queue).
    /// Esc takes back the newest it still can (DESIGN.md section 9).
    var recallableSteers: [String] {
        order.filter { id in
            guard let turn = turns[id] else { return false }
            return turn.isUnreadSteer || (turn.messageState == .queued && turn.steerRefusal != nil)
        }
    }

    /// The message a Stop acts on: the newest live one.
    var liveMessageID: String? {
        order.reversed().first { id in
            guard let state = turns[id]?.messageState else { return false }
            return [.waiting, .starting, .running, .approvalNeeded].contains(state)
        }
    }
}

/// What the conversation view remembers to bring waiting cards into view: each
/// new card once, when it appears, and the oldest whenever the conversation is
/// opened or the person asks. It waits until the opened conversation has read
/// its log, since rows arriving above a card move it out of view, and brings the
/// card back once when the first page of older history this visit loads adds
/// rows above it.
struct ApprovalFollower: Equatable {
    private(set) var conversationID: String?
    private(set) var shown: Set<String> = []
    /// The card last scrolled to in this visit.
    private(set) var last: String?
    /// History pages the timeline had when this visit began, and whether a page
    /// has loaded since (only the first one moves the card back).
    private(set) var historyPagesAtOpen = 0
    private(set) var historyLoaded = false

    /// The row to scroll to now, if any, after `timeline` changed or the person
    /// asked (`reveal`, set to false once a row answers it).
    mutating func target(in timeline: Timeline, reveal: inout Bool) -> String? {
        if conversationID != timeline.conversationID {
            conversationID = timeline.conversationID
            shown = []
            last = nil
            historyPagesAtOpen = timeline.historyPagesLoaded
            historyLoaded = false
        }
        guard timeline.caughtUp else { return nil }
        let pending = timeline.pendingApprovalItems.map(\.id)
        var target = timeline.approvalScrollTarget(shown: shown, reveal: reveal)
        shown.formUnion(pending)
        if !historyLoaded && timeline.historyPagesLoaded > historyPagesAtOpen {
            historyLoaded = true
            if target == nil, timeline.historyAddedByLastPage > 0, let last, pending.contains(last) { target = last }
        }
        if let target {
            last = target
            reveal = false
        }
        return target
    }
}

/// "950", "18k", "972k", "1.2M".
func tokenWords(_ count: Double) -> String {
    if count >= 999_500 { return String(format: "%.1fM", count / 1_000_000) }
    if count >= 1_000 { return "\(Int((count / 1_000).rounded()))k" }
    return "\(Int(count))"
}

/// ISO 8601 with or without fractional seconds, as the daemon and transcripts write it.
func parseTimestamp(_ value: String) -> Date? {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = formatter.date(from: value) { return date }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}


/// C-29.8: one history request. It fetches a page; with `follow` ("Load
/// earlier"), while pages add nothing to the timeline but hand on a cursor below
/// the one asked with, it fetches the next, up to `pages` in all. It stops at the
/// history's start or on an error (history is a courtesy: the live turns stay).
/// Returns how many pages it fetched.
@discardableResult
func loadHistoryPages(pages: Int, follow: Bool,
                      timeline: () async -> Timeline?,
                      fetch: (Int?) async throws -> HistoryPage,
                      apply: (HistoryPage) async -> Void) async -> Int {
    var fetched = 0
    while fetched < max(1, pages) {
        guard let current = await timeline(), !current.historyComplete else { return fetched }
        let before = current.historyBefore
        guard let page = try? await fetch(before) else { return fetched }
        await apply(page)
        fetched += 1
        guard follow, await timeline()?.shouldFollowHistory(askedBefore: before) == true else { return fetched }
    }
    return fetched
}
