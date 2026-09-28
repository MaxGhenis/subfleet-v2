// Subfleet: the queue tray. A message waiting its turn leaves the scrolling
// timeline and sits in a compact tray pinned above the composer, in the order
// the daemon sends the queue, with Withdraw (design §12, C-29.7). Foundation only.
//
// Which messages wait in the tray:
// - every message the daemon holds `queued` that the timeline places in the
//   queue (`Timeline.waitsInQueue`; a failover continuation shows under the
//   message it continues, where it runs);
// - a message this app is sending, before its receipt, when something is ahead
//   of it: a live turn, an earlier message still waiting, or a block that holds
//   the conversation. The daemon will answer `queued`, so it goes to the tray at
//   once instead of showing in the timeline first. With nothing ahead it starts
//   next and shows in the timeline, where the person's own send always shows.
// Everything else stays in the timeline. A message withdrawn before it started
// reads as one line there, not as a bubble: it never reached the provider.

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
    /// Whether the row offers Steer: deliver it into the running turn instead of
    /// after it. Only the head of the queue, a person's message the daemon holds,
    /// and only while a turn is live and the daemon steers this provider
    /// (`steer.v1`; the steer build turns it on).
    var canSteer = false
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

    /// The tray's heading; nil while nothing waits.
    var trayTitle: String? { queueTrayTitle(tray, live: live, held: held) }

    /// What the view compares to decide whether to keep the end of the timeline in view.
    func followKey(length: (TimelineItem) -> Int) -> FollowKey {
        let last = items.last
        var own = false
        if case .person(_, _, let state)? = last?.content, state == "sending" { own = true }
        return FollowKey(last: last?.id, lastIsOwnSend: own, followed: followed?.id,
                         followedLength: followed.map(length) ?? 0, tray: tray.map(\.id),
                         settling: settling, hasHistory: hasHistory)
    }
}

/// What the conversation view watches to keep the end of the timeline in view
/// (design §12).
struct FollowKey: Equatable {
    /// The timeline's last row.
    var last: String?
    /// That row is the person's message not yet received: one they just sent.
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
    /// A message queued or withdrawn while the person reads further up moves
    /// nothing; the tray shows it.
    static func move(from old: FollowKey, to new: FollowKey, atBottom: Bool) -> Move {
        guard old != new else { return .stay }
        if old.settling || new.settling { return .jump }
        if new.last != old.last && new.lastIsOwnSend { return .glide }
        guard atBottom else { return .stay }
        if new.last != old.last || new.followed != old.followed || new.tray != old.tray { return .glide }
        if new.followedLength != old.followedLength || new.hasHistory != old.hasHistory { return .jump }
        return .stay
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
        var ahead = held || liveMessageID != nil
        var out: [String] = []
        for id in queue {
            guard let turn = turn(id) else { continue }
            // The daemon holds it, or it waits behind something: a receipt will say `queued`.
            if turn.state == MessageState.queued.rawValue || ahead { out.append(id) }
            ahead = true
        }
        return out
    }

    /// The tray's rows.
    func queueTray(held: Bool) -> [QueuedMessage] {
        trayMessageIDs(held: held).compactMap { id in turn(id).map(QueuedMessage.init(turn:)) }
    }

    /// The timeline and the tray, as the conversation view shows them.
    /// `steerOffered`: the daemon steers this conversation's provider.
    func layout(held: Bool, steerOffered: Bool = false) -> ConversationLayout {
        var tray = queueTray(held: held)
        let live = liveMessageID != nil
        if steerOffered, live, let head = steerCandidate(tray), let index = tray.firstIndex(where: { $0.id == head }) {
            tray[index].canSteer = true
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
                                  settling: !caughtUp, hasHistory: historyPagesLoaded > 0)
    }

    /// A message the person withdrew before it started: `cancelled` by a
    /// withdrawal (`message.cancel`, a Stop while it waited, or the app's own
    /// withdrawal before the daemon had it), nothing done for it. It never
    /// reached the provider (D-12).
    static func withdrawnBeforeStart(_ turn: TurnTimeline) -> Bool {
        turn.state == MessageState.cancelled.rawValue && turn.items.isEmpty
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
func queueTrayTitle(_ tray: [QueuedMessage], live: Bool, held: Bool) -> String? {
    guard !tray.isEmpty else { return nil }
    let messages = tray.filter { $0.origin != "unblock-note" }.count
    let noted = messages < tray.count
    let count = messages == 1 ? "1 message" : "\(messages) messages"
    let what = messages == 0 ? "A note to the next turn is queued"
        : noted ? "A note and \(count) queued" : "\(count) queued"
    let one = tray.count == 1
    if held { return what + (one ? "; it waits" : "; they wait") + " until the conversation can continue" }
    if live { return what + (one ? "; it goes when this turn ends" : "; they go in order when this turn ends") }
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
