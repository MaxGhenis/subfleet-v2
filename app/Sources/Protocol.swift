// Subfleet: the conversation ops on the daemon socket (design §5, C-25).
//
// Every argument and result here mirrors `subfleet/conversations/service.py`
// (each `op_*` handler, `_view`, `_receipt`, `_approval_view`) and the store
// rows it reads. Property names are the wire's snake_case names on purpose, as
// in StatusModel.swift: a wire mirror stays greppable against the daemon.
// Open-ended objects (approval display, served facts, event data) keep every
// key through JSONValue. Foundation only.

import Foundation

let subfleetProtocolVersion = 1
let subfleetConversationSchema = 1
let requiredDaemonCapability = "conversations.v1"

// MARK: - Envelope

/// `{"v":1,"id":…,"op":…,"args":{…}}`, one line per connection (C-16).
struct RequestEnvelope<Args: Encodable>: Encodable {
    let v: Int
    let id: String
    let op: String
    let args: Args
}

/// `{"id","ok","result","error":{"code","message","fix"},"v"}`; decoded in two
/// steps so a result that does not match its model is reported as such.
struct ResponseHeader: Decodable {
    let id: String
    let ok: Bool
    let error: DaemonError?
    let v: Int?
}

struct ResponseResult<Result: Decodable>: Decodable {
    let result: Result
}

/// An `ok:false` answer. Conversation refusals are written
/// `"<reason>: <message>"` (service.py `respond`); `reason` recovers the slug.
struct DaemonError: Error, Codable, Equatable {
    var code: Int
    var message: String
    var fix: String?

    var reason: String? {
        guard let colon = message.range(of: ": ") else { return nil }
        let slug = message[..<colon.lowerBound]
        guard !slug.isEmpty, slug.allSatisfy({ $0.isLowercase || $0.isNumber || $0 == "-" }),
              slug.first?.isLetter == true else { return nil }
        return String(slug)
    }

    var detail: String {
        guard let reason, message.hasPrefix(reason + ": ") else { return message }
        return String(message.dropFirst(reason.count + 2))
    }

    /// Exit 2 `out-of-order`: the predecessor is not committed yet (C-24.2).
    var isOutOfOrder: Bool { code == 2 && reason == "out-of-order" }
    /// An older daemon without the op (protocol.decode_request).
    var isUnknownOp: Bool { message.hasPrefix("unknown op") }
    /// Exit 7: refused; person-only refusals carry reason `person-only`.
    var isPersonOnly: Bool { code == 7 && reason == "person-only" }
    /// Exit 1: an operational failure; nothing says the request changed anything.
    var isTransient: Bool { code == 1 }
}

// MARK: - Operations

/// One op: its wire name, its argument and result shapes, and whether it long-polls.
struct DaemonOperation<Args: Encodable, Result: Decodable> {
    let name: String
    var longPoll = false
    /// An op that runs git on the daemon's file pool waits at least this long:
    /// each git call there has its own cap (`caps.workspace_git_timeout_s`).
    var minimumTimeout: TimeInterval = 0

    /// 15 s, or `wait_s + 15` for a long poll (design §12); never below `minimumTimeout`.
    func timeout(for args: Args, base: TimeInterval = 15) -> TimeInterval {
        if longPoll, let poll = args as? LongPollArgs { return max(minimumTimeout, max(0, poll.wait_s ?? 0) + base) }
        return max(minimumTimeout, base)
    }
}

protocol LongPollArgs {
    var wait_s: Double? { get }
}

enum Ops {
    static let capabilities = DaemonOperation<NoArgs, Capabilities>(name: "capabilities")
    static let conversationList = DaemonOperation<ConversationListArgs, ConversationListResult>(name: "conversation.list")
    static let conversationOpen = DaemonOperation<ConversationOpenArgs, ConversationOpenResult>(name: "conversation.open")
    static let conversationCreate = DaemonOperation<ConversationCreateArgs, ConversationCreateResult>(name: "conversation.create")
    static let conversationSettings = DaemonOperation<ConversationSettingsArgs, ConversationResult>(name: "conversation.settings")
    static let conversationUnblock = DaemonOperation<ConversationUnblockArgs, ConversationResult>(name: "conversation.unblock")
    static let conversationHistory = DaemonOperation<ConversationHistoryArgs, HistoryPage>(name: "conversation.history")
    static let conversationEvents = DaemonOperation<ConversationEventsArgs, EventsPage>(name: "conversation.events", longPoll: true)
    static let conversationWatch = DaemonOperation<ConversationWatchArgs, WatchPage>(name: "conversation.watch", longPoll: true)
    static let messageSubmit = DaemonOperation<MessageSubmitArgs, Receipt>(name: "message.submit")
    static let messageStatus = DaemonOperation<MessageStatusArgs, MessageStatusResult>(name: "message.status")
    static let messageCancel = DaemonOperation<MessageCancelArgs, Receipt>(name: "message.cancel")
    static let turnInterrupt = DaemonOperation<TurnInterruptArgs, Receipt>(name: "turn.interrupt")
    static let messageResolve = DaemonOperation<MessageResolveArgs, Receipt>(name: "message.resolve")
    static let approvalList = DaemonOperation<ApprovalListArgs, ApprovalListResult>(name: "approval.list")
    static let approvalGet = DaemonOperation<ApprovalGetArgs, ApprovalDetail>(name: "approval.get")
    static let approvalRespond = DaemonOperation<ApprovalRespondArgs, ApprovalRespondResult>(name: "approval.respond")
    static let attachmentAdd = DaemonOperation<AttachmentAddArgs, AttachmentResult>(name: "attachment.add")
    static let catalogRefresh = DaemonOperation<NoArgs, CatalogRefreshResult>(name: "catalog.refresh")
    static let modelsList = DaemonOperation<ModelsListArgs, ModelsListResult>(name: "models.list")
    static let conversationRuns = DaemonOperation<ConversationRunsArgs, ConversationRunsResult>(name: "conversation.runs")
    static let turnDiff = DaemonOperation<TurnDiffArgs, DiffResult>(name: "turn.diff", minimumTimeout: 120)
    static let conversationDiff = DaemonOperation<ConversationDiffArgs, DiffResult>(name: "conversation.diff",
                                                                                    minimumTimeout: 120)
    /// Reads the source's native history and writes the brief on the daemon's
    /// file pool, so it gets the diff ops' longer floor.
    static let conversationHandoff = DaemonOperation<ConversationHandoffArgs, ConversationHandoffResult>(
        name: "conversation.handoff", minimumTimeout: 120)

    /// Every op in `subfleet/protocol.py` `CONVERSATION_OPS`, in its order.
    static let names = [
        capabilities.name, conversationList.name, conversationOpen.name, conversationCreate.name,
        conversationSettings.name, conversationUnblock.name, conversationHistory.name, conversationEvents.name,
        conversationWatch.name, messageSubmit.name, messageStatus.name, messageCancel.name, turnInterrupt.name,
        messageResolve.name, approvalList.name, approvalGet.name, approvalRespond.name, attachmentAdd.name,
        catalogRefresh.name, modelsList.name, conversationRuns.name, turnDiff.name, conversationDiff.name,
        conversationHandoff.name,
    ]

    /// Person-only ops (D-8, C-25.6); settings that widen are person-only too.
    static let personOnly: Set<String> = [
        approvalGet.name, approvalRespond.name, messageResolve.name, conversationUnblock.name,
    ]
}

// MARK: - Shared shapes

/// `{model, effort, fast, permission, auto_continue}` (store.validate_settings).
struct ConversationSettings: Codable, Equatable, Hashable {
    var model: String
    var effort: String?
    var fast: Bool
    var permission: String
    var auto_continue: Bool

    init(model: String, effort: String? = nil, fast: Bool = false, permission: String = PermissionPolicy.ask.rawValue,
         auto_continue: Bool = true) {
        self.model = model
        self.effort = effort
        self.fast = fast
        self.permission = permission
        self.auto_continue = auto_continue
    }

    private enum CodingKeys: String, CodingKey { case model, effort, fast, permission, auto_continue }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        model = try c.decode(String.self, forKey: .model)
        effort = try c.decodeIfPresent(String.self, forKey: .effort)
        fast = try c.decodeIfPresent(Bool.self, forKey: .fast) ?? false
        permission = try c.decode(String.self, forKey: .permission)
        auto_continue = try c.decodeIfPresent(Bool.self, forKey: .auto_continue) ?? true
    }

    /// `effort` is sent as an explicit null: `conversation.settings` merges the
    /// object it is given over the stored one, so a missing key keeps the old effort.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(model, forKey: .model)
        if let effort { try c.encode(effort, forKey: .effort) } else { try c.encodeNil(forKey: .effort) }
        try c.encode(fast, forKey: .fast)
        try c.encode(permission, forKey: .permission)
        try c.encode(auto_continue, forKey: .auto_continue)
    }

    var policy: PermissionPolicy? { PermissionPolicy(rawValue: permission) }
}

/// Permission policies (turn.PERMISSIONS, store.PERMISSION_ORDER).
enum PermissionPolicy: String, CaseIterable, Codable {
    case readOnly = "read-only"
    case ask
    case acceptEdits = "accept-edits"
    case bypass

    var rank: Int {
        switch self {
        case .readOnly: return 0
        case .ask: return 1
        case .acceptEdits: return 2
        case .bypass: return 3
        }
    }

    var label: String {
        switch self {
        case .readOnly: return "Read-only"
        case .ask: return "Ask"
        case .acceptEdits: return "Accept edits"
        case .bypass: return "Bypass"
        }
    }

    /// store.widens: a person must confirm (confirm_widen) moving up this order.
    static func widens(from before: String, to after: String) -> Bool {
        guard let after = PermissionPolicy(rawValue: after) else { return true }
        guard let before = PermissionPolicy(rawValue: before) else { return true }
        return after.rank > before.rank
    }
}

/// Message states (turn.py, design D-12).
enum MessageState: String, CaseIterable {
    case queued, waiting, starting, running
    case approvalNeeded = "approval-needed"
    case complete, failed, interrupted, cancelled
    case deliveryUnknown = "delivery-unknown"
    /// `message.status` for an id the daemon does not have.
    case unknown

    static let live: Set<MessageState> = [.waiting, .starting, .running, .approvalNeeded, .deliveryUnknown]
    static let terminal: Set<MessageState> = [.complete, .failed, .interrupted, .cancelled]

    var isTerminal: Bool { MessageState.terminal.contains(self) }
}

/// A conversation as `_view` returns it.
struct Conversation: Codable, Equatable, Identifiable {
    var conversation_id: String
    var provider: String
    var native_session_id: String?
    var title: String?
    var workspace: String
    var workspace_kind: String
    /// A worktree conversation's worktree (path, branch, source, repository, base).
    var worktree: JSONValue?
    var allow_main: Bool
    var lane_id: String?
    var settings: ConversationSettings
    var origin: String
    var handoff_from: JSONValue?
    var blocked_by: String?
    var created_at: String
    var updated_at: String
    var last_message: LastMessage?
    var pending_approvals: Int
    var active: Bool
    /// A Claude session another live process (the Claude app, a terminal) holds;
    /// its turns and Subfleet's share one transcript (from the catalog run).
    var live_elsewhere: Bool?

    var id: String { conversation_id }
}

struct LastMessage: Codable, Equatable {
    var message_id: String
    var state: String
    var state_reason: String?
    var updated_at: String?
}

/// A message receipt (`_receipt`). `message.status` answers an unknown id with
/// only `{message_id, state:"unknown"}`, so all but those two are optional.
struct Receipt: Codable, Equatable {
    var message_id: String
    var conversation_id: String?
    var seq: Int?
    var origin: String?
    var continues: String?
    var state: String
    var state_reason: String?
    var settings: ConversationSettings?
    var served: Served?
    var turn_ref: String?
    var updated_at: String?
    var stop_requested: Bool?
    var created: Bool?
    /// The person's text (`conversation.open`, `message.status`), at most 20,000 characters.
    var text: String?
    var text_truncated: Bool?

    var messageState: MessageState? { MessageState(rawValue: state) }
    /// A tombstone left by withdrawing a message the daemon never received.
    var isTombstone: Bool { origin == "tombstone" || state_reason == "withdrawn-before-receipt" }
}

/// What served a turn: the runner's merge of `served` events, then the lane and
/// served model at settlement (service.py `_on_outcome`). Open-ended.
struct Served: JSONObjectBacked, Hashable {
    var fields: [String: JSONValue]
    init(fields: [String: JSONValue] = [:]) { self.fields = fields }

    var lane_id: String? { string("lane_id") }
    var account: String? { string("account") }
    var model: String? { string("model") }
    var effort: String? { string("effort") }
    var fast_mode_state: String? { string("fast_mode_state") }
    var fast_mode_disabled_reason: String? { string("fast_mode_disabled_reason") }
    var fast_warning: String? { string("fast_warning") }
    var service_tier: String? { string("service_tier") }
    var permission_mode: String? { string("permission_mode") }

    /// Later non-null values win, as the runner merges them.
    func merging(_ other: Served?) -> Served {
        guard let other else { return self }
        var out = fields
        for (key, value) in other.fields where !value.isNull { out[key] = value }
        return Served(fields: out)
    }
}

// MARK: - capabilities, models.list

struct NoArgs: Codable, Equatable {}

struct Capabilities: Codable, Equatable {
    var `protocol`: Int
    var daemon_version: String
    var conversation_schema: Int
    var capabilities: [String]
    var limits: CapabilityLimits?
    var codex_writable: Bool?

    func has(_ capability: String) -> Bool { capabilities.contains(capability) }
}

struct CapabilityLimits: Codable, Equatable {
    var message_bytes: Int?
    var attachment_bytes: Int?
    var attachments_per_message: Int?
    var events_page_bytes: Int?
    var events_wait_s: Double?
    var relay_frame_bytes: Int?
    /// `diff.v1`: a diff's text and file-list bounds (C-26.14).
    var diff_bytes: Int?
    var diff_files: Int?
}

struct ModelsListArgs: Codable, Equatable {
    var provider: String?
}

struct ModelsListResult: Codable, Equatable {
    var models: [ModelEntry]
    var source: String?
}

/// `op_models_list`: a policy model and what a provider's catalog last said of it.
struct ModelEntry: Codable, Equatable, Identifiable {
    var short: String
    var id: String
    var provider: String
    var value: String
    var values: [String]
    var efforts: [String]?
    var default_effort: String?
    var fast: ModelFast
    var image_input: Bool?
    var observed_at: String?
}

struct ModelFast: Codable, Equatable {
    var supported: Bool?
    var billing: String
}

// MARK: - conversation.list

struct ConversationListArgs: Codable, Equatable {
    var provider: String?
    var query: String?
    var limit: Int?
    var include_catalog: Bool?
    /// The catalog's `next` (an mtime) from the previous page.
    var before: JSONValue?
    var include_archived: Bool?
}

struct ConversationListResult: Codable, Equatable {
    var conversations: [Conversation]
    var catalog: Catalog?
}

struct Catalog: Codable, Equatable {
    var generated_at: String?
    var complete: Bool
    var items: [CatalogItem]
    var next: JSONValue?
    /// D-23: `fresh`, `stale`, `missing` or `damaged`, with the index's age.
    var state: String?
    var age_s: Double?
    var stale_after_s: Double?
    /// A catalog run is in progress.
    var refreshing: Bool?
}

/// A native Claude or Codex session the catalog found (catalog.build).
struct CatalogItem: Codable, Equatable, Identifiable {
    var provider: String
    var native_session_id: String
    var path: String?
    var home: String?
    var lane_id: String?
    var title: String?
    var first_prompt: String?
    var cwd: String?
    var model: String?
    var permission_mode: String?
    var mtime: Double?
    var continuable: Bool?
    var continue_blocker: String?
    var archived: Bool?
    var live_elsewhere: Bool?

    var id: String { "native:\(provider):\(native_session_id)" }
}

// MARK: - conversation.open, create, settings, unblock

struct NativeSessionRef: Codable, Equatable, Hashable {
    var provider: String
    var session_id: String
    var home: String?
}

struct ConversationOpenArgs: Codable, Equatable {
    var conversation_id: String?
    var native: NativeSessionRef?

    static func conversation(_ id: String) -> ConversationOpenArgs { .init(conversation_id: id, native: nil) }
    static func native(provider: String, sessionID: String, home: String? = nil) -> ConversationOpenArgs {
        .init(conversation_id: nil, native: NativeSessionRef(provider: provider, session_id: sessionID, home: home))
    }
}

struct ConversationOpenResult: Codable, Equatable {
    var conversation: Conversation
    /// The latest 50 receipts, oldest first.
    var messages: [Receipt]
    var events_cursor: Int
    var pending_approvals: [ApprovalView]
}

struct ConversationCreateArgs: Codable, Equatable {
    var request_id: String
    var provider: String
    var workspace: String
    var workspace_kind: String = "in-place"
    var allow_main: Bool?
    var title: String?
    var settings: ConversationSettings
    var confirm_widen: Bool?
}

struct ConversationCreateResult: Codable, Equatable {
    var conversation: Conversation
    var created: Bool
}

struct ConversationSettingsArgs: Codable, Equatable {
    var conversation_id: String
    var settings: ConversationSettings
    var confirm_widen: Bool?
    var allow_main: Bool?
}

struct ConversationResult: Codable, Equatable {
    var conversation: Conversation
}

/// A person's choice for an unfinished turn; `confirm` is always the literal true (D-13).
struct ConversationUnblockArgs: Codable, Equatable {
    enum Choice: String, Codable { case `continue`, leave }
    var conversation_id: String
    var choice: Choice

    private enum CodingKeys: String, CodingKey { case conversation_id, choice, confirm }

    init(conversation_id: String, choice: Choice) {
        self.conversation_id = conversation_id
        self.choice = choice
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        conversation_id = try c.decode(String.self, forKey: .conversation_id)
        choice = try c.decode(Choice.self, forKey: .choice)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(conversation_id, forKey: .conversation_id)
        try c.encode(choice, forKey: .choice)
        try c.encode(true, forKey: .confirm)
    }
}

// MARK: - turn.diff, conversation.diff (C-26.14, design D-25)

/// The daemon advertises the two diff ops with this capability (C-25.2).
let diffCapability = "diff.v1"

struct TurnDiffArgs: Codable, Equatable {
    var message_id: String
    var path: String?
}

/// `conversation.handoff` (C-30.3, design D-18): a new conversation with the
/// other provider whose first message is a brief of this one; the source's
/// pending messages move behind it. Idempotent by `request_id`.
struct ConversationHandoffArgs: Codable, Equatable {
    struct Source: Codable, Equatable {
        var conversation_id: String
    }
    struct Target: Codable, Equatable {
        var provider: String
        var settings: ConversationSettings
        var workspace: String?
        var title: String?
        var allow_main: Bool?
    }
    var request_id: String
    var from: Source
    var to: Target
    var confirm_widen: Bool?
}

struct ConversationHandoffResult: Codable, Equatable {
    var conversation: Conversation
    var created: Bool
    var brief: Receipt
    var moved: [Receipt]
    var withdrawn: [String]
    var handoff_from: JSONValue?
}

struct ConversationDiffArgs: Codable, Equatable {
    var conversation_id: String
    var path: String?
}

/// What a turn, or a whole conversation, changed in its checkout: the files and
/// a unified diff between two working-tree snapshots, bounded and scrubbed.
/// With `available: false`, `reason` and `detail` say why, and the lists are empty.
struct DiffResult: Codable, Equatable {
    var message_id: String?
    var conversation_id: String
    var available: Bool
    var reason: String?
    var detail: String?
    var root: String?
    var path: String?
    var from: DiffEnd?
    var to: DiffEnd?
    var files: [DiffFile]
    var files_truncated: Bool
    var stats: DiffStats
    var diff: String
    var truncated: Bool
    var scrubbed: Int

    /// Compared with the working tree now, not a turn's end snapshot.
    var isLive: Bool { to?.live == true }
}

/// One side of a comparison: a snapshot's tree and HEAD, and when it was taken.
struct DiffEnd: Codable, Equatable {
    var tree: String?
    var head: String?
    var message_id: String?
    var live: Bool?
    var at: String?
}

/// `status` is `added`, `deleted`, `modified`, `renamed` (with `from`),
/// `type-changed` or `copied`; the counts are null for a binary file.
struct DiffFile: Codable, Equatable, Identifiable {
    var path: String
    var status: String
    var additions: Int?
    var deletions: Int?
    var binary: Bool
    var from: String?

    var id: String { path }
}

/// Every changed file is counted, listed or not; `complete: false` when git's
/// listing passed its bound and only the listed files are counted.
struct DiffStats: Codable, Equatable {
    var files: Int
    var additions: Int
    var deletions: Int
    var complete: Bool
}

// MARK: - conversation.history

struct ConversationRunsArgs: Codable, Equatable {
    var conversation_id: String
    var limit: Int?
}

struct ConversationRunsResult: Codable, Equatable {
    var runs: [RunSummary]
}

/// One detached job a conversation's turns dispatched: a sub-agent, where it
/// runs (its latest attempt's lane) and on which model.
struct RunSummary: Codable, Equatable, Identifiable {
    var job_id: String
    var name: String?
    var kind: String
    var state: String
    var task: String?
    var tier: String?
    var sandbox: String?
    var wait_reason: String?
    var created_at: String?
    var started_at: String?
    var finished_at: String?
    var out_path: String?
    var workdir: String?
    var lane_id: String?
    var model_served: String?
    var model_requested: String?
    var attempt_state: String?
    var attempts: Int?

    var id: String { job_id }
    var isLive: Bool { !["succeeded", "failed", "cancelled", "lost"].contains(state) }
}

struct ConversationHistoryArgs: Codable, Equatable {
    var conversation_id: String
    var before: Int?
    var limit: Int?
}

/// A page of the native transcript, newest first (history.page).
struct HistoryPage: Codable, Equatable {
    var items: [HistoryItem]
    var next_before: Int?
    var missing: Bool?
}

struct HistoryItem: Codable, Equatable {
    var role: String
    var kind: String
    var text: String
    var ts: String?
    var id: String?
    var cursor: Int?
    var tool: String?
    /// A tool call's id, whether it was hidden, its result preview and whether it
    /// failed: the fields a live `tool.started`/`tool.completed` pair carries.
    var tool_id: String?
    var hidden: Bool?
    var preview: String?
    var is_error: Bool?
}

// MARK: - conversation.events, conversation.watch

struct ConversationEventsArgs: Codable, Equatable, LongPollArgs {
    var conversation_id: String
    var after: Int
    var limit: Int?
    var wait_s: Double?
}

struct EventsPage: Codable, Equatable {
    var events: [ConversationEvent]
    var next: Int
    var reset: Bool
    var superseded: Bool?
    /// The compaction floor (C-25.4): `reset` is true exactly while the cursor is below it.
    var floor: Int?
}

struct ConversationEvent: Codable, Equatable {
    var seq: Int
    var message_id: String?
    var kind: String
    var ts: String?
    var data: JSONValue
}

struct ConversationWatchArgs: Codable, Equatable, LongPollArgs {
    var after: Int
    var wait_s: Double?
}

struct WatchPage: Codable, Equatable {
    var changes: [ConversationChange]
    var next: Int
    var superseded: Bool?
}

/// One row of the `changes` feed (store._change).
struct ConversationChange: Codable, Equatable {
    var seq: Int
    var conversation_id: String
    var message_id: String?
    var state: String?
    var pending_approvals: Int
    var ts: String?
    /// Why a message is in its state (a waiting message's hold, a failure).
    var state_reason: String?
}

// MARK: - messages

struct MessageSubmitArgs: Codable, Equatable {
    var conversation_id: String
    var message_id: String
    var after_message_id: String?
    var text: String
    var attachments: [String]
    var settings: ConversationSettings

    private enum CodingKeys: String, CodingKey {
        case conversation_id, message_id, after_message_id, text, attachments, settings
    }

    init(conversation_id: String, message_id: String, after_message_id: String?, text: String,
         attachments: [String] = [], settings: ConversationSettings) {
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.after_message_id = after_message_id
        self.text = text
        self.attachments = attachments
        self.settings = settings
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        conversation_id = try c.decode(String.self, forKey: .conversation_id)
        message_id = try c.decode(String.self, forKey: .message_id)
        after_message_id = try c.decodeIfPresent(String.self, forKey: .after_message_id)
        text = try c.decode(String.self, forKey: .text)
        attachments = try c.decodeIfPresent([String].self, forKey: .attachments) ?? []
        settings = try c.decode(ConversationSettings.self, forKey: .settings)
    }

    /// The first message of a conversation says so with an explicit null.
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(conversation_id, forKey: .conversation_id)
        try c.encode(message_id, forKey: .message_id)
        if let after_message_id { try c.encode(after_message_id, forKey: .after_message_id) }
        else { try c.encodeNil(forKey: .after_message_id) }
        try c.encode(text, forKey: .text)
        try c.encode(attachments, forKey: .attachments)
        try c.encode(settings, forKey: .settings)
    }
}

struct MessageStatusArgs: Codable, Equatable {
    var message_ids: [String]
}

struct MessageStatusResult: Codable, Equatable {
    var messages: [Receipt]
}

/// `conversation_id` lets the daemon leave a tombstone for an id it never
/// received, so a late copy of that submit cannot land (service `_tombstone`).
struct MessageCancelArgs: Codable, Equatable {
    var message_id: String
    var conversation_id: String?
}

struct TurnInterruptArgs: Codable, Equatable {
    var message_id: String
}

/// A person's ruling on an ambiguous delivery; `confirm` is always the literal true (C-24.6).
struct MessageResolveArgs: Codable, Equatable {
    enum Resolution: String, Codable { case delivered, notDelivered = "not-delivered" }
    var message_id: String
    var resolution: Resolution

    private enum CodingKeys: String, CodingKey { case message_id, resolution, confirm }

    init(message_id: String, resolution: Resolution) {
        self.message_id = message_id
        self.resolution = resolution
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        message_id = try c.decode(String.self, forKey: .message_id)
        resolution = try c.decode(Resolution.self, forKey: .resolution)
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(message_id, forKey: .message_id)
        try c.encode(resolution, forKey: .resolution)
        try c.encode(true, forKey: .confirm)
    }
}

// MARK: - approvals

/// `_approval_view` (no nonce).
struct ApprovalView: Codable, Equatable, Identifiable {
    var approval_id: String
    var message_id: String
    var conversation_id: String
    var kind: String
    var display: ApprovalDisplay
    var options: [String]
    var created_at: String
    var state: String

    var id: String { approval_id }
}

/// The display fields a driver recorded for a provider request (claude_turn /
/// codex_turn `summary`). Every key is kept: the card shows all of them.
struct ApprovalDisplay: JSONObjectBacked, Hashable {
    var fields: [String: JSONValue]
    init(fields: [String: JSONValue] = [:]) { self.fields = fields }

    var tool: String? { string("tool") }
    var title: String? { string("title") }
    var description: String? { string("description") }
    var input: String? { string("input") }
    var reason: String? { string("reason") }
    var command: String? { string("command") }
    var cwd: String? { string("cwd") }

    /// AskUserQuestion's questions (kind `question`).
    var questions: [ApprovalQuestion] {
        guard let raw = fields["questions"], !raw.isNull else { return [] }
        return (try? raw.decode([ApprovalQuestion].self)) ?? []
    }

    /// Known fields first, in the order a person reads them, then any other key,
    /// so a field the app does not know is still shown (C-27.1).
    static let knownOrder = ["tool", "title", "description", "command", "input", "cwd", "reason", "blocked_path",
                             "input_kind", "grant_root", "permissions", "network", "execpolicy_amendment",
                             "network_amendments"]

    var shownFields: [(key: String, value: String)] {
        let present = fields.filter { !$0.value.isNull && $0.key != "questions" }
        let known = ApprovalDisplay.knownOrder.filter { present[$0] != nil }
        let other = present.keys.filter { !ApprovalDisplay.knownOrder.contains($0) }.sorted()
        return (known + other).map { ($0, present[$0]!.displayText) }
    }
}

struct ApprovalQuestion: Codable, Equatable, Hashable {
    struct Option: Codable, Equatable, Hashable {
        var label: String
        var description: String?
    }
    var question: String
    var header: String?
    var multiSelect: Bool?
    var options: [Option]?
}

struct ApprovalListArgs: Codable, Equatable {
    var conversation_id: String?
}

struct ApprovalListResult: Codable, Equatable {
    var approvals: [ApprovalView]
}

struct ApprovalGetArgs: Codable, Equatable {
    var approval_id: String
    var reveal: Bool?
}

/// `approval.get` (person-only): the exact request, token-shaped values masked
/// in place unless `reveal`, its SHA-256 and the nonce `approval.respond` needs.
struct ApprovalDetail: Codable, Equatable {
    var approval: ApprovalView
    var request: JSONValue
    var masked: [MaskedSpan]
    var request_sha256: String
    var nonce: String
}

struct MaskedSpan: Codable, Equatable, Hashable {
    var path: String
    var rule: String
    var length: Int
    var sha256: String
}

struct ApprovalRespondArgs: Codable, Equatable {
    var approval_id: String
    var nonce: String
    var request_sha256: String
    var decision: String
    /// AskUserQuestion: question text to the chosen label or the person's own text.
    var answers: [String: String]?
    var message: String?
}

struct ApprovalRespondResult: Codable, Equatable {
    var approval: ApprovalView
    /// A repeat of the recorded answer, recognised and not sent again.
    var duplicate: Bool?
}

// MARK: - attachments, catalog

struct AttachmentAddArgs: Codable, Equatable {
    var path: String
    var sha256: String?
}

struct AttachmentResult: Codable, Equatable {
    var sha256: String
    var media_type: String
    var bytes: Int
}

struct CatalogRefreshResult: Codable, Equatable {
    var requested: Bool
    var running: Bool
    /// When the catalog now on disk was written (design §5).
    var generated_at: String?
}
