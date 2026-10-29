// Subfleet: the queue tray. A message waiting its turn leaves the scrolling
// timeline and sits in a compact tray pinned above the composer, in the order
// the daemon sends the queue, with Withdraw (design §12, C-29.7). Foundation only.
//
// Initial placement uses what is ahead or a deferral. It then moves only
// forward: a tray row stays until binding, and a timeline bubble never returns
// here on a retry, deferral or hold. Steers always remain timeline bubbles.
// A queued receipt alone does not move a row: every message.submit answers queued.
// Queued failover continuations stay as timeline notices, but count as ahead.
// Withdrawn messages, including tombstones, leave a compact timeline line.

import Foundation

/// One row of the queue tray: a message waiting its turn.
struct QueuedMessage: Identifiable, Equatable {
    /// The message id.
    var id: String
    var origin: String?
    /// The row's words (`queuePreview`): one or two lines of the message.
    var preview: String
    /// The whole text as far as the app has it, for the row's tooltip.
    var text: String?
    var attachments: Int
    /// No receipt yet: the daemon may not have the message (D-22).
    var sending: Bool
    /// A second caption when the row is not simply waiting: "Sending", a deferral.
    var status: String?
    /// The running turn the row's Send now steers into, as the tray shows it: set only
    /// on the head of the queue, a person's message the daemon holds, while a turn is
    /// live and the daemon steers this provider (`steer.v1`). Send now captures it when
    /// it is offered and names it (`into`): one used after that turn ended is refused,
    /// never joined to the next turn (C-24.9). Nil: the row offers no Send now.
    var steerInto: String? = nil
    /// Whether the row offers Send now: deliver it into the running turn instead of after it.
    var canSteer: Bool { steerInto != nil }
    /// What Withdraw does: `message.cancel` for a message the daemon holds, the
    /// outbox's withdrawal for one with no receipt (D-22), and nothing for an
    /// unblock note, which the person does not withdraw: without it the next turn
    /// resumes the stopped one, silently turning "Leave it" into "Continue it".
    var withdraw: StopAction

    var canWithdraw: Bool { withdraw != .none }
}

/// The conversation view in two parts: the scrolling timeline, and the queue
/// pinned above the composer.
struct ConversationLayout: Equatable {
    /// The scrolling timeline: every row except the tray's messages, with a
    /// message withdrawn before it started read as one line.
    var items: [TimelineItem]
    /// The queue, in the order the daemon sends it (`next_dispatchable`).
    var tray: [QueuedMessage]
    /// The row the view follows while the end of the timeline is on screen.
    var followed: TimelineItem?
    /// A block holds the queue until the person clears it (C-24.5).
    var held: Bool
    /// A turn is live: the queue goes after it.
    var live: Bool
    /// The conversation is still reading its log since it was focused
    /// (`Timeline.caughtUp` is false).
    var settling: Bool
    /// A page of older history has loaded (the one opening reads).
    var hasHistory: Bool
    var ownMessages: Set<String> = []
    var queuedAhead = false
    var localSend: String? = nil

    /// The tray's heading; nil while nothing waits.
    var trayTitle: String? { queueTrayTitle(tray, live: live, held: held, ahead: queuedAhead) }

    /// What the view compares to decide whether to keep the end of the timeline in view.
    func followKey(length: (TimelineItem) -> Int) -> FollowKey {
        let last = items.last
        var own = false
        if case .person(_, _, _)? = last?.content, let id = last?.messageID { own = ownMessages.contains(id) }
        return FollowKey(last: last?.id, lastIsOwnSend: own, followed: followed?.id,
                         followedLength: followed.map(length) ?? 0, tray: tray.map(\.id),
                         settling: settling, hasHistory: hasHistory, localSend: localSend, live: live, held: held)
    }
}

/// What the conversation view watches to keep the end of the timeline in view
/// (design §12).
struct FollowKey: Equatable {
    /// The timeline's last row.
    var last: String?
    /// The last row is a message sent locally. `localSend` identifies a new send.
    var lastIsOwnSend: Bool
    /// The newest row of the turn that began last (`Timeline.followedItem`).
    var followed: String?
    /// How much text that row holds: a streamed block grows in place.
    var followedLength: Int
    /// The tray's rows: the timeline gets shorter or taller as the tray changes.
    var tray: [String]
    /// The conversation is still reading its log since it was opened.
    var settling: Bool
    /// A page of older history has loaded (the one opening reads).
    var hasHistory: Bool
    /// Latest local send, even if a receipt arrived before the view drew it.
    var localSend: String? = nil
    /// Pinned strips and block banners change the viewport height.
    var live = false
    var held = false

    /// Before the conversation has a timeline.
    static let empty = FollowKey(last: nil, lastIsOwnSend: false, followed: nil, followedLength: 0, tray: [],
                                 settling: true, hasHistory: false)

    /// How the view moves to the end of the timeline after a change.
    enum Move: Equatable {
        /// It stays where it is.
        case stay
        /// At once, with no animation: opening lands at the end, and a growing block is followed.
        case jump
        /// With a short animation.
        case glide
    }

    /// How the view moves after the key changed from `old` to `new`.
    /// - While the conversation opens (it reads its log after being focused), and
    ///   when it has read it, the view lands at the end with no scrolling, however
    ///   many rows arrive (Max, 2026-09-28: going to a session should land at
    ///   the bottom without the scroll).
    /// - The person's own new message: always, with a glide.
    /// - Anything else only while the end is on screen: a new row, a new
    ///   followed row, or a tray that changed the timeline's height glide; a
    ///   growing block, or the first page of history landing above, jumps.
    /// An explicit row reveal suspends ordinary following even while the end
    /// marker's disappearance is pending. The marker resumes following when it
    /// appears again, or its presented geometry confirms that a completed reveal
    /// left it visible.
    /// Catch-up resumes after that confirmation releases suspension. The view
    /// releases suspension for the person's own send before asking (`ownSend`),
    /// so during catch-up it jumps to the end and otherwise glides.
    /// A message queued or withdrawn while the person reads further up moves
    /// nothing; the tray shows it.
    static func move(from old: FollowKey, to new: FollowKey, atBottom: Bool, suspended: Bool = false) -> Move {
        guard old != new else { return .stay }
        if old.settling || new.settling { return suspended ? .stay : .jump }
        if ownSend(from: old, to: new) { return .glide }
        guard !suspended else { return .stay }
        guard atBottom else { return .stay }
        if new.last != old.last || new.followed != old.followed || new.tray != old.tray { return .glide }
        if new.followedLength != old.followedLength || new.hasHistory != old.hasHistory
            || new.live != old.live || new.held != old.held { return .jump }
        return .stay
    }

    /// The change is the person's own new message: the last row is one this app sent.
    static func ownSend(from old: FollowKey, to new: FollowKey) -> Bool {
        new.last != old.last && new.lastIsOwnSend && (new.localSend == nil || new.localSend != old.localSend)
    }
}

extension Timeline {
    /// The queue in the order the daemon sends it: the messages the timeline
    /// places after every turn that has started (`waitsInQueue`), an unblock
    /// note first (`displayOrder`).
    var queue: [String] {
        displayOrder.filter { turn($0).map(Timeline.waitsInQueue) ?? false }
    }

    /// The tray's message ids, in the order the daemon sends them. `held`: a
    /// block holds the conversation (`blocked_by`), so nothing sent now starts.
    func trayMessageIDs(held: Bool) -> [String] {
        queue.filter { id in
            guard let turn = turn(id) else { return false }
            return turn.queuePlacement == .tray && !turn.sentAsSteer && turn.continues == nil
        }
    }

    /// The tray's rows.
    func queueTray(held: Bool, assistant: String = "Claude") -> [QueuedMessage] {
        trayMessageIDs(held: held).compactMap { id in
            guard let turn = turn(id) else { return nil }
            return QueuedMessage(turn: turn)
        }
    }

    /// The timeline and the tray, as the conversation view shows them.
    /// `steerOffered`: the daemon steers this conversation's provider.
    func layout(held: Bool, steerOffered: Bool = false, assistant: String = "Claude") -> ConversationLayout {
        var tray = queueTray(held: held, assistant: assistant)
        let live = liveMessageID != nil
        if steerOffered, let host = liveMessageID, let head = steerCandidate(tray), turn(head)?.messageState == .queued,
           let index = tray.firstIndex(where: { $0.id == head }) {
            tray[index].steerInto = host
        }
        let inTray = Set(tray.map(\.id))
        var shown: [TimelineItem] = []
        for item in items {
            guard let id = item.messageID, let turn = turn(id) else { shown.append(item); continue }
            if inTray.contains(id) { continue }
            if Timeline.withdrawnBeforeStart(turn), case .person = item.content {
                var line = item
                line.content = .notice(withdrawnWords(turn))
                shown.append(line)
            } else {
                shown.append(item)
            }
        }
        let ids = Set(shown.map(\.id))
        let followed = followedItem.flatMap { ids.contains($0.id) ? $0 : nil } ?? shown.last
        return ConversationLayout(items: shown, tray: tray, followed: followed, held: held, live: live,
                                  settling: !caughtUp, hasHistory: historyPagesLoaded > 0,
                                  ownMessages: Set(turns.values.filter { $0.locallySent }.map(\.messageID)),
                                  queuedAhead: queue.first.map { !inTray.contains($0) } ?? false, localSend: lastLocalMessageID)
    }

    /// A message the person withdrew before it started: `cancelled` by a
    /// withdrawal (`message.cancel`, a Stop while it waited, or the app's own
    /// withdrawal before the daemon had it), nothing done for it. It never
    /// reached the provider (D-12).
    static func withdrawnBeforeStart(_ turn: TurnTimeline) -> Bool {
        turn.state == MessageState.cancelled.rawValue && turn.items.isEmpty
            && turn.firstEventSeq == nil
            && ["withdrawn", "withdrawn-before-receipt"].contains(turn.stateReason ?? "")
    }
}

extension QueuedMessage {
    init(turn: TurnTimeline) {
        let note = turn.origin == "unblock-note"
        let sending = turn.messageState == nil
        id = turn.messageID
        origin = turn.origin
        text = note ? nil : turn.personText
        attachments = turn.attachments.count
        self.sending = sending
        preview = note ? "Note to the next turn: the stopped turn is left, not resumed"
            : queuePreview(turn.personText, attachments: attachments)
        status = sending ? "Sending" : queueStatusWords(turn.stateReason)
        withdraw = note ? .none : sending ? .withdraw(messageID: turn.messageID) : .cancel(messageID: turn.messageID)
    }
}

/// The row Steer may take: the head of the queue when it is a person's message
/// the daemon holds. Nothing goes ahead of a repair message (an unblock note),
/// and a message with no receipt cannot be steered yet.
func steerCandidate(_ tray: [QueuedMessage]) -> String? {
    guard let head = tray.first, head.origin == "person" || head.origin == nil, !head.sending else { return nil }
    return head.id
}

/// The most characters a tray row keeps; the row shows at most two lines of them.
let queuePreviewLimit = 280

/// A tray row's words: the message on one line (runs of spaces and line breaks
/// folded into one space), cut at `queuePreviewLimit` characters with "…"; the
/// image count when there is no text.
func queuePreview(_ text: String?, attachments: Int) -> String {
    let folded = (text ?? "").split(whereSeparator: \.isWhitespace).joined(separator: " ")
    if folded.isEmpty {
        return attachments > 0 ? imagesWords(attachments) : "(message text not available)"
    }
    let cut = folded.count > queuePreviewLimit ? String(folded.prefix(queuePreviewLimit - 1)) + "…" : folded
    return attachments > 0 ? "\(cut) (\(imagesWords(attachments)))" : cut
}

func imagesWords(_ count: Int) -> String {
    "\(count) image\(count == 1 ? "" : "s")"
}

/// A queued message's `state_reason`, as words for its row: nil while it simply
/// waits; "Deferred: <why>" when the daemon put it back (service.py `_defer`).
func queueStatusWords(_ reason: String?) -> String? {
    guard let reason, !reason.isEmpty else { return nil }
    if reason.hasPrefix("deferred:") {
        return "Deferred:" + reason.dropFirst("deferred:".count)
    }
    return reason.prefix(1).uppercased() + reason.dropFirst().replacingOccurrences(of: "-", with: " ")
}

/// The timeline's line for a message withdrawn before it started.
func withdrawnWords(_ turn: TurnTimeline) -> String {
    let words = queuePreview(turn.personText, attachments: turn.attachments.count)
    return "Withdrawn before it was sent: " + words
}

/// The tray's heading: what waits, and when it goes. Nil when the tray is empty.
func queueTrayTitle(_ tray: [QueuedMessage], live: Bool, held: Bool, ahead: Bool = false) -> String? {
    guard !tray.isEmpty else { return nil }
    let messages = tray.filter { $0.origin != "unblock-note" }.count
    let noted = messages < tray.count
    let count = messages == 1 ? "1 message" : "\(messages) messages"
    let what = messages == 0 ? "A note to the next turn is queued"
        : noted ? "A note and \(count) queued" : "\(count) queued"
    let one = tray.count == 1
    if held { return what + (one ? "; it waits" : "; they wait") + " until the conversation can continue" }
    if live { return what + (one ? "; it goes when this turn ends" : "; they go in order when this turn ends") }
    if ahead { return what + (one ? "; it follows the message ahead" : "; they follow the message ahead in order") }
    return what + (one ? "; it goes next" : "; they go in order")
}

/// How many tray rows show: all of them up to `limit`; past that, the first
/// `limit - 1` and a "Show N more" control until the person expands the tray.
func queueTrayVisible(count: Int, expanded: Bool, limit: Int = 4) -> (shown: Int, hidden: Int) {
    let count = max(0, count)
    if expanded || count <= limit { return (count, 0) }
    let shown = max(1, limit - 1)
    return (shown, count - shown)
}

/// Search visibility is a projection of state, independent of view lifecycle.
/// A reveal for another conversation or outside this tray leaves it collapsed.
struct QueueTrayPresentation {
    let revealTargetsTray: Bool
    let expanded: Bool
    let rows: [QueuedMessage]
    let hidden: Int

    init(rows: [QueuedMessage], expanded: Bool, conversationID: String?, reveal: SearchReveal?, limit: Int = 4) {
        revealTargetsTray = reveal.map { request in
            request.conversationID == conversationID && rows.contains { "person:" + $0.id == request.itemID }
        } ?? false
        self.expanded = expanded || revealTargetsTray
        let visible = queueTrayVisible(count: rows.count, expanded: self.expanded, limit: limit)
        self.rows = Array(rows.prefix(visible.shown))
        hidden = visible.hidden
    }
}


/// The label shared by queued-row controls and the engine's state projection.
func stopLabel(_ action: StopAction) -> String? {
    switch action {
    case .withdraw, .cancel, .cancelSteer: return "Withdraw"
    case .interrupt: return "Stop"
    case .none: return nil
    }
}

extension ConversationStoreState {
    /// One projection for the conversation view and future per-message notices.
    /// The optional QueueTray.notice seam can attach future notices by row.id.
    func layout(conversationID: String) -> ConversationLayout? {
        guard let timeline = timelines[conversationID], let conversation = conversation(conversationID) else { return nil }
        var layout = timeline.layout(held: conversation.blocked_by != nil || conversation.live_elsewhere == true,
                                     assistant: conversation.provider == "codex" ? "Codex" : "Claude")
        if let head = layout.tray.first, timeline.queue.first == head.id,
           let host = queuedSteerHost(messageID: head.id, conversation: conversation, timeline: timeline,
                                      capabilities: availability.capabilities) {
            layout.tray[0].steerInto = host.messageID
        }
        return layout
    }
}
