// Suggested conversations in the app's core (C-25.7, C-29.14): the store's fold of
// `suggestion.list` and the watch feed, the Suggested section, and the timeline's
// card, driven by JSON the daemon's own code produced. Foundation only.
import Foundation

/// suggestions <input.json>: steps of `list`, `suggestions`, `watch`, `events`
/// (with `conversation_id`), `upsert`, `search` and `provider_filter`; then what the
/// sidebar's Suggested section and each timeline's cards show.
func runSuggestions(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var state = ConversationStoreState()
    var log: [Any] = []
    for step in input["steps"]?.array ?? [] {
        if let list = step["list"] { state.apply(list: try list.decode(ConversationListResult.self)); log.append("list") }
        if let list = step["suggestions"] {
            state.apply(suggestions: try list.decode(SuggestionListResult.self))
            log.append("suggestions")
        }
        if let one = step["upsert"] { state.upsert(try one.decode(Suggestion.self)); log.append("upsert") }
        if let page = step["watch"] {
            switch state.apply(watch: try page.decode(WatchPage.self)) {
            case .applied(let count): log.append("watch:\(count)")
            case .superseded: log.append("watch:superseded")
            }
        }
        if let page = step["events"], let cid = step["conversation_id"]?.string {
            log.append(pageResult(state.apply(events: try page.decode(EventsPage.self), conversationID: cid)))
        }
        if let query = step["search"]?.string { state.searchQuery = query }
        if step["provider_filter"] != nil { state.providerFilter = step["provider_filter"]?.string }
    }
    var capabilities: Capabilities?
    if let value = input["capabilities"] { capabilities = try value.decode(Capabilities.self) }
    func card(_ suggestion: Suggestion) -> [String: Any] {
        ["id": suggestion.suggestion_id, "state": suggestion.state, "title": suggestion.title,
         "brief_truncated": suggestion.brief_truncated, "brief_chars": suggestion.brief_chars,
         "brief": suggestion.brief,
         "state_words": suggestionStateWords(suggestion) as Any? ?? NSNull(),
         "where": suggestionWhereWords(suggestion, home: "/Users/test"),
         "origin": state.suggestionOrigin(suggestion) as Any? ?? NSNull(),
         "start_permissions": suggestionStartPermissions(provider: suggestion.provider, capabilities: capabilities)
            .map(\.rawValue)]
    }
    var timelines: [String: Any] = [:]
    for (id, timeline) in state.timelines {
        timelines[id] = timeline.items.compactMap { item -> [String: Any]? in
            guard case .suggestion(let suggestion) = item.content else { return nil }
            return ["item": item.id, "message_id": item.messageID as Any? ?? NSNull(), "card": card(suggestion)]
        }
    }
    return ["log": log, "open": state.openSuggestions.map(card), "count": state.openSuggestionCount,
            "known": state.suggestions.keys.sorted(), "timelines": timelines,
            "sidebar_entries": state.sidebarEntries().map(\.id), "watch_cursor": state.watchCursor,
            "pending": state.pendingApprovals.keys.sorted()]
}
