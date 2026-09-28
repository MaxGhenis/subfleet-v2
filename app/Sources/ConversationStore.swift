// Subfleet: the conversation store's logic, as plain Swift (design §12, D-24).
//
// `ConversationStoreState` is a value the UI publishes: conversations and the
// catalog of native sessions, models, the focused conversation's timeline, the
// pending approvals count (the Dock badge), and the notifications to post. It
// changes only through its `apply` methods, which fold daemon answers in.
// `ConversationEngine` makes the daemon calls those answers come from; its
// calls block, so the UI runs them on one serial background queue and applies
// the results on the main actor. The UI's ObservableObject wraps both.
// Foundation only.

import Foundation

// MARK: - Sidebar

struct SidebarEntry: Identifiable, Equatable {
    enum Target: Equatable, Hashable {
        case conversation(String)
        case native(NativeSessionRef)
    }

    var id: String
    var target: Target
    var provider: String
    var title: String
    var subtitle: String
    var workspace: String?
    var date: Date?
    var pendingApprovals: Int
    var active: Bool
    var blockedBy: String?
    var liveElsewhere: Bool
    var continuable: Bool
    var continueBlocker: String?
    /// A message of this conversation is not going through (C-29.12): the worst kind.
    var sendProblem: SendNotice.Kind? = nil
}

struct SidebarSection: Identifiable, Equatable {
    var id: String
    var title: String
    var entries: [SidebarEntry]
}

enum SidebarGrouping: String, CaseIterable {
    case recency
    case workspace
}

/// `~/…` for paths under the home directory.
func abbreviatedPath(_ path: String, home: String = NSHomeDirectory()) -> String {
    let prefix = home.hasSuffix("/") ? home : home + "/"
    if path == home { return "~" }
    return path.hasPrefix(prefix) ? "~/" + path.dropFirst(prefix.count) : path
}

// MARK: - Composer

struct ModelChoice: Identifiable, Equatable {
    var id: String { value }
    var label: String
    var value: String
    var model: ModelEntry
}

struct PermissionChoice: Identifiable, Equatable {
    var id: String { policy.rawValue }
    var policy: PermissionPolicy
    var enabled: Bool
    var disabledReason: String?
    /// Choosing it needs the person's confirmation (`confirm_widen`).
    var widens: Bool
}

struct ComposerOptions: Equatable {
    var models: [ModelChoice]
    var selectedModel: ModelEntry?
    var efforts: [String]
    /// False when no turn has reported the model's efforts yet (D-19: accepted
    /// as unverified and checked by the driver before the message is sent).
    var effortsObserved: Bool
    var fastSupported: Bool?
    /// "Bills usage credits" (Claude) or "Draws on plan limits" (Codex).
    var fastNote: String
    var permissions: [PermissionChoice]
}

let codexReadOnlyReason = "Codex conversations are read-only until the never-rules hook is shown firing in a live app-server turn."

func makeComposerOptions(provider: String, settings: ConversationSettings, models: [ModelEntry],
                         capabilities: Capabilities?) -> ComposerOptions {
    let mine = models.filter { $0.provider == provider }
    var choices: [ModelChoice] = []
    for entry in mine {
        let name = entry.short.prefix(1).uppercased() + entry.short.dropFirst()
        for value in entry.values where !choices.contains(where: { $0.value == value }) {
            choices.append(ModelChoice(label: entry.values.count > 1 ? "\(name) (\(value))" : name, value: value, model: entry))
        }
    }
    let selected = mine.first { $0.values.contains(settings.model) || $0.value == settings.model || $0.id == settings.model }
    let efforts = selected?.efforts ?? selected?.default_effort.map { [$0] } ?? []
    let billing = selected?.fast.billing ?? (provider == "claude" ? "usage credits" : "plan limits")
    let note = billing == "usage credits" ? "Bills usage credits" : billing == "plan limits" ? "Draws on plan limits" : billing
    let writable = provider != "codex" || capabilities?.codex_writable == true
    let permissions = PermissionPolicy.allCases.map { policy -> PermissionChoice in
        let enabled = writable || policy == .readOnly
        return PermissionChoice(policy: policy, enabled: enabled, disabledReason: enabled ? nil : codexReadOnlyReason,
                                widens: PermissionPolicy.widens(from: settings.permission, to: policy.rawValue))
    }
    return ComposerOptions(models: choices, selectedModel: selected, efforts: efforts,
                           effortsObserved: selected?.efforts != nil, fastSupported: selected?.fast.supported,
                           fastNote: note, permissions: permissions)
}

// MARK: - Stop, blocked banner, served chip

enum StopAction: Equatable {
    case none
    /// Journaled but not acknowledged: withdraw through the outbox (D-22).
    case withdraw(messageID: String)
    /// Queued in the daemon: `message.cancel`.
    case cancel(messageID: String)
    /// Waiting, starting, running or asking: `turn.interrupt`.
    case interrupt(messageID: String)
}

/// What Stop does for a message (brief item 7; service.py `op_message_cancel`,
/// `op_turn_interrupt`). A message with no receipt yet (`sending`) is withdrawn
/// through the outbox (D-22), which asks the daemon first when it may have it.
func stopAction(for messageID: String, state: String?, outboxEntry: OutboxEntry?) -> StopAction {
    if let entry = outboxEntry, entry.kind == .messageSubmit, entry.isOpen { return .withdraw(messageID: messageID) }
    switch state.flatMap(MessageState.init(rawValue:)) {
    case .queued: return .cancel(messageID: messageID)
    case .waiting, .starting, .running, .approvalNeeded: return .interrupt(messageID: messageID)
    case nil where state == "sending": return .withdraw(messageID: messageID)
    default: return .none
    }
}

/// The words on a message's control: Stop while its turn runs, Withdraw while it
/// waits; nil when there is nothing to stop.
func stopLabel(_ action: StopAction) -> String? {
    switch action {
    case .none: return nil
    case .interrupt: return "Stop"
    case .cancel, .withdraw: return "Withdraw"
    }
}

struct BlockedChoice: Equatable {
    enum Action: Equatable {
        case unblock(ConversationUnblockArgs.Choice)
        case resolve(messageID: String, resolution: MessageResolveArgs.Resolution)
    }
    var label: String
    var detail: String
    var action: Action
}

struct BlockedBanner: Equatable {
    var title: String
    var detail: String
    var choices: [BlockedChoice]
}

/// C-24.6, C-24.8: a blocked conversation waits for the person's choice.
func makeBlockedBanner(for conversation: Conversation, timeline: Timeline?) -> BlockedBanner? {
    switch conversation.blocked_by {
    case nil:
        return nil
    case "unfinished-turn":
        return BlockedBanner(
            title: "The last turn stopped without finishing",
            detail: "Claude would resume it with the next message. Choose what happens before anything else is sent.",
            choices: [
                BlockedChoice(label: "Continue it", detail: "The next turn picks up the stopped work.", action: .unblock(.continue)),
                BlockedChoice(label: "Leave it", detail: "A note tells the next turn not to resume it.", action: .unblock(.leave)),
            ])
    case "delivery-unknown":
        let unknown = timeline?.order.reversed().first { timeline?.turn($0)?.messageState == .deliveryUnknown }
            ?? (conversation.last_message?.state == MessageState.deliveryUnknown.rawValue ? conversation.last_message?.message_id : nil)
        guard let messageID = unknown else {
            return BlockedBanner(title: "A message's delivery is unknown",
                                 detail: "Open the conversation's latest messages to resolve it.", choices: [])
        }
        return BlockedBanner(
            title: "It is not known whether the provider received the last message",
            detail: "Subfleet never sends it again on its own. Say what happened; it is recorded.",
            choices: [
                BlockedChoice(label: "It was delivered", detail: "Keep the conversation as the provider has it.",
                              action: .resolve(messageID: messageID, resolution: .delivered)),
                BlockedChoice(label: "It was not delivered", detail: "Mark it not delivered; send it again yourself if you want.",
                              action: .resolve(messageID: messageID, resolution: .notDelivered)),
            ])
    case "quarantined-turn":
        return BlockedBanner(title: "The last turn's process is quarantined",
                             detail: "The daemon holds this conversation until the quarantine is cleared; see `subfleet daemon status`.",
                             choices: [])
    case let other?:
        return BlockedBanner(title: "This conversation is blocked", detail: other, choices: [])
    }
}

struct ServedChip: Equatable {
    var account: String?
    var model: String?
    var effort: String?
    /// "on", "off", or what the provider reported.
    var fast: String?
    var warnings: [String]
}

/// Lane ids to the account labels `status.json` shows (codex homes and Claude accounts).
func laneLabels(from snapshot: Snapshot?) -> [String: String] {
    var out: [String: String] = [:]
    for home in snapshot?.codex.homes ?? [] {
        if let lane = home.lane_id { out[lane] = home.email ?? home.home }
    }
    for account in snapshot?.claude.accounts ?? [] {
        if let lane = account.lane_id { out[lane] = account.email }
    }
    return out
}

/// C-26.8: what served the turn, not what was asked for; a served Fast state
/// that differs from the request is a visible warning.
func makeServedChip(for turn: TurnTimeline, provider: String, laneLabels: [String: String] = [:]) -> ServedChip? {
    let served = turn.served
    guard !served.fields.isEmpty else { return nil }
    var fast: String?
    if provider == "codex" {
        // The thread's `serviceTier` is null at standard speed, and served facts
        // drop nulls when they merge (as the runner does): once a served model is
        // known, anything but `priority` is standard.
        if served.service_tier == "priority" { fast = "on" }
        else if served.service_tier != nil || served.model != nil { fast = "off" }
    } else {
        fast = served.fast_mode_state
    }
    var warnings: [String] = []
    if let warning = served.fast_warning { warnings.append("Fast is off: \(warning)") }
    if turn.settings?.fast == true, let fast, fast != "on" {
        warnings.append("Fast was asked for; this turn ran at standard speed")
    }
    let account = served.account ?? served.lane_id.flatMap { laneLabels[$0] ?? $0 }
    return ServedChip(account: account, model: served.model, effort: served.effort ?? turn.settings?.effort,
                      fast: fast, warnings: warnings)
}

// MARK: - Notifications

struct NotificationIntent: Identifiable, Equatable {
    enum Kind: String {
        case completed, failed, approval
        case deliveryUnknown = "delivery-unknown"
    }
    var id: String
    var kind: Kind
    var conversationID: String
    var messageID: String?
    var title: String
    var body: String
}

// MARK: - State

struct ConversationStoreState: Equatable {
    var availability: DaemonAvailability = .unknown
    var conversations: [Conversation] = []
    var catalog: Catalog?
    /// `models.list` per provider.
    var models: [String: [ModelEntry]] = [:]
    var focusedConversationID: String?
    var timelines: [String: Timeline] = [:]
    /// Pending approvals per conversation: from `conversation.list`, then the watch feed.
    var pendingApprovals: [String: Int] = [:]
    var watchCursor = 0
    /// Live messages per conversation, from the watch feed (read from seq 0 at launch).
    var liveMessages: [String: Set<String>] = [:]
    /// The first drain of the watch feed after launch posts nothing.
    var watchBaselined = false
    var notifications: [NotificationIntent] = []
    private(set) var notified: Set<String> = []
    var searchQuery = ""
    var providerFilter: String?
    var grouping: SidebarGrouping = .recency
    var laneLabels: [String: String] = [:]
    /// The outbox's open entries by key, as its queue last reported them.
    private(set) var sends: [String: OutboxEntry] = [:]
    /// Their notices (C-29.12), by key.
    private(set) var sendNotices: [String: SendNotice] = [:]

    /// The Dock badge.
    var pendingApprovalCount: Int { pendingApprovals.values.reduce(0, +) }

    var focusedConversation: Conversation? { focusedConversationID.flatMap(conversation) }
    var focusedTimeline: Timeline? { focusedConversationID.flatMap { timelines[$0] } }

    func conversation(_ id: String) -> Conversation? { conversations.first { $0.conversation_id == id } }

    // MARK: Folding daemon answers

    mutating func apply(list: ConversationListResult) {
        conversations = list.conversations.map(withWatchActivity).sorted { $0.updated_at > $1.updated_at }
        catalog = list.catalog ?? catalog
        for conversation in list.conversations {
            pendingApprovals[conversation.conversation_id] = conversation.pending_approvals
        }
    }

    /// `_view.active` is false while the newest message is queued, even when an
    /// earlier one's turn runs (docs/desktop/app-needs.md); the watch feed's live
    /// messages keep such a conversation active.
    private func withWatchActivity(_ conversation: Conversation) -> Conversation {
        var conversation = conversation
        if !(liveMessages[conversation.conversation_id] ?? []).isEmpty { conversation.active = true }
        return conversation
    }

    mutating func apply(models: ModelsListResult, provider: String) {
        self.models[provider] = models.models.filter { $0.provider == provider }
    }

    mutating func upsert(_ conversation: Conversation) {
        let conversation = withWatchActivity(conversation)
        if let index = conversations.firstIndex(where: { $0.conversation_id == conversation.conversation_id }) {
            conversations[index] = conversation
        } else {
            conversations.insert(conversation, at: 0)
        }
        conversations.sort { $0.updated_at > $1.updated_at }
        pendingApprovals[conversation.conversation_id] = conversation.pending_approvals
        // A native session that now has a conversation leaves the catalog list.
        if let native = conversation.native_session_id {
            catalog?.items.removeAll { $0.provider == conversation.provider && $0.native_session_id == native }
        }
    }

    /// `conversation.open`: the conversation, its latest receipts and pending
    /// approvals. Its events are read from 0 by the events loop.
    mutating func apply(open: ConversationOpenResult) {
        upsert(open.conversation)
        let id = open.conversation.conversation_id
        var timeline = timelines[id] ?? Timeline(conversationID: id)
        timeline.apply(receipts: open.messages)
        timeline.attach(approvals: open.pending_approvals)
        timelines[id] = timeline
    }

    mutating func focus(_ conversationID: String?) {
        focusedConversationID = conversationID
        guard let conversationID else { return }
        // Its events loop reads the log again from the cursor (C-29.9).
        var timeline = timelines[conversationID] ?? Timeline(conversationID: conversationID)
        timeline.beginReading()
        timelines[conversationID] = timeline
        restoreUnsent(in: conversationID)
    }

    @discardableResult
    mutating func apply(events page: EventsPage, conversationID: String) -> Timeline.PageResult {
        var timeline = timelines[conversationID] ?? Timeline(conversationID: conversationID)
        let result = timeline.apply(page: page)
        timelines[conversationID] = timeline
        return result
    }

    mutating func apply(history page: HistoryPage, conversationID: String) {
        var timeline = timelines[conversationID] ?? Timeline(conversationID: conversationID)
        timeline.apply(history: page)
        timelines[conversationID] = timeline
    }

    mutating func apply(receipt: Receipt) {
        guard let id = receipt.conversation_id else {
            for key in timelines.keys where timelines[key]?.turn(receipt.message_id) != nil {
                timelines[key]?.apply(receipt: receipt)
            }
            return
        }
        var timeline = timelines[id] ?? Timeline(conversationID: id)
        timeline.apply(receipt: receipt)
        timelines[id] = timeline
    }

    mutating func apply(approvals: [ApprovalView], conversationID: String) {
        var timeline = timelines[conversationID] ?? Timeline(conversationID: conversationID)
        timeline.attach(approvals: approvals)
        timelines[conversationID] = timeline
        pendingApprovals[conversationID] = approvals.filter { $0.state == "pending" }.count
    }

    /// The composer's optimistic row, before the receipt.
    mutating func addLocalMessage(conversationID: String, messageID: String, text: String,
                                  attachments: [String] = [], settings: ConversationSettings? = nil) {
        var timeline = timelines[conversationID] ?? Timeline(conversationID: conversationID)
        timeline.addLocal(messageID: messageID, text: text, attachments: attachments, settings: settings)
        timelines[conversationID] = timeline
    }

    /// The person's text for a message this app sent (the receipt carries none).
    mutating func setPersonText(_ text: String, conversationID: String, messageID: String) {
        timelines[conversationID]?.setPersonText(text, for: messageID)
    }

    // MARK: Sends not going through

    /// Whether `entries` (the outbox's open ones) differ from what the state shows;
    /// the app sets them only then, so an unchanged pump redraws nothing.
    func differs(sends entries: [OutboxEntry]) -> Bool {
        let open = entries.filter(\.isOpen)
        return open.count != sends.count || open.contains { sends[$0.key] != $0 }
    }

    mutating func apply(sends entries: [OutboxEntry]) {
        let open = entries.filter(\.isOpen)
        sends = Dictionary(open.map { ($0.key, $0) }, uniquingKeysWith: { $1 })
        sendNotices = Outbox.notices(open)
        for conversationID in Set(open.map(\.conversation)) where timelines[conversationID] != nil {
            restoreUnsent(in: conversationID)
        }
    }

    /// The conversation's journaled messages the timeline has no row for: ones a
    /// previous run of the app left unsent. Each gets its row, in the order the
    /// outbox sends them, so its notice shows where the message shows (C-29.12).
    private mutating func restoreUnsent(in conversationID: String) {
        guard var timeline = timelines[conversationID] else { return }
        let missing = sends.values
            .filter { $0.kind == .messageSubmit && $0.conversation == conversationID && timeline.turn($0.key) == nil }
            .sorted { $0.order < $1.order }
        guard !missing.isEmpty else { return }
        for entry in missing {
            timeline.addLocal(messageID: entry.key, text: entry.message?.text ?? "",
                              attachments: entry.message?.attachments ?? [], settings: entry.message?.settings)
        }
        timelines[conversationID] = timeline
    }

    /// A message's notice while the app has seen no receipt for it: once the
    /// daemon has it (a receipt from the feed or an open), its state says more.
    /// A message that waits shows it only while a message ahead of it shows why.
    func sendNotice(conversationID: String, messageID: String) -> SendNotice? {
        guard let notice = sendNotices[messageID], unreceipted(conversationID, messageID) else { return nil }
        if notice.kind == .waiting {
            guard let own = sends[messageID], stuckSend(in: conversationID, before: own.order) != nil else { return nil }
        }
        return notice
    }

    /// No receipt for the message has reached this conversation's timeline.
    private func unreceipted(_ conversationID: String, _ messageID: String) -> Bool {
        timelines[conversationID]?.turn(messageID)?.messageState == nil
    }

    /// The first message of a conversation that is not going through (not one
    /// that only waits behind it): what the composer warns about. `before`: only
    /// messages the outbox sends ahead of that order.
    func stuckSend(in conversationID: String, before order: Int? = nil) -> SendNotice? {
        sends.values
            .filter { entry in
                entry.kind == .messageSubmit && entry.conversation == conversationID && (order.map { entry.order < $0 } ?? true)
            }
            .sorted { $0.order < $1.order }
            .lazy.compactMap { entry -> SendNotice? in
                guard let notice = self.sendNotices[entry.key], notice.kind != .waiting,
                      self.unreceipted(conversationID, entry.key) else { return nil }
                return notice
            }
            .first
    }

    /// The conversation view's timeline and queue tray (C-29.7), each tray row
    /// of a send that is not going through carrying its notice (C-29.12). A
    /// message is a row in one of them, so its notice shows in one place.
    func layout(conversationID: String, held: Bool, steerOffered: Bool = false) -> ConversationLayout? {
        guard var layout = timelines[conversationID]?.layout(held: held, steerOffered: steerOffered) else { return nil }
        for index in layout.tray.indices {
            layout.tray[index].attach(sendNotice(conversationID: conversationID, messageID: layout.tray[index].id))
        }
        return layout
    }

    /// New conversations whose create is not going through, oldest first: they
    /// have no conversation to show it in yet.
    var createNotices: [SendNotice] {
        sends.values.filter { $0.kind == .conversationCreate }.sorted { $0.order < $1.order }
            .compactMap { sendNotices[$0.key] }
    }

    /// The worst notice among a conversation's messages, for its sidebar row.
    func sendProblem(in conversationID: String) -> SendNotice.Kind? {
        let kinds = sends.values.filter { $0.kind == .messageSubmit && $0.conversation == conversationID }
            .compactMap { sendNotice(conversationID: conversationID, messageID: $0.key)?.kind }
        for kind in [SendNotice.Kind.needsPerson, .refused, .retrying] where kinds.contains(kind) { return kind }
        return nil
    }

    /// A message the person withdrew before the daemon had it: no receipt comes.
    mutating func withdrawLocal(messageID: String) {
        for key in timelines.keys where timelines[key]?.turn(messageID) != nil {
            timelines[key]?.withdrawLocal(messageID: messageID)
        }
    }

    /// Fold an outbox report: receipts, and the conversations creates made.
    mutating func apply(outbox report: OutboxSender.Report, outbox: Outbox) {
        for receipt in report.receipts {
            apply(receipt: receipt)
            if let cid = receipt.conversation_id, let text = outbox.text(of: receipt.message_id) {
                timelines[cid]?.setPersonText(text, for: receipt.message_id)
            }
        }
        for conversation in report.conversations { upsert(conversation) }
    }

    enum WatchResult: Equatable {
        case applied(Int)
        case superseded
    }

    /// The global change feed (D-24): approval counts, message states, and
    /// notifications for conversations that are not focused.
    @discardableResult
    mutating func apply(watch page: WatchPage) -> WatchResult {
        if page.superseded == true { return .superseded }
        var applied = 0
        for change in page.changes.sorted(by: { $0.seq < $1.seq }) where change.seq > watchCursor {
            applied += 1
            let cid = change.conversation_id
            let before = pendingApprovals[cid] ?? 0
            pendingApprovals[cid] = change.pending_approvals
            if let mid = change.message_id, let state = change.state.flatMap(MessageState.init(rawValue:)) {
                if MessageState.live.contains(state) { liveMessages[cid, default: []].insert(mid) }
                else { liveMessages[cid]?.remove(mid) }
            }
            if let index = conversations.firstIndex(where: { $0.conversation_id == cid }) {
                conversations[index].pending_approvals = change.pending_approvals
                if let mid = change.message_id, let state = change.state {
                    // A message first appears as `queued`, so a queued change names the newest one.
                    let previous = conversations[index].last_message
                    if previous == nil || previous?.message_id == mid || state == MessageState.queued.rawValue {
                        conversations[index].last_message = LastMessage(message_id: mid, state: state,
                                                                        state_reason: change.state_reason,
                                                                        updated_at: change.ts)
                    }
                    conversations[index].active = !(liveMessages[cid] ?? []).isEmpty
                }
            }
            if let mid = change.message_id, let state = change.state, var timeline = timelines[cid],
               let turn = timeline.turn(mid), turn.state != state || turn.stateReason != change.state_reason {
                // A reason can change while the state stays (a deferral, then a hold).
                timeline.apply(receipt: Receipt(message_id: mid, conversation_id: cid, state: state,
                                                state_reason: change.state_reason))
                timelines[cid] = timeline
            }
            if watchBaselined && cid != focusedConversationID {
                notify(change, pendingBefore: before)
            }
            watchCursor = max(watchCursor, change.seq)
        }
        watchCursor = max(watchCursor, page.next)
        if page.changes.isEmpty { watchBaselined = true }
        return .applied(applied)
    }

    private mutating func notify(_ change: ConversationChange, pendingBefore: Int) {
        let title = conversation(change.conversation_id).map(conversationTitle) ?? change.conversation_id
        var intent: NotificationIntent?
        if change.pending_approvals > pendingBefore {
            intent = NotificationIntent(id: "approval:\(change.seq)", kind: .approval, conversationID: change.conversation_id,
                                        messageID: change.message_id, title: "Approval needed", body: title)
        } else if let mid = change.message_id, let state = change.state.flatMap(MessageState.init(rawValue:)) {
            switch state {
            case .complete:
                intent = NotificationIntent(id: "complete:\(mid)", kind: .completed, conversationID: change.conversation_id,
                                            messageID: mid, title: "Turn completed", body: title)
            case .failed:
                intent = NotificationIntent(id: "failed:\(mid)", kind: .failed, conversationID: change.conversation_id,
                                            messageID: mid, title: "Turn failed", body: title)
            case .deliveryUnknown:
                intent = NotificationIntent(id: "delivery-unknown:\(mid)", kind: .deliveryUnknown,
                                            conversationID: change.conversation_id, messageID: mid,
                                            title: "Delivery unknown", body: title)
            default:
                break
            }
        }
        guard let intent, !notified.contains(intent.id) else { return }
        notified.insert(intent.id)
        notifications.append(intent)
    }

    mutating func drainNotifications() -> [NotificationIntent] {
        defer { notifications = [] }
        return notifications
    }

    // MARK: Sidebar

    func conversationTitle(_ conversation: Conversation) -> String {
        if let title = conversation.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty { return title }
        let folder = URL(fileURLWithPath: conversation.workspace).lastPathComponent
        return folder.isEmpty ? conversation.conversation_id : folder
    }

    /// Subfleet conversations and native sessions, filtered by the search text
    /// and provider (the daemon's `query` filters only the catalog, so the
    /// conversations are filtered here too).
    func sidebarEntries() -> [SidebarEntry] {
        let needle = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        func matches(_ fields: [String?]) -> Bool {
            needle.isEmpty || fields.contains { ($0 ?? "").lowercased().contains(needle) }
        }
        var entries: [SidebarEntry] = []
        for conversation in conversations where providerFilter == nil || conversation.provider == providerFilter {
            let title = conversationTitle(conversation)
            guard matches([title, conversation.workspace, conversation.title]) else { continue }
            entries.append(SidebarEntry(
                id: "cv:" + conversation.conversation_id, target: .conversation(conversation.conversation_id),
                provider: conversation.provider, title: title, subtitle: abbreviatedPath(conversation.workspace),
                workspace: conversation.workspace, date: parseTimestamp(conversation.updated_at),
                pendingApprovals: pendingApprovals[conversation.conversation_id] ?? conversation.pending_approvals,
                active: conversation.active, blockedBy: conversation.blocked_by,
                liveElsewhere: conversation.live_elsewhere ?? false,
                continuable: true, continueBlocker: nil,
                sendProblem: sendProblem(in: conversation.conversation_id)))
        }
        let bound = Set(conversations.compactMap { c in c.native_session_id.map { "\(c.provider):\($0)" } })
        for item in catalog?.items ?? [] where providerFilter == nil || item.provider == providerFilter {
            guard !bound.contains("\(item.provider):\(item.native_session_id)") else { continue }
            let prompt = item.first_prompt?.trimmingCharacters(in: .whitespacesAndNewlines)
            let title = [item.title, prompt].compactMap { $0 }.first { !$0.isEmpty } ?? String(item.native_session_id.prefix(8))
            guard matches([title, item.cwd, item.first_prompt]) else { continue }
            entries.append(SidebarEntry(
                id: item.id,
                target: .native(NativeSessionRef(provider: item.provider, session_id: item.native_session_id, home: item.home)),
                provider: item.provider, title: title, subtitle: item.cwd.map { abbreviatedPath($0) } ?? "",
                workspace: item.cwd, date: item.mtime.map { Date(timeIntervalSince1970: $0) }, pendingApprovals: 0,
                active: false, blockedBy: nil, liveElsewhere: item.live_elsewhere ?? false,
                continuable: item.continuable ?? true, continueBlocker: item.continue_blocker))
        }
        return entries.sorted { ($0.date ?? .distantPast) > ($1.date ?? .distantPast) }
    }

    func sidebar(now: Date = Date(), calendar: Calendar = .current) -> [SidebarSection] {
        let entries = sidebarEntries()
        switch grouping {
        case .recency:
            let buckets = ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older", "Undated"]
            var grouped: [String: [SidebarEntry]] = [:]
            let today = calendar.startOfDay(for: now)
            for entry in entries {
                let bucket: String
                if let date = entry.date {
                    let day = calendar.startOfDay(for: date)
                    let days = calendar.dateComponents([.day], from: day, to: today).day ?? 0
                    bucket = days <= 0 ? "Today" : days == 1 ? "Yesterday" : days <= 7 ? "Previous 7 days"
                        : days <= 30 ? "Previous 30 days" : "Older"
                } else {
                    bucket = "Undated"
                }
                grouped[bucket, default: []].append(entry)
            }
            return buckets.compactMap { bucket in
                grouped[bucket].map { SidebarSection(id: bucket, title: bucket, entries: $0) }
            }
        case .workspace:
            var grouped: [String: [SidebarEntry]] = [:]
            var order: [String] = []
            for entry in entries {
                let key = entry.workspace ?? ""
                if grouped[key] == nil { order.append(key) }
                grouped[key, default: []].append(entry)
            }
            return order.map { key in
                SidebarSection(id: "ws:" + key, title: key.isEmpty ? "No workspace" : abbreviatedPath(key),
                               entries: grouped[key] ?? [])
            }
        }
    }

    // MARK: Composer, stop, banner, chip

    func composerOptions(for conversationID: String) -> ComposerOptions? {
        guard let conversation = conversation(conversationID) else { return nil }
        return makeComposerOptions(provider: conversation.provider, settings: conversation.settings,
                                   models: models[conversation.provider] ?? [], capabilities: availability.capabilities)
    }

    func blockedBanner(for conversationID: String) -> BlockedBanner? {
        guard let conversation = conversation(conversationID) else { return nil }
        return makeBlockedBanner(for: conversation, timeline: timelines[conversationID])
    }

    func servedChip(conversationID: String, messageID: String) -> ServedChip? {
        guard let conversation = conversation(conversationID), let turn = timelines[conversationID]?.turn(messageID) else {
            return nil
        }
        return makeServedChip(for: turn, provider: conversation.provider, laneLabels: laneLabels)
    }
}

// MARK: - Engine

enum ConversationEngineError: Error, Equatable {
    /// Allowing while masked values are hidden needs the person to look first.
    case maskedValuesNeedReview([MaskedSpan])
    /// A wider permission needs the confirmation sheet first.
    case widenNeedsConfirmation(from: String, to: String)
    case notOffered(String)
}

/// The daemon calls behind the store. Blocking; confine to one serial queue
/// (it owns the outbox).
final class ConversationEngine {
    let client: DaemonCalling
    let outbox: Outbox
    let sender: OutboxSender
    /// Long-poll wait for events and the watch feed (design §5: at most 50).
    var pollWait: Double = 25
    /// Closed outbox entries kept after each pump (their text feeds the timeline).
    var keptClosedEntries = 200
    /// How long a cancel the daemon answered `dispatching` waits before each try
    /// again: the dispatcher is binding the message to its turn job, and the
    /// daemon's fix is "send the cancel again in a moment" (service.py `_cancel`).
    var dispatchingRetries: [TimeInterval] = [0.1, 0.25, 0.5, 1]
    /// How the engine waits between those tries (a test passes one that does not sleep).
    var pause: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) }

    init(client: DaemonCalling, outbox: Outbox) {
        self.client = client
        self.outbox = outbox
        self.sender = OutboxSender(outbox: outbox, client: client)
    }

    func checkAvailability() -> DaemonAvailability { DaemonAvailability.check(client) }

    func list(query: String? = nil, provider: String? = nil, limit: Int = 200) throws -> ConversationListResult {
        let query = query?.trimmingCharacters(in: .whitespacesAndNewlines)
        return try client.call(Ops.conversationList, ConversationListArgs(
            provider: provider, query: query?.isEmpty == false ? query : nil, limit: limit, include_catalog: true))
    }

    func models(provider: String) throws -> ModelsListResult {
        try client.call(Ops.modelsList, ModelsListArgs(provider: provider))
    }

    /// Open a conversation or continue a native session (`native:{provider,
    /// session_id}` creates its conversation once). Records the conversation's
    /// last person message for the outbox.
    func open(_ target: SidebarEntry.Target) throws -> ConversationOpenResult {
        let args: ConversationOpenArgs
        switch target {
        case .conversation(let id): args = .conversation(id)
        case .native(let native): args = .native(provider: native.provider, sessionID: native.session_id, home: native.home)
        }
        let open = try client.call(Ops.conversationOpen, args)
        let cid = open.conversation.conversation_id
        if !outbox.pending(in: cid).contains(where: { $0.state == .sending }) {
            try outbox.knowChain(cid, lastPersonMessageID: Outbox.lastPersonMessage(in: open.messages))
        }
        return open
    }

    /// One events poll for the focused conversation; `wait` 0 while catching up.
    /// The daemon keeps one events poll per client, whatever the conversation
    /// (service `_slot`, C-29.9): any call here supersedes the app's running long
    /// poll, whose answer then says `superseded`. Only the focused loop calls it.
    func events(conversationID: String, after: Int, wait: Double? = nil) throws -> EventsPage {
        try client.call(Ops.conversationEvents, ConversationEventsArgs(conversation_id: conversationID, after: after,
                                                                        limit: nil, wait_s: wait ?? pollWait))
    }

    func watch(after: Int, wait: Double? = nil) throws -> WatchPage {
        try client.call(Ops.conversationWatch, ConversationWatchArgs(after: after, wait_s: wait ?? pollWait))
    }

    func runs(conversationID: String, limit: Int = 50) throws -> [RunSummary] {
        try client.call(Ops.conversationRuns, ConversationRunsArgs(conversation_id: conversationID, limit: limit)).runs
    }

    /// What one turn changed (C-26.14): its start and end snapshots, or its start
    /// and the working tree now while it runs.
    func turnDiff(messageID: String, path: String? = nil) throws -> DiffResult {
        try client.call(Ops.turnDiff, TurnDiffArgs(message_id: messageID, path: path))
    }

    /// What the conversation changed since its first writable turn started.
    func conversationDiff(conversationID: String, path: String? = nil) throws -> DiffResult {
        try client.call(Ops.conversationDiff, ConversationDiffArgs(conversation_id: conversationID, path: path))
    }

    func history(conversationID: String, before: Int?, limit: Int = 50) throws -> HistoryPage {
        try client.call(Ops.conversationHistory, ConversationHistoryArgs(conversation_id: conversationID, before: before,
                                                                          limit: limit))
    }

    func approvals(conversationID: String?) throws -> [ApprovalView] {
        try client.call(Ops.approvalList, ApprovalListArgs(conversation_id: conversationID)).approvals
    }

    func status(_ messageIDs: [String]) throws -> [Receipt] {
        try client.call(Ops.messageStatus, MessageStatusArgs(message_ids: messageIDs)).messages
    }

    /// Journal a new conversation; the outbox sends it. Returns the draft key its
    /// first messages are journaled under.
    func createConversation(provider: String, workspace: String, settings: ConversationSettings,
                            title: String? = nil, workspaceKind: String = "in-place", allowMain: Bool = false,
                            confirmWiden: Bool = false, requestID: String = "app-" + UUID().uuidString.lowercased()) throws -> String {
        if PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: settings.permission) && !confirmWiden {
            throw ConversationEngineError.widenNeedsConfirmation(from: PermissionPolicy.ask.rawValue, to: settings.permission)
        }
        let args = ConversationCreateArgs(request_id: requestID, provider: provider, workspace: workspace,
                                          workspace_kind: workspaceKind, allow_main: allowMain ? true : nil, title: title,
                                          settings: settings, confirm_widen: confirmWiden ? true : nil)
        try outbox.enqueueCreate(args)
        return Outbox.draftKey(requestID)
    }

    /// Journal a message (sending is `pump`). `conversation` is a daemon id or
    /// the draft key `createConversation` returned.
    func send(conversation: String, text: String, staged: [StagedAttachment] = [], settings: ConversationSettings,
              messageID: String = Outbox.newMessageID()) throws -> OutboxEntry {
        try outbox.enqueueSubmit(conversation: conversation, messageID: messageID, text: text, staged: staged,
                                 settings: settings)
    }

    /// Send what the outbox holds; keep the newest `keptClosedEntries` closed
    /// entries and drop older ones (open entries are never dropped).
    func pump() -> OutboxSender.Report {
        let report = sender.pump()
        try? outbox.prune(keep: keptClosedEntries)
        return report
    }

    func withdraw(_ messageID: String) throws -> OutboxSender.WithdrawOutcome {
        try sender.withdraw(messageID)
    }

    /// The outbox's open entries, for the conversation views (C-29.12).
    func openSends() -> [OutboxEntry] { outbox.entries.filter(\.isOpen) }

    /// The person's "Send now" or "Try again" on a journaled send; `pump` sends it.
    func sendNow(_ key: String) throws { try outbox.sendNow(key) }

    enum WithdrawResult: Equatable {
        /// Gone: with the tombstone's receipt when it had been sent, none when never.
        case withdrawn(Receipt?)
        /// The daemon had it: Stop acted there (the receipt, or nil when nothing was left to stop).
        case stopped(Receipt?)
        /// A send is under way; ask again when it has an answer.
        case inFlight
    }

    /// Withdraw a journaled message (D-22), or stop it where the daemon has it.
    func withdrawSend(_ messageID: String) throws -> WithdrawResult {
        switch try withdraw(messageID) {
        case .withdrawn(let receipt): return .withdrawn(receipt)
        case .inDaemon(let receipt):
            return .stopped(try stop(stopAction(for: messageID, state: receipt.state, outboxEntry: nil)))
        case .inFlight: return .inFlight
        }
    }

    /// Stop a message: cancel it while queued, interrupt it once it runs.
    func stop(_ action: StopAction) throws -> Receipt? {
        switch action {
        case .none: return nil
        case .withdraw(let messageID):
            switch try withdrawSend(messageID) {
            case .withdrawn(let receipt), .stopped(let receipt): return receipt
            case .inFlight: return nil
            }
        case .cancel(let messageID):
            return try cancel(messageID)
        case .interrupt(let messageID):
            return try client.call(Ops.turnInterrupt, TurnInterruptArgs(message_id: messageID))
        }
    }

    /// `message.cancel` for a message the app saw queued (the queue tray's
    /// Withdraw). A `dispatching` refusal is tried again after a moment. On
    /// `too-late` the message left the queue since the app looked: it is stopped
    /// with `turn.interrupt` (the daemon's fix) only while it is its own live turn
    /// (`waiting`, `starting`, `running`, `approval-needed`). A message that has
    /// ended, was withdrawn already, or is in any other state the app does not
    /// stop gets its current receipt back, so Withdraw never stops anything but
    /// the message itself, and a second Withdraw is not an error.
    func cancel(_ messageID: String) throws -> Receipt? {
        var waits = dispatchingRetries
        while true {
            do {
                return try client.call(Ops.messageCancel, MessageCancelArgs(message_id: messageID, conversation_id: nil))
            } catch DaemonClientError.daemon(let refusal) where refusal.reason == "dispatching" && !waits.isEmpty {
                pause(waits.removeFirst())
            } catch DaemonClientError.daemon(let refusal) where refusal.reason == "too-late" {
                return try stopIfRunning(messageID)
            }
        }
    }

    /// `turn.interrupt` for a message the daemon says is its own live turn; its
    /// current receipt otherwise (`message.status`). One that ends between the
    /// two answers `not-running`, and its receipt then says how it ended.
    private func stopIfRunning(_ messageID: String) throws -> Receipt? {
        guard let receipt = try status([messageID]).first(where: { $0.message_id == messageID }) else {
            throw DaemonClientError.malformed("message.status left out \(messageID)")
        }
        switch receipt.messageState {
        case .waiting?, .starting?, .running?, .approvalNeeded?:
            do {
                return try client.call(Ops.turnInterrupt, TurnInterruptArgs(message_id: messageID))
            } catch DaemonClientError.daemon(let refusal) where refusal.reason == "not-running" {
                return try status([messageID]).first { $0.message_id == messageID }
            }
        default:
            return receipt
        }
    }

    /// `approval.get` (person-only): the request the person must see before answering.
    func approvalDetail(_ approvalID: String, reveal: Bool = false) throws -> ApprovalDetail {
        try client.call(Ops.approvalGet, ApprovalGetArgs(approval_id: approvalID, reveal: reveal ? true : nil))
    }

    /// `approval.respond`. An allowing answer while masked spans are hidden needs
    /// `reviewedMasked` (the person revealed or explicitly confirmed them): no
    /// one-tap allow over values the person could not see.
    func respond(to detail: ApprovalDetail, decision: String, answers: [String: String]? = nil, message: String? = nil,
                 reviewedMasked: Bool = false) throws -> ApprovalRespondResult {
        guard detail.approval.options.contains(decision) else { throw ConversationEngineError.notOffered(decision) }
        let allowing = ["allow", "allow-session", "allow-turn", "answer"].contains(decision)
        if allowing && !detail.masked.isEmpty && !reviewedMasked {
            throw ConversationEngineError.maskedValuesNeedReview(detail.masked)
        }
        return try client.call(Ops.approvalRespond, ApprovalRespondArgs(
            approval_id: detail.approval.approval_id, nonce: detail.nonce, request_sha256: detail.request_sha256,
            decision: decision, answers: answers, message: message))
    }

    func perform(_ choice: BlockedChoice.Action, conversationID: String) throws -> (Conversation?, Receipt?) {
        switch choice {
        case .unblock(let choice):
            let result = try client.call(Ops.conversationUnblock, ConversationUnblockArgs(conversation_id: conversationID,
                                                                                           choice: choice))
            return (result.conversation, nil)
        case .resolve(let messageID, let resolution):
            return (nil, try client.call(Ops.messageResolve, MessageResolveArgs(message_id: messageID, resolution: resolution)))
        }
    }

    /// Change a conversation's settings; widening needs the person's confirmation first.
    func updateSettings(_ conversation: Conversation, to settings: ConversationSettings,
                        confirmedWiden: Bool = false) throws -> Conversation {
        let widening = PermissionPolicy.widens(from: conversation.settings.permission, to: settings.permission)
        if widening && !confirmedWiden {
            throw ConversationEngineError.widenNeedsConfirmation(from: conversation.settings.permission, to: settings.permission)
        }
        return try client.call(Ops.conversationSettings, ConversationSettingsArgs(
            conversation_id: conversation.conversation_id, settings: settings,
            confirm_widen: widening ? true : nil, allow_main: nil)).conversation
    }

    func refreshCatalog() throws -> CatalogRefreshResult {
        try client.call(Ops.catalogRefresh, NoArgs())
    }

    // MARK: Synchronous helpers (the frontend probes, and simple callers)

    /// Poll events without waiting until caught up, folding into `state`.
    /// A reset re-reads from 0 once.
    func catchUp(_ state: inout ConversationStoreState, conversationID: String, maxPages: Int = 64) throws {
        var resets = 0
        for _ in 0..<maxPages {
            let after = state.timelines[conversationID]?.cursor ?? 0
            let page = try events(conversationID: conversationID, after: after, wait: 0)
            switch state.apply(events: page, conversationID: conversationID) {
            case .superseded: return
            case .reset:
                resets += 1
                if resets > 2 { return }
            case .applied(let count):
                if count == 0 { return }
            }
        }
    }

    /// Drain the watch feed without waiting (the launch baseline, or a catch-up).
    func drainWatch(_ state: inout ConversationStoreState, maxPages: Int = 64) throws {
        for _ in 0..<maxPages {
            let page = try watch(after: state.watchCursor, wait: 0)
            guard case .applied(let count) = state.apply(watch: page), count > 0 else { return }
        }
    }
}

/// The app's `conversation.watch` loop (UIModel.startWatchLoop), apart from its
/// threads: each page is delivered; a failure is reported (the app then checks
/// availability) and paused on; and the first page after a failure asks for
/// availability to be checked again. The check made when the feed failed may have
/// been answered while the daemon was busy or restarting, and nothing else checks
/// once the feed answers again, so the app kept its banner and stopped sending
/// (review of the descriptor hotfix, F7).
struct WatchLoop {
    let engine: ConversationEngine
    /// Where to watch from; nil ends the loop.
    var cursor: () -> Int?
    var deliver: (WatchPage) -> Void
    /// A watch failed.
    var lost: (Error) -> Void
    /// A watch answered after one or more failed.
    var regained: () -> Void
    var pause: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) }

    /// The pause after the `failures`th failure in a row: 2 s more each time, at most 30 s.
    static func backoff(failures: Int) -> TimeInterval { min(30, Double(failures) * 2) }

    func run() {
        var failures = 0
        while let after = cursor() {
            do {
                let page = try engine.watch(after: after)
                deliver(page)
                if failures > 0 { regained() }
                failures = 0
            } catch {
                failures += 1
                lost(error)
                pause(WatchLoop.backoff(failures: failures))
            }
        }
    }
}
