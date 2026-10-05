// Core probe subcommands for the ⌘K palette's matching and ranking (C-29.12)
// and the text scale (C-29.13). Offsets are in Unicode scalars, as Python
// indexes strings.
import Foundation

func scalarRanges(_ ranges: [Range<String.Index>], in text: String) -> [[Int]] {
    let scalars = text.unicodeScalars
    return ranges.map { range in
        [scalars.distance(from: scalars.startIndex, to: range.lowerBound),
         scalars.distance(from: range.lowerBound, to: range.upperBound)]
    }
}

func probeCandidate(_ value: JSONValue) -> SearchCandidate {
    let id = value["id"]?.string ?? ""
    let provider = value["provider"]?.string ?? "claude"
    let workspace = value["workspace"]?.string ?? ""
    let target: SidebarEntry.Target = value["kind"]?.string == "session"
        ? .native(NativeSessionRef(provider: provider, session_id: id, home: nil)) : .conversation(id)
    let entry = SidebarEntry(id: id, target: target, provider: provider, title: value["title"]?.string ?? "",
                             subtitle: workspace, workspace: workspace,
                             date: value["date"]?.double.map { Date(timeIntervalSince1970: $0) }, pendingApprovals: 0,
                             active: false, blockedBy: nil, liveElsewhere: false,
                             continuable: value["continuable"]?.bool ?? true, continueBlocker: nil)
    let messages = (value["messages"]?.array ?? []).map {
        SearchMessage(itemID: $0["item"]?.string, author: $0["author"]?.string ?? "You", text: $0["text"]?.string ?? "")
    }
    return SearchCandidate(entry: entry, messages: messages)
}

func project(_ result: SearchResult) -> [String: Any] {
    var out: [String: Any] = [
        "id": result.id, "tier": result.tier.name, "title": result.entry.title,
        "title_highlights": scalarRanges(result.titleHighlights, in: result.entry.title),
        "workspace": result.entry.subtitle,
        "workspace_highlights": scalarRanges(result.workspaceHighlights, in: result.entry.subtitle),
        "provider_matched": result.providerMatched, "snippet": NSNull(),
    ]
    if let snippet = result.snippet {
        out["snippet"] = ["item": snippet.itemID as Any? ?? NSNull(), "author": snippet.author, "text": snippet.text,
                          "highlights": scalarRanges(snippet.highlights, in: snippet.text)]
    }
    return out
}

func project(_ outcome: SearchOutcome) -> [String: Any] {
    ["query": outcome.query, "total": outcome.total, "ignored_words": outcome.ignoredWords,
     "results": outcome.results.map(project)]
}

/// `search <input.json>`: `{"candidates", "queries", "limit"?, "cache"?}`. With
/// `cache`, the index is built twice through one fold cache and both answers are
/// reported, with the cache's size after each build.
func runSearch(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let candidates = (input["candidates"]?.array ?? []).map(probeCandidate)
    let queries = (input["queries"]?.array ?? []).compactMap(\.string)
    let limit = input["limit"]?.int ?? 50
    func answers(_ index: SearchIndex) -> [Any] {
        queries.map { project(index.search($0, limit: limit)!) }
    }
    let index = SearchIndex(candidates)
    // The processor time each search takes, whatever else the machine runs.
    let seconds = queries.map { query -> Double in
        let started = clock()
        _ = index.search(query, limit: limit)
        return Double(clock() - started) / Double(CLOCKS_PER_SEC)
    }
    var out: [String: Any] = ["answers": answers(index), "cpu_seconds": seconds]
    if input["cache"]?.bool == true {
        let cache = SearchFoldCache()
        out["cached"] = answers(SearchIndex(candidates, cache: cache))
        out["cache_size"] = cache.count
        out["recached"] = answers(SearchIndex(candidates, cache: cache))
        out["cache_size_again"] = cache.count
        out["cache_size_after_fewer"] = { () -> Int in
            _ = SearchIndex(Array(candidates.prefix(1)), cache: cache)
            return cache.count
        }()
    }
    var cancelled = 0
    out["cancelled"] = SearchIndex(candidates).search(queries.first ?? "a", limit: limit, isCancelled: {
        cancelled += 1
        return true
    }) == nil
    return out
}

/// `search-fold <input.json>`: `{"texts", "pairs": [[text, word]]}`: the ASCII
/// fold against Foundation's, and the index's containment against
/// `range(of:options:)`.
func runSearchFold(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let folds = (input["texts"]?.array ?? []).compactMap(\.string).map { text -> [String: Any] in
        let folded = SearchFold.fold(text)
        return ["bytes": folded.bytes.map(Int.init), "ascii": folded.ascii,
                "foundation": SearchFold.foundationFold(text).map(Int.init)]
    }
    let pairs = (input["pairs"]?.array ?? []).compactMap { pair -> [String: Any]? in
        guard let text = pair.array?.first?.string, let word = pair.array?.last?.string else { return nil }
        let bytes = SearchFold.fold(word).bytes
        return ["folded": !bytes.isEmpty && SearchFold.contains(SearchFold.fold(text).bytes, bytes),
                "foundation": !bytes.isEmpty && text.range(of: word, options: SearchFold.options) != nil]
    }
    return ["folds": folds, "pairs": pairs]
}

/// `search-state <input.json>`: fold store steps (`list`, `open`, `events` with
/// `conversation_id`, `history` with `conversation_id`, `local`), set the
/// sidebar's `search`/`provider_filter`, then list the palette's candidates
/// (with `sessions` from a daemon query) and answer `queries`.
func runSearchState(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var state = ConversationStoreState()
    for step in input["steps"]?.array ?? [] {
        if let list = step["list"] { state.apply(list: try list.decode(ConversationListResult.self)) }
        if let open = step["open"] { state.apply(open: try open.decode(ConversationOpenResult.self)) }
        if let page = step["events"], let cid = step["conversation_id"]?.string {
            state.apply(events: try page.decode(EventsPage.self), conversationID: cid)
        }
        if let page = step["history"], let cid = step["conversation_id"]?.string {
            state.apply(history: try page.decode(HistoryPage.self), conversationID: cid)
        }
        if let local = step["local"] {
            state.addLocalMessage(conversationID: local["conversation_id"]?.string ?? "",
                                  messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "")
        }
    }
    state.searchQuery = input["search"]?.string ?? ""
    state.providerFilter = input["provider_filter"]?.string
    let sessions = try input["sessions"]?.decode([CatalogItem].self) ?? []
    let candidates = state.searchCandidates(sessions: sessions)
    let index = SearchIndex(candidates)
    return [
        "sidebar": state.sidebarEntries().map(\.id),
        "candidates": candidates.map { candidate -> [String: Any] in
            ["id": candidate.entry.id, "title": candidate.entry.title, "workspace": candidate.entry.subtitle,
             "provider": candidate.entry.provider,
             "messages": candidate.messages.map { ["item": $0.itemID as Any? ?? NSNull(), "author": $0.author, "text": $0.text] }]
        },
        "answers": (input["queries"]?.array ?? []).compactMap(\.string).map { project(index.search($0)!) },
    ]
}

/// A JSON number, or "nan", "inf" and "-inf".
func probeDouble(_ value: JSONValue) -> Double? {
    if let number = value.double { return number }
    switch value.string {
    case "nan": return .nan
    case "inf": return .infinity
    case "-inf": return -.infinity
    default: return nil
    }
}

/// `text-scale <input.json>`: `{"values", "scales", "suite", "stored"}`: clamp,
/// Bigger and Smaller for each value; every reading size at each scale; and
/// the setting saved and loaded through a throwaway UserDefaults suite (a path,
/// so its plist stays in the test's directory), also when what is stored there
/// is not a scale.
func runTextScale(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let values = (input["values"]?.array ?? []).compactMap(probeDouble)
    let scales = (input["scales"]?.array ?? []).compactMap(probeDouble)
    func finite(_ value: Double) -> Any { value.isFinite ? value : NSNull() }
    var out: [String: Any] = [
        "key": TextScale.defaultsKey, "actual": TextScale.actual, "steps": TextScale.steps,
        "range": [TextScale.range.lowerBound, TextScale.range.upperBound],
        "minimum_point_size": ReadingStyle.minimumPointSize,
        "values": values.map { value -> [String: Any] in
            ["clamp": TextScale.clamp(value), "bigger": TextScale.bigger(value), "smaller": TextScale.smaller(value),
             "can_enlarge": TextScale.canEnlarge(value), "can_reduce": TextScale.canReduce(value),
             "is_actual": TextScale.isActual(value)]
        },
        "styles": ReadingStyle.allCases.map(\.rawValue),
        "base": Dictionary(uniqueKeysWithValues: ReadingStyle.allCases.map { ($0.rawValue, $0.basePointSize) }),
        "weights": Dictionary(uniqueKeysWithValues: ReadingStyle.allCases.map { ($0.rawValue, $0.weight.rawValue) }),
        "sizes": scales.map { scale -> [String: Any] in
            ["scale": finite(scale),
             "sizes": Dictionary(uniqueKeysWithValues: ReadingStyle.allCases.map { ($0.rawValue, $0.pointSize(scale: scale)) }),
             "column": ReadingStyle.columnWidth(scale: scale), "bubble": ReadingStyle.bubbleWidth(scale: scale)]
        },
    ]
    if let suite = input["suite"]?.string, let defaults = UserDefaults(suiteName: suite) {
        defaults.removePersistentDomain(forName: suite)
        var persisted: [String: Any] = ["missing": TextScale.load(from: defaults)]
        TextScale.save(1.25, to: defaults)
        persisted["saved"] = UserDefaults(suiteName: suite)?.load()
        persisted["raw_after_save"] = defaults.object(forKey: TextScale.defaultsKey) ?? NSNull()
        TextScale.save(9, to: defaults)
        persisted["saved_too_large"] = UserDefaults(suiteName: suite)?.load()
        var stored: [Any] = []
        var normalized: [Any] = []
        for value in input["stored"]?.array ?? [] {
            switch value {
            case .string(let text): defaults.set(text, forKey: TextScale.defaultsKey)
            case .bool(let flag): defaults.set(flag, forKey: TextScale.defaultsKey)
            case .null: defaults.removeObject(forKey: TextScale.defaultsKey)
            default: if let number = value.double { defaults.set(number, forKey: TextScale.defaultsKey) }
            }
            stored.append(TextScale.load(from: defaults))
            // What the app's @AppStorage reads after launch normalizes it: a Double, or nothing.
            TextScale.normalize(defaults)
            normalized.append(defaults.object(forKey: TextScale.defaultsKey).map { ($0 as? Double) as Any? ?? "not a Double" }
                              ?? NSNull())
        }
        persisted["stored"] = stored
        persisted["normalized"] = normalized
        defaults.removePersistentDomain(forName: suite)
        out["persisted"] = persisted
    }
    return out
}

private extension UserDefaults {
    func load() -> Double { TextScale.load(from: self) }
}

func searchProbeCommand(_ arguments: [String]) throws -> Any? {
    switch arguments[1] {
    case "search": return try runSearch(readFile(arguments[2]))
    case "search-fold": return try runSearchFold(readFile(arguments[2]))
    case "search-state": return try runSearchState(readFile(arguments[2]))
    case "text-scale": return try runTextScale(readFile(arguments[2]))
    case "code-expansion":
        // code-expansion <input.json>: [[shown, total]]: the lines a code block
        // shows after "Show more lines"
        let pairs = try JSONValue.parse(readFile(arguments[2])).array ?? []
        return ["code_lines": MarkdownBounds.codeLines, "step": MarkdownBounds.codeExpansion,
                "expanded": pairs.map {
                    MarkdownBounds.expandedCodeLines(from: $0.array?.first?.int ?? 0, total: $0.array?.last?.int ?? 0)
                }]
    default: return nil
    }
}
