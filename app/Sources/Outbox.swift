// Subfleet: the outbox keeps the person's sends in order (design D-22, C-24.2, C-28.3).
//
// Before anything is sent, `conversation.create` and `message.submit` are
// journaled under their idempotency keys (the create's `request_id`, the
// message's client UUID) in a JSON file under Application Support. Per
// conversation, at most one `message.submit` is outstanding, sent in journal
// order, each carrying `after_message_id`: the last person message the daemon
// accepted in that conversation (null for the first). A retry resends the same
// key; the daemon answers a repeat with the stored receipt. `out-of-order`
// (exit 2) means the app's idea of the predecessor is stale: it re-reads the
// conversation and tries again. A send that may have reached the daemon is
// withdrawn only after `message.status` shows the daemon never received it,
// and then through `message.cancel`, which leaves a tombstone so a late copy
// cannot land.
//
// A steer (C-24.9, design §1) is journaled too, keyed by its message id: the
// app's steer is `message.submit` then `message.steer`, sent in journal order
// in the same one-at-a-time lane as its conversation's submits. A refusal
// closes it without holding the conversation: the message stays queued.
//
// The other mutating ops (settings, stop, unblock, resolve, approval answers)
// are a person's immediate actions without an idempotency key; they are sent
// directly and not replayed after a restart (docs/desktop/app-needs.md).
//
// Not thread-safe: confine an Outbox and its sender to one serial queue.

import Foundation

struct OutboxMessage: Codable, Equatable {
    var text: String
    var attachments: [String]
    var settings: ConversationSettings
    /// Images the app staged: registered with `attachment.add` (idempotent by
    /// content) right before each submit, so a resend after a restart still finds them.
    var staged: [StagedAttachment]?
}

struct OutboxFailure: Codable, Equatable {
    var code: Int?
    var reason: String?
    var message: String
    /// Whether the outbox will try again on its own.
    var retryable: Bool
}

struct OutboxEntry: Codable, Equatable, Identifiable {
    enum Kind: String, Codable {
        case conversationCreate = "conversation.create"
        case messageSubmit = "message.submit"
    }

    enum State: String, Codable {
        /// Journaled. Not sent yet, or sent without an answer (`attempts` > 0).
        case queued
        /// A send is under way; journaled before the socket write.
        case sending
        /// The daemon's receipt is recorded.
        case acknowledged
        /// The daemon refused it on its merits. Later messages of its
        /// conversation wait until the person retries or discards it.
        case failed
        /// Removed by the person before the daemon had it (or tombstoned there).
        case withdrawn
    }

    var key: String
    var kind: Kind
    var order: Int
    /// The daemon's conversation id, or `draft:<request_id>` until that create is acknowledged.
    var conversation: String
    var create: ConversationCreateArgs?
    var message: OutboxMessage?
    var state: State
    var attempts: Int = 0
    /// The predecessor the last send carried.
    var lastAfterMessageID: String?
    /// The last answer was `out-of-order`; the conversation is re-read first.
    var waitingForPredecessor = false
    /// Seconds since 1970 before which a retry waits.
    var nextAttemptAt: Double?
    var failure: OutboxFailure?
    var receipt: Receipt?
    /// For a create: the conversation the daemon made (or had already made).
    var conversationID: String?
    var createdAt: String

    var id: String { key }
    var isOpen: Bool { state == .queued || state == .sending || state == .failed }
}

/// A person's steer of one message into the running turn (C-24.9). Kept apart
/// from `entries` so an older app still opens the journal: it ignores this list,
/// drops it on its next save, and the message simply stays queued.
struct OutboxSteer: Codable, Equatable, Identifiable {
    enum State: String, Codable {
        /// Journaled. Not sent yet, or sent without an answer (`attempts` > 0).
        case queued
        /// A send is under way; journaled before the socket write.
        case sending
        /// The daemon's receipt is recorded.
        case acknowledged
        /// Refused (or never answered): the message stays queued; `failure` says why.
        case refused
        /// Taken back unsent: its message was withdrawn or cancelled first.
        case withdrawn
    }

    static let keyPrefix = "steer:"

    var messageID: String
    var conversation: String
    var order: Int
    var state: State
    var attempts: Int = 0
    var nextAttemptAt: Double?
    var failure: OutboxFailure?
    var receipt: Receipt?
    var createdAt: String
    /// The running turn it was sent to (C-24.9); absent from older journals.
    var into: String?

    var key: String { OutboxSteer.keyPrefix + messageID }
    var id: String { key }
    var isOpen: Bool { state == .queued || state == .sending }
}

/// What the outbox knows of a conversation's last accepted person message.
struct OutboxChain: Codable, Equatable {
    var lastPersonMessageID: String?
}

struct OutboxJournal: Codable, Equatable {
    var version = 1
    var nextOrder = 1
    var entries: [OutboxEntry] = []
    /// Present for a conversation whose predecessor chain is known.
    var chains: [String: OutboxChain] = [:]
    /// Steer intents, one per message (C-24.9); absent from older journals.
    var steers: [OutboxSteer] = []

    init() {}

    private enum CodingKeys: String, CodingKey { case version, nextOrder, entries, chains, steers }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        nextOrder = try c.decode(Int.self, forKey: .nextOrder)
        entries = try c.decode([OutboxEntry].self, forKey: .entries)
        chains = try c.decode([String: OutboxChain].self, forKey: .chains)
        steers = try c.decodeIfPresent([OutboxSteer].self, forKey: .steers) ?? []
    }
}

/// One thing the outbox may send: a journaled create or submit, or a steer.
enum OutboxWork: Equatable {
    case entry(OutboxEntry)
    case steer(OutboxSteer)

    var key: String {
        switch self {
        case .entry(let entry): return entry.key
        case .steer(let steer): return steer.key
        }
    }
}

enum OutboxRequest: Equatable {
    case create(ConversationCreateArgs)
    case submit(MessageSubmitArgs)
}

enum OutboxOutcome {
    case created(ConversationCreateResult)
    case submitted(Receipt)
    case failed(DaemonClientError)
}

enum OutboxSteerOutcome {
    case steered(Receipt)
    case failed(DaemonClientError)
}

enum OutboxError: Error, Equatable {
    case unknownEntry(String)
    case notSendable(String)
    case badMessageID(String)
    case invalidState(String)
}

final class Outbox {
    static let draftPrefix = "draft:"
    /// A steer tried this many times (no answer, or a turn that could not take it
    /// yet) is given up: it is worth something only while the turn runs, and it may
    /// hold the messages the person sends after it only briefly (0.5 s doubling:
    /// 7.5 s at most). The message stays queued; nothing is lost either way.
    static let steerAttempts = 5

    private(set) var journal: OutboxJournal
    let url: URL?
    var now: () -> Date

    /// Load the journal. A send that was under way when the app stopped is
    /// queued again: its fate is unknown, and resending its key is safe.
    init(url: URL?, now: @escaping () -> Date = Date.init) throws {
        self.url = url
        self.now = now
        if let url, FileManager.default.fileExists(atPath: url.path) {
            let data = try Data(contentsOf: url)
            journal = try JSONDecoder().decode(OutboxJournal.self, from: data)
            var recovered = false
            for index in journal.entries.indices where journal.entries[index].state == .sending {
                journal.entries[index].state = .queued
                recovered = true
            }
            for index in journal.steers.indices where journal.steers[index].state == .sending {
                journal.steers[index].state = .queued
                recovered = true
            }
            if recovered { try save() }
        } else {
            journal = OutboxJournal()
        }
    }

    var entries: [OutboxEntry] { journal.entries }

    func entry(_ key: String) -> OutboxEntry? { journal.entries.first { $0.key == key } }

    /// Open entries of one conversation (by daemon id or draft key), in order.
    func pending(in conversation: String) -> [OutboxEntry] {
        journal.entries.filter { $0.conversation == conversation && $0.isOpen }.sorted { $0.order < $1.order }
    }

    static func newMessageID() -> String { UUID().uuidString.lowercased() }

    static func draftKey(_ requestID: String) -> String { draftPrefix + requestID }

    private func index(_ key: String) throws -> Int {
        guard let index = journal.entries.firstIndex(where: { $0.key == key }) else { throw OutboxError.unknownEntry(key) }
        return index
    }

    func save() throws {
        guard let url else { return }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try atomicWrite(try encoder.encode(journal), to: url)
    }

    private func stamp() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: now())
    }

    // MARK: Journaling

    /// Journal a new conversation (idempotent by `request_id`).
    @discardableResult
    func enqueueCreate(_ args: ConversationCreateArgs) throws -> OutboxEntry {
        if let existing = entry(args.request_id) { return existing }
        let entry = OutboxEntry(key: args.request_id, kind: .conversationCreate, order: journal.nextOrder,
                                conversation: Outbox.draftKey(args.request_id), create: args, message: nil,
                                state: .queued, createdAt: stamp())
        journal.nextOrder += 1
        journal.entries.append(entry)
        try save()
        return entry
    }

    /// Journal a message for a conversation (its daemon id, or the draft key of
    /// a journaled create). Idempotent by message id. With `steer`, its steer is
    /// journaled right behind it in the same write (C-24.9): submit, then steer.
    @discardableResult
    func enqueueSubmit(conversation: String, messageID: String = Outbox.newMessageID(), text: String,
                       attachments: [String] = [], staged: [StagedAttachment] = [],
                       settings: ConversationSettings, steer: Bool = false, into: String? = nil) throws -> OutboxEntry {
        try Outbox.checkMessageID(messageID)
        if let existing = entry(messageID) {
            if steer && !existing.conversation.hasPrefix(Outbox.draftPrefix) {
                try enqueueSteer(conversation: existing.conversation, messageID: messageID, into: into)
            }
            return existing
        }
        let entry = OutboxEntry(key: messageID, kind: .messageSubmit, order: journal.nextOrder,
                                conversation: conversation,
                                create: nil,
                                message: OutboxMessage(text: text, attachments: staged.map(\.sha256) + attachments,
                                                       settings: settings, staged: staged.isEmpty ? nil : staged),
                                state: .queued, createdAt: stamp())
        journal.nextOrder += 1
        journal.entries.append(entry)
        if steer && !conversation.hasPrefix(Outbox.draftPrefix) {
            appendSteer(conversation: conversation, messageID: messageID, into: into)
        }
        try save()
        return entry
    }

    private static func checkMessageID(_ messageID: String) throws {
        guard let parsed = UUID(uuidString: messageID), parsed.uuidString.lowercased() == messageID else {
            throw OutboxError.badMessageID(messageID)
        }
    }

    // MARK: Steers (C-24.9)

    func steer(_ messageID: String) -> OutboxSteer? { journal.steers.first { $0.messageID == messageID } }

    var steers: [OutboxSteer] { journal.steers }

    /// A conversation's steers, in journal order.
    func steers(in conversation: String) -> [OutboxSteer] {
        journal.steers.filter { $0.conversation == conversation }.sorted { $0.order < $1.order }
    }

    /// Journal a steer of a message the daemon has, or that is journaled ahead
    /// of it. Idempotent by message id while one is open; a closed one (refused,
    /// withdrawn, or answered and since back in the queue) is journaled again at
    /// the end, since the person asked again.
    @discardableResult
    func enqueueSteer(conversation: String, messageID: String, into: String? = nil) throws -> OutboxSteer {
        try Outbox.checkMessageID(messageID)
        guard !conversation.hasPrefix(Outbox.draftPrefix) else {
            throw OutboxError.notSendable("\(messageID): a conversation not created yet has no running turn")
        }
        if let existing = steer(messageID), existing.isOpen { return existing }
        let steer = appendSteer(conversation: conversation, messageID: messageID, into: into)
        try save()
        return steer
    }

    @discardableResult
    private func appendSteer(conversation: String, messageID: String, into: String?) -> OutboxSteer {
        let steer = OutboxSteer(messageID: messageID, conversation: conversation, order: journal.nextOrder,
                                state: .queued, createdAt: stamp(), into: into)
        journal.nextOrder += 1
        journal.steers.removeAll { $0.messageID == messageID }
        journal.steers.append(steer)
        return steer
    }

    private func steerIndex(_ messageID: String) throws -> Int {
        guard let index = journal.steers.firstIndex(where: { $0.messageID == messageID }) else {
            throw OutboxError.unknownEntry(OutboxSteer.keyPrefix + messageID)
        }
        return index
    }

    /// Mark a steer as being sent (journaled first) and build its request.
    func beginSteer(_ messageID: String) throws -> MessageSteerArgs {
        let index = try steerIndex(messageID)
        guard journal.steers[index].state == .queued else {
            throw OutboxError.notSendable("steer of \(messageID) is \(journal.steers[index].state.rawValue)")
        }
        journal.steers[index].state = .sending
        journal.steers[index].attempts += 1
        try save()
        return MessageSteerArgs(message_id: messageID, into: journal.steers[index].into)
    }

    /// Record the daemon's answer to a steer. A refusal closes it: the message
    /// stays queued in the daemon, and nothing later in the conversation waits.
    /// A lost answer, or `not-steerable` for a steer that names its turn, is
    /// asked again briefly first (`steerAttempts`).
    @discardableResult
    func finishSteer(_ messageID: String, _ outcome: OutboxSteerOutcome) throws -> OutboxSteer {
        let index = try steerIndex(messageID)
        var steer = journal.steers[index]
        switch outcome {
        case .steered(let receipt):
            steer.state = .acknowledged
            steer.receipt = receipt
            steer.failure = nil
            steer.nextAttemptAt = nil
        case .failed(let error):
            let daemon = error.daemonError
            steer.failure = OutboxFailure(code: daemon?.code, reason: daemon?.reason, message: error.summary, retryable: false)
            steer.nextAttemptAt = nil
            if let daemon, daemon.isUnknownOp {
                steer.state = .refused
                steer.failure?.reason = "unsupported"
            } else if error.isRetryable || { if case .endpointRefused = error { return true }; return false }() {
                if steer.attempts >= Outbox.steerAttempts {
                    steer.state = .refused
                    steer.failure?.reason = "no-answer"
                } else {
                    steer.state = .queued
                    steer.failure?.retryable = true
                    steer.nextAttemptAt = now().timeIntervalSince1970 + Outbox.backoff(attempts: steer.attempts)
                }
            } else if daemon?.reason == "not-steerable" && steer.into != nil && steer.attempts < Outbox.steerAttempts {
                // The turn could not take a steer at that moment: its runner still
                // catching up after a daemon restart, or the provider not ready yet.
                // It is asked again briefly (0.5 s doubling, `steerAttempts` in all);
                // `into` has the daemon refuse any try that comes after that turn ended.
                steer.state = .queued
                steer.failure?.retryable = true
                steer.nextAttemptAt = now().timeIntervalSince1970 + Outbox.backoff(attempts: steer.attempts)
            } else {
                steer.state = .refused
            }
        }
        journal.steers[index] = steer
        try save()
        return steer
    }

    /// Take back a steer not yet sent (its message is being withdrawn or
    /// cancelled). Returns whether one was open.
    @discardableResult
    func withdrawSteer(_ messageID: String) throws -> Bool {
        guard let index = journal.steers.firstIndex(where: { $0.messageID == messageID }),
              journal.steers[index].state == .queued else { return false }
        journal.steers[index].state = .withdrawn
        try save()
        return true
    }

    // MARK: Predecessor chains

    func chainKnown(_ conversationID: String) -> Bool { journal.chains[conversationID] != nil }

    func chain(_ conversationID: String) -> OutboxChain? { journal.chains[conversationID] }

    /// Record the daemon's last person message in a conversation (from
    /// `conversation.open`), or nil for a conversation with none.
    func knowChain(_ conversationID: String, lastPersonMessageID: String?) throws {
        journal.chains[conversationID] = OutboxChain(lastPersonMessageID: lastPersonMessageID)
        for index in journal.entries.indices where journal.entries[index].conversation == conversationID {
            journal.entries[index].waitingForPredecessor = false
        }
        try save()
    }

    func forgetChain(_ conversationID: String) throws {
        journal.chains[conversationID] = nil
        try save()
    }

    /// The last person message among receipts (the daemon's `after_message_id`
    /// check counts only `origin: person`, store.submit_message).
    static func lastPersonMessage(in receipts: [Receipt]) -> String? {
        receipts.filter { $0.origin == "person" }.max { ($0.seq ?? 0) < ($1.seq ?? 0) }?.message_id
    }

    /// Conversations with a queued message whose predecessor is unknown: the
    /// sender re-reads them (`conversation.open`) before sending.
    func conversationsNeedingChain() -> [String] {
        var out: [String] = []
        for entry in journal.entries.sorted(by: { $0.order < $1.order })
        where entry.kind == .messageSubmit && entry.isOpen && !entry.conversation.hasPrefix(Outbox.draftPrefix)
            && !chainKnown(entry.conversation) && !out.contains(entry.conversation) {
            out.append(entry.conversation)
        }
        return out
    }

    // MARK: Scheduling

    /// What may be sent now: every due create, and per conversation its first
    /// open submit or steer (in journal order) when nothing of that conversation
    /// is outstanding or failed.
    func sendable(at date: Date? = nil) -> [OutboxWork] {
        let time = (date ?? now()).timeIntervalSince1970
        func due(_ at: Double?) -> Bool { (at ?? 0) <= time }
        let open = journal.entries.filter(\.isOpen).map { (order: $0.order, work: OutboxWork.entry($0)) }
            + journal.steers.filter(\.isOpen).map { (order: $0.order, work: OutboxWork.steer($0)) }
        var out: [OutboxWork] = []
        var seen: Set<String> = []
        for (_, work) in open.sorted(by: { $0.order < $1.order }) {
            switch work {
            case .entry(let entry) where entry.kind == .conversationCreate:
                if entry.state == .queued && due(entry.nextAttemptAt) { out.append(work) }
            case .entry(let entry):
                guard !seen.contains(entry.conversation) else { continue }
                seen.insert(entry.conversation)
                if entry.state == .queued, due(entry.nextAttemptAt), !entry.conversation.hasPrefix(Outbox.draftPrefix),
                   chainKnown(entry.conversation) {
                    out.append(work)
                }
            case .steer(let steer):
                guard !seen.contains(steer.conversation) else { continue }
                seen.insert(steer.conversation)
                if steer.state == .queued, due(steer.nextAttemptAt), !steer.conversation.hasPrefix(Outbox.draftPrefix) {
                    out.append(work)
                }
            }
        }
        return out
    }

    /// Mark an entry as being sent (journaled first) and build its request.
    func begin(_ key: String) throws -> OutboxRequest {
        let index = try index(key)
        var entry = journal.entries[index]
        guard entry.state == .queued else { throw OutboxError.notSendable("\(key) is \(entry.state.rawValue)") }
        let request: OutboxRequest
        switch entry.kind {
        case .conversationCreate:
            guard let create = entry.create else { throw OutboxError.invalidState("create without arguments") }
            request = .create(create)
        case .messageSubmit:
            guard let message = entry.message, !entry.conversation.hasPrefix(Outbox.draftPrefix),
                  let chain = journal.chains[entry.conversation] else {
                throw OutboxError.notSendable("\(key) waits for its conversation or its predecessor")
            }
            entry.lastAfterMessageID = chain.lastPersonMessageID
            request = .submit(MessageSubmitArgs(conversation_id: entry.conversation, message_id: key,
                                                after_message_id: chain.lastPersonMessageID, text: message.text,
                                                attachments: message.attachments, settings: message.settings))
        }
        entry.state = .sending
        entry.attempts += 1
        journal.entries[index] = entry
        try save()
        return request
    }

    /// Record the daemon's answer to a send.
    @discardableResult
    func finish(_ key: String, _ outcome: OutboxOutcome) throws -> OutboxEntry {
        let index = try index(key)
        var entry = journal.entries[index]
        switch outcome {
        case .created(let result):
            let cid = result.conversation.conversation_id
            entry.state = .acknowledged
            entry.conversationID = cid
            entry.failure = nil
            entry.nextAttemptAt = nil
            journal.entries[index] = entry
            let draft = Outbox.draftKey(key)
            for other in journal.entries.indices where journal.entries[other].conversation == draft {
                journal.entries[other].conversation = cid
            }
            for other in journal.steers.indices where journal.steers[other].conversation == draft {
                journal.steers[other].conversation = cid
            }
            if result.created {
                journal.chains[cid] = OutboxChain(lastPersonMessageID: nil)
            }
            // Otherwise an earlier attempt made it: its messages are read
            // (`conversation.open`) before the first send.
        case .submitted(let receipt):
            entry.receipt = receipt
            entry.failure = nil
            entry.nextAttemptAt = nil
            entry.waitingForPredecessor = false
            if receipt.isTombstone {
                entry.state = .withdrawn
                withdrawSteerUnsaved(key)
            } else {
                entry.state = .acknowledged
                journal.chains[entry.conversation] = OutboxChain(lastPersonMessageID: key)
            }
            journal.entries[index] = entry
        case .failed(let error):
            entry = classify(entry, error)
            journal.entries[index] = entry
            if entry.waitingForPredecessor { journal.chains[entry.conversation] = nil }
        }
        try save()
        return journal.entries[index]
    }

    /// 0.5 s doubling to 30 s.
    static func backoff(attempts: Int) -> TimeInterval {
        min(30, 0.5 * pow(2, Double(max(0, attempts - 1))))
    }

    private func classify(_ entry: OutboxEntry, _ error: DaemonClientError) -> OutboxEntry {
        var entry = entry
        let daemon = error.daemonError
        entry.failure = OutboxFailure(code: daemon?.code, reason: daemon?.reason, message: error.summary,
                                      retryable: true)
        if let daemon, daemon.isOutOfOrder {
            entry.state = .queued
            entry.waitingForPredecessor = true
            entry.nextAttemptAt = now().timeIntervalSince1970 + Outbox.backoff(attempts: entry.attempts)
        } else if error.isRetryable || { if case .endpointRefused = error { return true }; return false }() {
            entry.state = .queued
            entry.nextAttemptAt = now().timeIntervalSince1970 + Outbox.backoff(attempts: entry.attempts)
        } else {
            entry.state = .failed
            entry.failure?.retryable = false
            entry.nextAttemptAt = nil
        }
        return entry
    }

    // MARK: The person's choices

    /// Send a failed entry again, unchanged (for example after the daemon's
    /// capabilities changed).
    func retry(_ key: String) throws {
        let index = try index(key)
        guard journal.entries[index].state == .failed else { throw OutboxError.invalidState("only a failed send is retried") }
        journal.entries[index].state = .queued
        journal.entries[index].nextAttemptAt = nil
        try save()
    }

    enum WithdrawPlan: Equatable {
        /// Never sent: removed here.
        case withdrawn
        /// It may have reached the daemon: ask `message.status` first.
        case needsStatus(messageID: String, conversationID: String)
        /// A send is under way; ask again when it has an answer.
        case inFlight
        /// The daemon has it: stop it there (`message.cancel`, `turn.interrupt`).
        case acknowledged(Receipt?)
    }

    /// The person removes a message that has no receipt (D-22).
    func planWithdraw(_ key: String) throws -> WithdrawPlan {
        let index = try index(key)
        let entry = journal.entries[index]
        switch entry.state {
        case .withdrawn:
            return .withdrawn
        case .acknowledged:
            return .acknowledged(entry.receipt)
        case .sending:
            return .inFlight
        case .queued, .failed:
            if entry.attempts == 0 {
                journal.entries[index].state = .withdrawn
                withdrawSteerUnsaved(key)
                if entry.kind == .conversationCreate {
                    // Its messages can never be sent without it.
                    let draft = Outbox.draftKey(key)
                    for other in journal.entries.indices where journal.entries[other].conversation == draft
                        && journal.entries[other].isOpen {
                        journal.entries[other].state = .withdrawn
                        withdrawSteerUnsaved(journal.entries[other].key)
                    }
                }
                try save()
                return .withdrawn
            }
            guard entry.kind == .messageSubmit else {
                throw OutboxError.invalidState("a conversation that may exist is not withdrawn")
            }
            return .needsStatus(messageID: key, conversationID: entry.conversation)
        }
    }

    /// `message.status` said the daemon has the message: it is the person's
    /// message there now, and the predecessor chain moves to it.
    func acknowledge(_ key: String, receipt: Receipt) throws {
        try finish(key, .submitted(receipt))
    }

    /// The daemon never had it and now holds a tombstone (or it was never sent).
    func markWithdrawn(_ key: String, receipt: Receipt? = nil) throws {
        let index = try index(key)
        journal.entries[index].state = .withdrawn
        journal.entries[index].receipt = receipt ?? journal.entries[index].receipt
        withdrawSteerUnsaved(key)
        try save()
    }

    /// A withdrawn message's steer can never be sent.
    private func withdrawSteerUnsaved(_ messageID: String) {
        for index in journal.steers.indices where journal.steers[index].messageID == messageID
            && journal.steers[index].isOpen {
            journal.steers[index].state = .withdrawn
        }
    }

    /// Keep the newest `keep` closed entries (their text feeds the timeline) and
    /// the newest `keep` closed steers (their refusals feed its status lines).
    func prune(keep: Int = 200) throws {
        let closed = journal.entries.filter { !$0.isOpen }.sorted { $0.order > $1.order }
        let closedSteers = journal.steers.filter { !$0.isOpen }.sorted { $0.order > $1.order }
        guard closed.count > keep || closedSteers.count > keep else { return }
        let drop = Set(closed.dropFirst(keep).map(\.key))
        journal.entries.removeAll { drop.contains($0.key) }
        let dropSteers = Set(closedSteers.dropFirst(keep).map(\.messageID))
        journal.steers.removeAll { dropSteers.contains($0.messageID) && !$0.isOpen }
        try save()
    }

    /// The person's text of a message this app journaled.
    func text(of messageID: String) -> String? { entry(messageID)?.message?.text }
}

// MARK: - Sending

/// Drives an outbox against the daemon, one entry at a time.
final class OutboxSender {
    let outbox: Outbox
    let client: DaemonCalling
    /// Conversations the creates of the current pump made or found.
    private var created: [Conversation] = []

    struct Report: Equatable {
        var sent: [String] = []
        var acknowledged: [String] = []
        var failed: [String] = []
        var retrying: [String] = []
        var resynced: [String] = []
        /// Steers the daemon refused (or that never got an answer): closed, and
        /// holding nothing; their messages stay queued.
        var refused: [String] = []
        /// Receipts and created conversations, for the store to fold in.
        var receipts: [Receipt] = []
        var conversations: [Conversation] = []
        /// Steers answered in this pump, accepted or refused, for the timeline.
        var steers: [OutboxSteer] = []
    }

    init(outbox: Outbox, client: DaemonCalling) {
        self.outbox = outbox
        self.client = client
    }

    /// Read a conversation's last person message from the daemon.
    @discardableResult
    func resync(_ conversationID: String) throws -> ConversationOpenResult {
        let open = try client.call(Ops.conversationOpen, .conversation(conversationID))
        try outbox.knowChain(conversationID, lastPersonMessageID: Outbox.lastPersonMessage(in: open.messages))
        return open
    }

    /// Send one journaled entry and record the answer.
    @discardableResult
    func send(_ key: String) throws -> OutboxEntry {
        let request = try outbox.begin(key)
        do {
            switch request {
            case .create(let args):
                let result = try client.call(Ops.conversationCreate, args)
                created.append(result.conversation)
                return try outbox.finish(key, .created(result))
            case .submit(let args):
                for image in outbox.entry(key)?.message?.staged ?? [] {
                    _ = try client.call(Ops.attachmentAdd, AttachmentAddArgs(path: image.path, sha256: image.sha256))
                }
                return try outbox.finish(key, .submitted(try client.call(Ops.messageSubmit, args)))
            }
        } catch let error as DaemonClientError {
            return try outbox.finish(key, .failed(error))
        } catch {
            return try outbox.finish(key, .failed(.transport("\(error)")))
        }
    }

    /// Send one journaled steer and record the answer (C-24.9).
    @discardableResult
    func sendSteer(_ messageID: String) throws -> OutboxSteer {
        let args = try outbox.beginSteer(messageID)
        do {
            return try outbox.finishSteer(messageID, .steered(try client.call(Ops.messageSteer, args)))
        } catch let error as DaemonClientError {
            return try outbox.finishSteer(messageID, .failed(error))
        } catch {
            return try outbox.finishSteer(messageID, .failed(.transport("\(error)")))
        }
    }

    /// Send everything that may go now, in order, until nothing more can.
    func pump(maxSends: Int = 64) -> Report {
        var report = Report()
        created = []
        defer { created = [] }
        var sends = 0
        while sends < maxSends {
            for conversation in outbox.conversationsNeedingChain() where !report.resynced.contains(conversation) {
                if (try? resync(conversation)) != nil { report.resynced.append(conversation) }
            }
            let batch = outbox.sendable()
            if batch.isEmpty { break }
            var progressed = false
            for work in batch where sends < maxSends {
                sends += 1
                report.sent.append(work.key)
                if case .steer(let steer) = work {
                    guard let result = try? sendSteer(steer.messageID) else { continue }
                    switch result.state {
                    case .acknowledged:
                        progressed = true
                        report.acknowledged.append(work.key)
                        report.steers.append(result)
                        if let receipt = result.receipt { report.receipts.append(receipt) }
                    case .refused:
                        progressed = true
                        report.refused.append(work.key)
                        report.steers.append(result)
                    default:
                        report.retrying.append(work.key)
                    }
                    continue
                }
                guard let result = try? send(work.key) else { continue }
                switch result.state {
                case .acknowledged:
                    progressed = true
                    report.acknowledged.append(work.key)
                    if let receipt = result.receipt { report.receipts.append(receipt) }
                case .withdrawn:
                    progressed = true
                    if let receipt = result.receipt { report.receipts.append(receipt) }
                case .failed:
                    report.failed.append(work.key)
                default:
                    report.retrying.append(work.key)
                }
            }
            if !progressed && outbox.conversationsNeedingChain().allSatisfy(report.resynced.contains) { break }
        }
        report.conversations = created
        return report
    }

    enum WithdrawOutcome: Equatable {
        /// Removed; the daemon never had it (a tombstone is left when it was sent).
        case withdrawn(Receipt?)
        /// The daemon has it; its receipt, so the caller can stop it there.
        case inDaemon(Receipt)
        /// A send is under way; ask again later.
        case inFlight
    }

    /// D-22: withdraw a journaled send that has no receipt.
    func withdraw(_ key: String) throws -> WithdrawOutcome {
        switch try outbox.planWithdraw(key) {
        case .withdrawn:
            return .withdrawn(nil)
        case .inFlight:
            return .inFlight
        case .acknowledged(let receipt):
            if let receipt { return .inDaemon(receipt) }
            throw OutboxError.invalidState("acknowledged without a receipt")
        case .needsStatus(let messageID, let conversationID):
            let status = try client.call(Ops.messageStatus, MessageStatusArgs(message_ids: [messageID]))
            guard let receipt = status.messages.first(where: { $0.message_id == messageID }) else {
                throw DaemonClientError.malformed("message.status left out \(messageID)")
            }
            if receipt.messageState == .unknown {
                // Never received: a tombstone stops a late copy of the send from landing.
                let tomb = try client.call(Ops.messageCancel, MessageCancelArgs(message_id: messageID,
                                                                                conversation_id: conversationID))
                try outbox.markWithdrawn(key, receipt: tomb)
                return .withdrawn(tomb)
            }
            if receipt.isTombstone {
                try outbox.markWithdrawn(key, receipt: receipt)
                return .withdrawn(receipt)
            }
            try outbox.acknowledge(key, receipt: receipt)
            return .inDaemon(receipt)
        }
    }
}
