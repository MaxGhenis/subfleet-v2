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
}

struct TimelineItem: Identifiable, Equatable {
    var id: String
    var messageID: String?
    var content: TimelineContent
    var ts: String?
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

    /// The status strip's words for where this turn is (design §12).
    var statusText: String {
        switch messageState {
        case .queued: return "Queued behind the current turn"
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
        case nil: return state == "sending" ? "Sending" : state
        }
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
        return .applied(count)
    }

    /// Drop everything the event log produced; receipts and the person's text stay.
    mutating func resetEvents() {
        cursor = 0
        resets += 1
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
        if firstEventTS == nil, let ts = event.ts {
            firstEventTS = ts
            trimHistory()
        }
        let data = event.data
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
            if let requestID, turn.items.contains(where: { $0.id == "approval:\(id):\(requestID)" }) { break }
            if let index = turn.items.firstIndex(where: {
                if case .approval(let card) = $0.content { return card.requestID == nil && card.display == display && card.kind == kind }
                return false
            }), case .approval(var card) = turn.items[index].content {
                card.requestID = requestID
                turn.items[index].content = .approval(card)
            } else {
                let card = ApprovalCard(requestID: requestID,
                                        approvalID: knownApprovalID(in: turn, kind: kind, display: display),
                                        kind: kind, display: display, options: options, state: .pending)
                turn.items.append(TimelineItem(id: "approval:\(id):\(requestID ?? "seq\(event.seq)")", messageID: id,
                                               content: .approval(card), ts: event.ts))
            }
            if !(turn.messageState?.isTerminal ?? false) { turn.state = MessageState.approvalNeeded.rawValue }
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
        default:
            unknownKinds[event.kind, default: 0] += 1
        }
        turns[id] = turn
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
        turns[receipt.message_id] = turn
        sortOrder()
    }

    /// A message this app is sending: shown at once, before its receipt (the
    /// composer's optimistic row).
    mutating func addLocal(messageID: String, text: String, attachments: [String] = [],
                           settings: ConversationSettings? = nil) {
        ensureTurn(messageID)
        guard var turn = turns[messageID] else { return }
        turn.personText = text
        turn.attachments = attachments
        turn.origin = turn.origin ?? "person"
        turn.settings = turn.settings ?? settings
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
    /// events made (which carry the provider's request id), by message, kind and
    /// display; an approval with no card yet gets one.
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
                if card.isPending { card.state = state }
                turn.items[index].content = .approval(card)
            } else if let index = turn.items.firstIndex(where: {
                if case .approval(let card) = $0.content {
                    return card.approvalID == nil && card.kind == approval.kind && card.display == approval.display
                }
                return false
            }), case .approval(var card) = turn.items[index].content {
                card.approvalID = approval.approval_id
                if card.isPending { card.state = state }
                turn.items[index].content = .approval(card)
            } else if approval.state == "pending" {
                let card = ApprovalCard(requestID: nil, approvalID: approval.approval_id, kind: approval.kind,
                                        display: approval.display, options: approval.options, state: .pending)
                turn.items.append(TimelineItem(id: "approval:\(approval.message_id):\(approval.approval_id)",
                                               messageID: approval.message_id, content: .approval(card),
                                               ts: approval.created_at))
            }
            turns[approval.message_id] = turn
        }
    }

    /// The id of a known approval for a card an event is making: same message,
    /// kind and display, not yet held by another card of the turn; the oldest first.
    private func knownApprovalID(in turn: TurnTimeline, kind: String, display: ApprovalDisplay) -> String? {
        let held = Set(turn.items.compactMap { item -> String? in
            if case .approval(let card) = item.content { return card.approvalID }
            return nil
        })
        return knownApprovals.values
            .filter { $0.message_id == turn.messageID && $0.kind == kind && $0.display == display
                && !held.contains($0.approval_id) }
            .min { ($0.created_at, $0.approval_id) < ($1.created_at, $1.approval_id) }?
            .approval_id
    }

    /// Every card still waiting for the person.
    var pendingApprovalCards: [ApprovalCard] { order.flatMap { turns[$0]?.pendingApprovals ?? [] } }

    // MARK: History

    var needsHistory: Bool { !historyComplete }

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
    /// followed by what its turn did.
    var items: [TimelineItem] {
        var out = history
        for id in order {
            guard let turn = turns[id] else { continue }
            if let person = personItem(turn) { out.append(person) }
            out += turn.items
        }
        return out
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

    /// The message a Stop acts on: the newest live one.
    var liveMessageID: String? {
        order.reversed().first { id in
            guard let state = turns[id]?.messageState else { return false }
            return [.waiting, .starting, .running, .approvalNeeded].contains(state)
        }
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
