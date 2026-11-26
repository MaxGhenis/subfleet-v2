// Subfleet: suggested conversations, the app's model (C-25.7, C-29.14).
//
// An agent proposes a separate conversation (`subfleet suggest`); the app shows
// it as a card in the conversation that proposed it and in the sidebar's
// Suggested section, and the person starts it with one click or dismisses it.
// The wire shapes mirror `subfleet/conversations/suggestions.py` (`view`) and
// the service's `op_suggestion_*` handlers. Foundation only.

import Foundation

/// The daemon serves the suggestion ops (C-25.1): an added op is a capability.
let suggestionsCapability = "suggestions.v1"

/// A suggestion as `suggestions.view` returns it: the agent's words (scrubbed when
/// it was made), where and how it would start, and what became of it.
struct Suggestion: Codable, Equatable, Identifiable {
    var suggestion_id: String
    /// `open`, `starting` (the person's Start is under way), `started`,
    /// `dismissed` or `withdrawn`.
    var state: String
    var title: String
    var tldr: String
    /// The whole brief, or its first 2,000 characters in a listing (`brief_truncated`).
    var brief: String
    var brief_truncated: Bool
    var brief_chars: Int
    var origin_conversation_id: String?
    var origin_message_id: String?
    var creator_session: String?
    var workspace: String?
    /// The worktree the agent asked for; `workspace_kind` says what Start does.
    var worktree: Bool
    /// Where Start runs it, decided by the daemon when it was made: `worktree` (a new
    /// worktree of `repository`, on its own branch) or `in-place` (its folder itself,
    /// or a new scratch folder when `workspace` is nil). Nil only from a daemon that
    /// predates the field.
    var workspace_kind: String? = nil
    var repository: String? = nil
    var provider: String
    var model: String?
    var decided_by: String?
    var decided_at: String?
    /// Why a Start a person claimed has not finished yet; Start again finishes it.
    var start_error: String?
    var started_conversation_id: String?
    var started_message_id: String?
    var created_at: String
    var updated_at: String

    var id: String { suggestion_id }
    var isOpen: Bool { state == "open" }
    var isStarting: Bool { state == "starting" }
    /// Open or starting: what the Suggested section lists and its badge counts.
    var awaitsPerson: Bool { state == "open" || state == "starting" }
    var createdDate: Date? { parseTimestamp(created_at) }

    /// A newer view of the same suggestion: a later state wins, and a full brief
    /// is kept over a listing's preview of the same words.
    func merged(with newer: Suggestion) -> Suggestion {
        var out = newer
        if newer.brief_truncated && !brief_truncated && brief.hasPrefix(newer.brief) {
            out.brief = brief
            out.brief_truncated = false
        }
        return out
    }
}

struct SuggestionLimits: Codable, Equatable {
    var suggestion_title_chars: Int?
    var suggestion_tldr_chars: Int?
    var suggestion_brief_bytes: Int?
    var suggestions_per_origin: Int?
    var suggestions_open: Int?
}

struct SuggestionCreateArgs: Codable, Equatable {
    var request_id: String
    var title: String
    var tldr: String
    var brief: String
    var workspace: String?
    var worktree: Bool?
    var provider: String?
    var model: String?
    var session_id: String?
    var calling_job: String?
}

struct SuggestionCreateResult: Codable, Equatable {
    var suggestion: Suggestion
    var created: Bool
    /// Secret-shaped values the daemon masked in what the agent wrote (C-25.5).
    var scrubbed: Int
}

struct SuggestionListArgs: Codable, Equatable {
    var conversation_id: String?
    var suggestion_ids: [String]?
    var limit: Int?
    var closed_limit: Int?
}

struct SuggestionListResult: Codable, Equatable {
    var suggestions: [Suggestion]
    /// Open suggestions in all (or in the conversation the request named).
    var open: Int
    var limits: SuggestionLimits
    /// The watch feed's sequence the rows were read at: every feed row up to it is in
    /// them, so a suggestion the feed delivered later is newer than this answer.
    var watch_seq: Int? = nil
}

/// `suggestion.start` (person-only, C-25.6). Above Ask needs `confirm_widen: true`.
struct SuggestionStartArgs: Codable, Equatable {
    var suggestion_id: String
    var permission: String?
    var confirm_widen: Bool?
}

struct SuggestionStartResult: Codable, Equatable {
    var suggestion: Suggestion
    var conversation: Conversation
    /// The first message's receipt, its text the brief.
    var message: Receipt?
    var created: Bool
}

/// `suggestion.dismiss` (person-only) and `suggestion.withdraw` (its creator's).
struct SuggestionCloseArgs: Codable, Equatable {
    var suggestion_id: String
    var session_id: String?
    var calling_job: String?
}

struct SuggestionCloseResult: Codable, Equatable {
    var suggestion: Suggestion
    var changed: Bool
}

extension Ops {
    static let suggestionCreate = DaemonOperation<SuggestionCreateArgs, SuggestionCreateResult>(
        name: "suggestion.create", minimumTimeout: 120)
    static let suggestionList = DaemonOperation<SuggestionListArgs, SuggestionListResult>(name: "suggestion.list")
    /// Checks the folder and may cut a worktree on the daemon's file pool, as
    /// `conversation.create` does, so it gets the diff ops' longer floor.
    static let suggestionStart = DaemonOperation<SuggestionStartArgs, SuggestionStartResult>(
        name: "suggestion.start", minimumTimeout: 120)
    static let suggestionDismiss = DaemonOperation<SuggestionCloseArgs, SuggestionCloseResult>(name: "suggestion.dismiss")
    static let suggestionWithdraw = DaemonOperation<SuggestionCloseArgs, SuggestionCloseResult>(name: "suggestion.withdraw")
}

/// The permissions Start offers for a suggestion's provider: Ask, and the wider
/// ones behind a confirmation; a Codex suggestion starts read-only until Codex
/// conversations may write (C-26.11).
func suggestionStartPermissions(provider: String, capabilities: Capabilities?) -> [PermissionPolicy] {
    if provider == "codex" && capabilities?.codex_writable != true { return [.readOnly] }
    return [.ask, .acceptEdits, .bypass]
}

/// The card's status line once the person has acted, or the agent withdrew it.
func suggestionStateWords(_ suggestion: Suggestion) -> String? {
    switch suggestion.state {
    case "open": return nil
    case "starting":
        if let error = suggestion.start_error { return "Not started yet: \(error). Start again to finish it." }
        return "Starting…"
    case "started": return "Started"
    case "dismissed": return "Dismissed"
    case "withdrawn": return "Withdrawn by the agent that suggested it"
    default: return suggestion.state
    }
}

/// Whether Start makes a worktree, as the daemon decided (C-25.7).
func suggestionStartsInWorktree(_ suggestion: Suggestion) -> Bool { suggestion.workspace_kind == "worktree" }

/// Where Start will run a suggestion, in words, exactly as the daemon decided it
/// (`workspace_kind`): a new worktree of its folder's checkout, the folder itself,
/// or a new scratch folder; then who answers. A worktree the agent asked for in a
/// folder that is no checkout says it runs in place.
func suggestionWhereWords(_ suggestion: Suggestion, home: String = NSHomeDirectory()) -> String {
    var parts: [String] = []
    if let folder = suggestion.workspace.map({ abbreviatedPath($0, home: home) }) {
        parts.append(folder)
        switch suggestion.workspace_kind {
        case "worktree":
            let repository = suggestion.repository.map { abbreviatedPath($0, home: home) }
            parts.append(repository == nil || repository == folder ? "a new worktree on its own branch"
                         : "a new worktree of \(repository!) on its own branch")
        case "in-place":
            parts.append(suggestion.worktree ? "in place: not a git checkout, so no worktree" : "in place")
        default:
            parts.append(suggestion.worktree ? "a new worktree if it is a git checkout, else in place" : "in place")
        }
    } else {
        parts.append("A new scratch folder")
    }
    let provider = suggestion.provider == "codex" ? "Codex" : "Claude"
    parts.append(suggestion.model.map { "\(provider) · \($0)" } ?? provider)
    return parts.joined(separator: " · ")
}

extension ConversationStoreState {
    /// Fold a whole `suggestion.list` answer: every listed suggestion as it is now.
    /// A suggestion that was open and is no longer listed has closed and aged out
    /// of the listing, and leaves the Suggested section; but only if the answer is
    /// as new as what the app knows of it. The answer was read at feed sequence
    /// `watch_seq`: a suggestion the feed delivered after that (made, or changed,
    /// after the read) is newer than the answer, which neither drops nor rewinds it.
    mutating func apply(suggestions list: SuggestionListResult) {
        let read = list.watch_seq ?? 0
        let listed = Set(list.suggestions.map(\.suggestion_id))
        for (id, known) in suggestions where known.awaitsPerson && !listed.contains(id)
            && (suggestionSeqs[id] ?? 0) <= read {
            suggestions[id] = nil
            suggestionSeqs[id] = nil
        }
        apply(suggestionRows: list.suggestions, readAt: read)
    }

    /// Fold suggestions an answer carried (a listing, or the ones it named), read
    /// at feed sequence `readAt`: each one the app knows from a later feed row is
    /// kept as the feed left it.
    mutating func apply(suggestionRows rows: [Suggestion], readAt read: Int) {
        for suggestion in rows where (suggestionSeqs[suggestion.suggestion_id] ?? 0) <= read {
            upsert(suggestion, seq: read)
        }
    }

    /// One suggestion as of feed sequence `seq` (a feed row's, or the read's of an
    /// answer); nil for an op's own answer, which is as new as its op.
    mutating func upsert(_ suggestion: Suggestion, seq: Int? = nil) {
        let id = suggestion.suggestion_id
        if let seq { suggestionSeqs[id] = max(suggestionSeqs[id] ?? 0, seq) }
        if let known = suggestions[id] {
            // Feed rows arrive in order, but an answer may race one: never go back.
            if suggestionRank(suggestion.state) < suggestionRank(known.state) { return }
            suggestions[id] = known.merged(with: suggestion)
        } else {
            suggestions[id] = suggestion
        }
    }

    /// The sidebar's Suggested section: open (and starting) suggestions, newest first,
    /// with or without an origin, filtered by the sidebar's provider filter and search.
    var openSuggestions: [Suggestion] {
        let needle = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return suggestions.values
            .filter { $0.awaitsPerson && (providerFilter == nil || $0.provider == providerFilter) }
            .filter { needle.isEmpty || [$0.title, $0.tldr, $0.workspace ?? ""].contains { $0.lowercased().contains(needle) } }
            .sorted { ($0.created_at, $0.suggestion_id) > ($1.created_at, $1.suggestion_id) }
    }

    /// The Suggested section's badge: every suggestion awaiting the person, whatever the filters.
    var openSuggestionCount: Int { suggestions.values.filter(\.awaitsPerson).count }

    /// The title of a suggestion's origin conversation, for "From …".
    func suggestionOrigin(_ suggestion: Suggestion) -> String? {
        suggestion.origin_conversation_id.flatMap(conversation).map(conversationTitle)
    }
}

/// open < starting < started, and the two closes are as late as started: a state
/// never moves back to one before it.
func suggestionRank(_ state: String) -> Int {
    switch state {
    case "open": return 0
    case "starting": return 1
    default: return 2
    }
}

extension ConversationEngine {
    func suggestions(conversationID: String? = nil, ids: [String]? = nil) throws -> SuggestionListResult {
        try client.call(Ops.suggestionList, SuggestionListArgs(conversation_id: conversationID, suggestion_ids: ids))
    }

    /// A person's Start (C-25.6): the daemon makes the conversation and submits the
    /// brief as its first message. A permission above Ask is sent only confirmed.
    func startSuggestion(_ id: String, permission: PermissionPolicy = .ask, confirmedWiden: Bool = false) throws
        -> SuggestionStartResult {
        let widening = PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: permission.rawValue)
        if widening && !confirmedWiden {
            throw ConversationEngineError.widenNeedsConfirmation(from: PermissionPolicy.ask.rawValue, to: permission.rawValue)
        }
        return try client.call(Ops.suggestionStart, SuggestionStartArgs(
            suggestion_id: id, permission: permission == .ask ? nil : permission.rawValue,
            confirm_widen: widening ? true : nil))
    }

    func dismissSuggestion(_ id: String) throws -> SuggestionCloseResult {
        try client.call(Ops.suggestionDismiss, SuggestionCloseArgs(suggestion_id: id))
    }
}
