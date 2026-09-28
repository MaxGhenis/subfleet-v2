// Subfleet: the ⌘K palette's matching and ranking (C-29.12), as plain Swift.
//
// The palette finds conversations and native sessions. A query is split into
// words at white space; a candidate matches when every word appears, ignoring
// case, diacritics and width, somewhere in its title, its workspace as the
// sidebar shows it, its provider, or its messages' text (a loaded
// conversation's timeline, a session's first prompt). Matches rank in tiers:
//
//   exact title   the title is the query (white space aside);
//   title         every word is in the title;
//   details       every word is in the title, workspace or provider;
//   message       some word is only in message text: the result carries a
//                 snippet of the message holding the most of those words;
//   fuzzy title   a word found nowhere has its letters in the title in
//                 order, close together ("sbflt" in "Subfleet"), as Claude
//                 Code's palette matches titles; the others are found as
//                 above.
//
// Within a tier the most recently active comes first (undated last); ties go
// by title, then id, so the order never depends on the candidates' order. An
// empty query lists every candidate by recent activity.
//
// Text is folded once, when the index is built (ASCII by lowering its letters,
// the rest with Foundation's folding), and words are found in the folded bytes
// with `memmem`: `range(of:options:)` over a few megabytes of timeline takes
// seconds. Highlights fold the displayed text a character at a time the same
// way, so they mark exactly what matched, whole characters: where folding makes
// one character several ("ß" is "ss", "ﬁ" is "fi"), a word matches part of
// them, which `range(of:options:)` does not (and it finds "ß" in "st", which
// folding does not). Foundation only.

import Foundation

// MARK: - Folding

/// Text as it is matched: its UTF-8 with case, diacritics and width folded
/// away. `ascii` says the text was all ASCII, so byte offsets are the text's own.
struct FoldedText: Equatable {
    var bytes: [UInt8]
    var ascii: Bool
}

enum SearchFold {
    static let options: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive, .widthInsensitive]

    static func fold(_ text: String) -> FoldedText {
        var text = text
        text.makeContiguousUTF8()
        let lowered: [UInt8]?? = text.utf8.withContiguousStorageIfAvailable { buffer -> [UInt8]? in
            var out = [UInt8]()
            out.reserveCapacity(buffer.count)
            for byte in buffer {
                if byte >= 0x80 { return nil }
                out.append(byte &- 0x41 < 26 ? byte | 0x20 : byte)
            }
            return out
        }
        if case let bytes?? = lowered { return FoldedText(bytes: bytes, ascii: true) }
        return FoldedText(bytes: foundationFold(text), ascii: false)
    }

    /// Foundation's folding, which the ASCII path must equal.
    static func foundationFold(_ text: String) -> [UInt8] {
        Array(text.folding(options: options, locale: nil).utf8)
    }

    static func contains(_ haystack: [UInt8], _ needle: [UInt8]) -> Bool { offset(of: needle, in: haystack) != nil }

    /// Where `needle` first starts in `haystack` at or after `from`, in bytes.
    static func offset(of needle: [UInt8], in haystack: [UInt8], from: Int = 0) -> Int? {
        guard !needle.isEmpty else { return from }
        guard from >= 0, needle.count <= haystack.count - min(from, haystack.count) else { return nil }
        return haystack.withUnsafeBytes { hay in
            needle.withUnsafeBytes { word in
                guard let base = hay.baseAddress,
                      let found = memmem(base + from, hay.count - from, word.baseAddress, word.count) else { return nil }
                return base.distance(to: UnsafeRawPointer(found))
            }
        }
    }

    /// Folded bytes of `text`, with the character each byte came from: what
    /// highlights are found in. Folding a character at a time gives the bytes
    /// folding the whole text does; a character of combining marks alone (one
    /// after a control character) folds to nothing there, except first.
    static func foldedCharacters(_ text: Substring) -> (bytes: [UInt8], owners: [Range<String.Index>]) {
        var bytes: [UInt8] = []
        var owners: [Range<String.Index>] = []
        bytes.reserveCapacity(text.utf8.count)
        owners.reserveCapacity(text.utf8.count)
        var index = text.startIndex
        while index < text.endIndex {
            let next = text.index(after: index)
            let character = text[index]
            let folded = character.isASCII ? character.utf8.map { $0 &- 0x41 < 26 ? $0 | 0x20 : $0 }
                : index != text.base.startIndex && SearchQuery.isMarks(character.unicodeScalars) ? []
                : foundationFold(String(character))
            bytes += folded
            owners += repeatElement(index..<next, count: folded.count)
            index = next
        }
        return (bytes, owners)
    }
}

// MARK: - Query

struct SearchQuery: Equatable {
    /// The words as typed.
    var words: [String]
    var folded: [[UInt8]]
    /// The folded words joined by single spaces, compared with a title's.
    var exact: [UInt8]
    /// Words past `maximumWords`, which are not searched.
    var ignoredWords: Int
    /// The folded words as Unicode scalars, for fuzzy title matches.
    var scalars: [[Unicode.Scalar]]

    static let maximumWords = 12

    init(_ text: String) {
        var words: [String] = []
        var folded: [[UInt8]] = []
        var ignored = 0
        for word in text.split(whereSeparator: { $0.isWhitespace }) {
            guard let bytes = SearchQuery.fold(word), !folded.contains(bytes) else { continue }
            guard words.count < SearchQuery.maximumWords else {
                ignored += 1
                continue
            }
            words.append(String(word))
            folded.append(bytes)
        }
        self.words = words
        self.folded = folded
        self.scalars = folded.map(SearchFuzzy.scalars)
        self.ignoredWords = ignored
        self.exact = SearchQuery.exactForm(text)
    }

    var isEmpty: Bool { words.isEmpty }

    /// A word's folded bytes; nil for one that folds to nothing or is only
    /// combining marks (a lone accent), which is no word.
    static func fold(_ word: Substring) -> [UInt8]? {
        guard !isMarks(word.unicodeScalars) else { return nil }
        let bytes = SearchFold.fold(String(word)).bytes
        return bytes.isEmpty ? nil : bytes
    }

    /// Whether every scalar is a combining mark (true of none).
    static func isMarks<S: Sequence>(_ scalars: S) -> Bool where S.Element == Unicode.Scalar {
        scalars.allSatisfy { [.nonspacingMark, .spacingMark, .enclosingMark].contains($0.properties.generalCategory) }
    }

    /// A text's folded words joined by single spaces: a title equals a query
    /// when their exact forms do.
    static func exactForm(_ text: String) -> [UInt8] {
        Array(text.split(whereSeparator: { $0.isWhitespace }).compactMap(fold).joined(separator: [0x20]))
    }
}

// MARK: - Candidates and results

struct SearchMessage: Equatable {
    /// The timeline row it came from, to scroll to; nil for a session's first prompt.
    var itemID: String?
    /// "You", the provider's name, or "First prompt".
    var author: String
    var text: String
}

struct SearchCandidate: Equatable {
    /// What opening it selects, and what a result shows: its title, workspace
    /// (`subtitle`), provider and date.
    var entry: SidebarEntry
    /// Oldest first.
    var messages: [SearchMessage] = []
}

enum SearchTier: Int, Comparable, CaseIterable {
    case exactTitle, title, details, message, fuzzyTitle
    /// The empty query's list.
    case recent

    static func < (lhs: SearchTier, rhs: SearchTier) -> Bool { lhs.rawValue < rhs.rawValue }

    var name: String {
        switch self {
        case .exactTitle: return "exact-title"
        case .title: return "title"
        case .details: return "details"
        case .message: return "message"
        case .fuzzyTitle: return "fuzzy-title"
        case .recent: return "recent"
        }
    }
}

struct SearchSnippet: Equatable {
    var itemID: String?
    var author: String
    /// The message around the match, on one line, with "…" where it was cut.
    var text: String
    var highlights: [Range<String.Index>]
}

struct SearchResult: Identifiable, Equatable {
    var entry: SidebarEntry
    var tier: SearchTier
    var titleHighlights: [Range<String.Index>] = []
    /// In `entry.subtitle`, the workspace as the sidebar shows it.
    var workspaceHighlights: [Range<String.Index>] = []
    var providerMatched = false
    var snippet: SearchSnippet?

    var id: String { entry.id }
}

struct SearchOutcome: Equatable {
    var query: String
    var results: [SearchResult]
    /// Every match; `results` are the first of them.
    var total: Int
    var ignoredWords: Int

    static let empty = SearchOutcome(query: "", results: [], total: 0, ignoredWords: 0)
}

// MARK: - Index

/// Folded message texts kept between index builds, so reopening the palette
/// folds only what changed. Each build keeps only the entries it used.
final class SearchFoldCache: @unchecked Sendable {
    private var entries: [String: (source: String, folded: FoldedText)] = [:]
    private let lock = NSLock()

    func fold(_ text: String, key: String) -> FoldedText {
        lock.lock()
        defer { lock.unlock() }
        // Byte for byte: `==` holds for canonically equivalent texts of other lengths.
        if let hit = entries[key], hit.source.utf8.elementsEqual(text.utf8) { return hit.folded }
        let folded = SearchFold.fold(text)
        entries[key] = (text, folded)
        return folded
    }

    func retain(_ keys: Set<String>) {
        lock.lock()
        defer { lock.unlock() }
        entries = entries.filter { keys.contains($0.key) }
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return entries.count
    }
}

struct SearchIndex {
    struct Document {
        var candidate: SearchCandidate
        var title: [UInt8]
        var titleScalars: [Unicode.Scalar]
        var exactTitle: [UInt8]
        var workspace: [UInt8]
        var provider: [UInt8]
        var messages: [FoldedText]
    }

    let documents: [Document]

    /// The first candidate of each id is kept.
    init(_ candidates: [SearchCandidate], cache: SearchFoldCache? = nil) {
        var seen = Set<String>()
        var keys = Set<String>()
        var documents: [Document] = []
        for candidate in candidates where seen.insert(candidate.entry.id).inserted {
            let messages = candidate.messages.enumerated().map { index, message -> FoldedText in
                guard let cache else { return SearchFold.fold(message.text) }
                let key = candidate.entry.id + "\u{1F}" + (message.itemID ?? "#\(index)")
                keys.insert(key)
                return cache.fold(message.text, key: key)
            }
            let title = SearchFold.fold(candidate.entry.title).bytes
            documents.append(Document(candidate: candidate, title: title, titleScalars: SearchFuzzy.scalars(title),
                                      exactTitle: SearchQuery.exactForm(candidate.entry.title),
                                      workspace: SearchFold.fold(candidate.entry.subtitle).bytes,
                                      provider: SearchFold.fold(candidate.entry.provider).bytes, messages: messages))
        }
        cache?.retain(keys)
        self.documents = documents
    }

    /// The first `limit` matches in rank order and how many there are; nil when
    /// `isCancelled` says so first.
    func search(_ text: String, limit: Int = 50, isCancelled: () -> Bool = { false }) -> SearchOutcome? {
        let query = SearchQuery(text)
        guard !query.isEmpty else {
            let order = documents.indices.sorted { SearchIndex.precedes(documents[$0], documents[$1]) }
            return SearchOutcome(query: text, results: order.prefix(max(0, limit)).map {
                SearchResult(entry: documents[$0].candidate.entry, tier: .recent)
            }, total: documents.count, ignoredWords: query.ignoredWords)
        }
        var matches: [Match] = []
        for (index, document) in documents.enumerated() {
            if index % 32 == 0 && isCancelled() { return nil }
            if let match = SearchIndex.match(document, query) {
                matches.append(Match(document: index, tier: match.tier, messageWords: match.messageWords,
                                     fuzzyWords: match.fuzzyWords))
            }
        }
        matches.sort { lhs, rhs in
            lhs.tier != rhs.tier ? lhs.tier < rhs.tier : SearchIndex.precedes(documents[lhs.document], documents[rhs.document])
        }
        var results: [SearchResult] = []
        for match in matches.prefix(max(0, limit)) {
            if isCancelled() { return nil }
            results.append(result(match, query))
        }
        return SearchOutcome(query: text, results: results, total: matches.count, ignoredWords: query.ignoredWords)
    }

    private struct Match {
        var document: Int
        var tier: SearchTier
        /// The query's words (by position) that only the messages hold.
        var messageWords: [Int]
        /// The query's words found nowhere but as a fuzzy title match.
        var fuzzyWords: [Int]
    }

    static func match(_ document: Document, _ query: SearchQuery)
        -> (tier: SearchTier, messageWords: [Int], fuzzyWords: [Int])? {
        var inTitle = true
        var messageWords: [Int] = []
        for (position, word) in query.folded.enumerated() {
            if SearchFold.contains(document.title, word) { continue }
            inTitle = false
            if SearchFold.contains(document.workspace, word) || SearchFold.contains(document.provider, word) { continue }
            messageWords.append(position)
        }
        if inTitle { return (document.exactTitle == query.exact ? .exactTitle : .title, [], []) }
        if messageWords.isEmpty { return (.details, [], []) }
        let inMessages = messageWords.filter { position in
            document.messages.contains { SearchFold.contains($0.bytes, query.folded[position]) }
        }
        if inMessages.count == messageWords.count { return (.message, messageWords, []) }
        let fuzzy = messageWords.filter { !inMessages.contains($0) }
        guard fuzzy.allSatisfy({ SearchFuzzy.positions(query.scalars[$0], in: document.titleScalars) != nil }) else {
            return nil
        }
        return (.fuzzyTitle, inMessages, fuzzy)
    }

    /// Recent activity first, undated last; then title, then id.
    static func precedes(_ lhs: Document, _ rhs: Document) -> Bool {
        switch (lhs.candidate.entry.date, rhs.candidate.entry.date) {
        case let (left?, right?) where left != right: return left > right
        case (.some, nil): return true
        case (nil, .some): return false
        default: break
        }
        if lhs.title != rhs.title { return lhs.title.lexicographicallyPrecedes(rhs.title) }
        return lhs.candidate.entry.id < rhs.candidate.entry.id
    }

    private func result(_ match: Match, _ query: SearchQuery) -> SearchResult {
        let document = documents[match.document]
        let entry = document.candidate.entry
        var result = SearchResult(entry: entry, tier: match.tier)
        let found = query.folded.indices.filter { !match.fuzzyWords.contains($0) }.map { query.folded[$0] }
        result.titleHighlights = SearchHighlight.merge(SearchHighlight.ranges(of: found, in: entry.title)
            + SearchHighlight.fuzzyRanges(of: match.fuzzyWords.map { query.scalars[$0] }, in: entry.title))
        result.workspaceHighlights = SearchHighlight.ranges(of: query.folded, in: entry.subtitle)
        result.providerMatched = query.folded.contains { SearchFold.contains(document.provider, $0) }
        if !match.messageWords.isEmpty { result.snippet = snippet(document, query, words: match.messageWords) }
        return result
    }

    /// The newest of the messages holding the most of `words`, cut around the
    /// first of them it holds.
    private func snippet(_ document: Document, _ query: SearchQuery, words: [Int]) -> SearchSnippet? {
        var best: (index: Int, count: Int)?
        for index in document.messages.indices.reversed() {
            let count = words.filter { SearchFold.contains(document.messages[index].bytes, query.folded[$0]) }.count
            if count > (best?.count ?? 0) { best = (index, count) }
            if count == words.count { break }
        }
        guard let best else { return nil }
        let message = document.candidate.messages[best.index]
        let folded = document.messages[best.index]
        let first = words.compactMap { position in
            SearchFold.offset(of: query.folded[position], in: folded.bytes).map { (position: position, offset: $0) }
        }.min { $0.offset < $1.offset }
        guard let first else { return nil }
        let anchor = SearchSnippetText.locate(query.folded[first.position], foldedOffset: first.offset, in: message.text,
                                              folded: folded)
        let text = SearchSnippetText.window(message.text, around: anchor)
        return SearchSnippet(itemID: message.itemID, author: message.author, text: text,
                             highlights: SearchHighlight.ranges(of: query.folded, in: text))
    }
}

// MARK: - Snippets and highlights

enum SearchSnippetText {
    /// Characters kept before the match, and in all.
    static let before = 40
    static let length = 160

    /// The match in the original text for one of `word` (folded) found at
    /// `foldedOffset` in its folded bytes. ASCII folds byte for byte. Otherwise
    /// newlines fold one for one, so the match is in the same line: a short line
    /// is folded a character at a time to find it; on a long one only a stretch
    /// around where the line's proportion of text to folded bytes puts it, so a
    /// megabyte line costs no more than a short one.
    static func locate(_ word: [UInt8], foldedOffset: Int, in text: String, folded: FoldedText) -> Range<String.Index> {
        let utf8 = text.utf8
        if folded.ascii {
            if let lower = utf8.index(utf8.startIndex, offsetBy: foldedOffset, limitedBy: utf8.endIndex),
               let upper = utf8.index(lower, offsetBy: word.count, limitedBy: utf8.endIndex) {
                return lower..<upper
            }
        }
        let offset = min(max(0, foldedOffset), folded.bytes.count)
        var line = 0
        var foldedLineStart = 0
        for index in 0..<offset where folded.bytes[index] == 0x0A {
            line += 1
            foldedLineStart = index + 1
        }
        var start = utf8.startIndex
        if line > 0 {
            var seen = 0
            for index in utf8.indices where utf8[index] == 0x0A {
                seen += 1
                if seen == line {
                    start = utf8.index(after: index)
                    break
                }
            }
        }
        let end = utf8[start...].firstIndex(of: 0x0A) ?? utf8.endIndex
        let lineBytes = utf8.distance(from: start, to: end)
        if lineBytes <= lineLimit, let found = SearchHighlight.ranges(of: [word], in: text[start..<end]).first {
            return found
        }
        let foldedLineEnd = folded.bytes[foldedLineStart...].firstIndex(of: 0x0A) ?? folded.bytes.count
        let ratio = Double(lineBytes) / Double(max(1, foldedLineEnd - foldedLineStart))
        let estimate = utf8.distance(from: utf8.startIndex, to: start) + Int(Double(offset - foldedLineStart) * ratio)
        for reach in [reach, reach * 16] {
            if let found = near(word, byte: estimate, reach: reach, in: text) { return found }
        }
        let center = scalarIndex(estimate, in: text)
        return center..<center
    }

    /// Lines up to this many bytes are folded whole to find the match.
    static let lineLimit = 16 * 1024
    /// Bytes folded on each side of an estimated match, at first.
    static let reach = 4 * 1024

    /// The match of `word` nearest the byte offset `byte` within `reach` bytes of it.
    static func near(_ word: [UInt8], byte: Int, reach: Int, in text: String) -> Range<String.Index>? {
        let utf8 = text.utf8
        let center = scalarIndex(byte, in: text)
        let found = SearchHighlight.ranges(of: [word], in: text[scalarIndex(byte - reach, in: text)..<scalarIndex(
            byte + reach + word.count, in: text)])
        return found.min {
            abs(utf8.distance(from: center, to: $0.lowerBound)) < abs(utf8.distance(from: center, to: $1.lowerBound))
        }
    }

    /// The start of the scalar at or before byte `offset` (clamped to the text).
    static func scalarIndex(_ offset: Int, in text: String) -> String.Index {
        let utf8 = text.utf8
        var index = utf8.index(utf8.startIndex, offsetBy: min(max(0, offset), utf8.count))
        while index > utf8.startIndex, index < utf8.endIndex, utf8[index] & 0xC0 == 0x80 {
            index = utf8.index(before: index)
        }
        return index
    }

    /// Up to `length` characters from shortly before `anchor`, starting at a
    /// word, with white space (newlines too) made single spaces and "…" where
    /// the message goes on.
    static func window(_ text: String, around anchor: Range<String.Index>) -> String {
        var start = text.index(anchor.lowerBound, offsetBy: -before, limitedBy: text.startIndex) ?? text.startIndex
        if start > text.startIndex, let space = text[start..<anchor.lowerBound].firstIndex(where: \.isWhitespace) {
            start = text.index(after: space)
        }
        var end = text.index(start, offsetBy: length, limitedBy: text.endIndex) ?? text.endIndex
        if end < anchor.upperBound { end = anchor.upperBound }
        let body = text[start..<end].split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let cutBefore = text[..<start].contains { !$0.isWhitespace }
        let cutAfter = text[end...].contains { !$0.isWhitespace }
        return (cutBefore ? "…" : "") + body + (cutAfter ? "…" : "")
    }
}

enum SearchHighlight {
    /// Occurrences of one word looked for in one text, at most.
    static let maximumPerWord = 64

    /// Where the folded `words` occur in `text`, as whole characters, sorted,
    /// overlapping ones merged. The ranges index the string `text` is part of.
    static func ranges(of words: [[UInt8]], in text: Substring) -> [Range<String.Index>] {
        guard !words.isEmpty, !text.isEmpty else { return [] }
        let (bytes, owners) = SearchFold.foldedCharacters(text)
        var found: [Range<String.Index>] = []
        for word in words where !word.isEmpty {
            var from = 0
            var count = 0
            while count < maximumPerWord, let offset = SearchFold.offset(of: word, in: bytes, from: from) {
                found.append(owners[offset].lowerBound..<owners[offset + word.count - 1].upperBound)
                from = offset + word.count
                count += 1
            }
        }
        return merge(found)
    }

    static func ranges(of words: [[UInt8]], in text: String) -> [Range<String.Index>] { ranges(of: words, in: text[...]) }

    /// The characters of `text` that each word's fuzzy match takes, merged.
    static func fuzzyRanges(of words: [[Unicode.Scalar]], in text: String) -> [Range<String.Index>] {
        let (bytes, owners) = SearchFold.foldedCharacters(text[...])
        var scalars: [Unicode.Scalar] = []
        var offsets: [Int] = []
        var offset = 0
        for scalar in String(decoding: bytes, as: UTF8.self).unicodeScalars {
            scalars.append(scalar)
            offsets.append(offset)
            offset += UTF8.width(scalar)
        }
        var found: [Range<String.Index>] = []
        for word in words {
            for position in SearchFuzzy.positions(word, in: scalars) ?? [] where offsets[position] < owners.count {
                found.append(owners[offsets[position]])
            }
        }
        return merge(found)
    }

    static func merge(_ ranges: [Range<String.Index>]) -> [Range<String.Index>] {
        var merged: [Range<String.Index>] = []
        for range in ranges.sorted(by: { $0.lowerBound < $1.lowerBound }) {
            if let last = merged.last, range.lowerBound <= last.upperBound {
                merged[merged.count - 1] = last.lowerBound..<max(last.upperBound, range.upperBound)
            } else {
                merged.append(range)
            }
        }
        return merged
    }

    /// `text` in runs, each marked whether it is highlighted.
    static func segments(_ text: String, _ ranges: [Range<String.Index>]) -> [(text: Substring, highlighted: Bool)] {
        var out: [(text: Substring, highlighted: Bool)] = []
        var position = text.startIndex
        for range in ranges where range.lowerBound >= position && range.upperBound <= text.endIndex {
            if position < range.lowerBound { out.append((text[position..<range.lowerBound], false)) }
            out.append((text[range], true))
            position = range.upperBound
        }
        if position < text.endIndex { out.append((text[position...], false)) }
        return out
    }
}

enum SearchFuzzy {
    /// A fuzzy match may spread over at most this many times the word's length.
    static let spread = 3

    static func scalars(_ folded: [UInt8]) -> [Unicode.Scalar] {
        Array(String(decoding: folded, as: UTF8.self).unicodeScalars)
    }

    /// Where `word`'s letters appear in `title` in order, in the shortest such
    /// stretch (the first of equals); nil when there is none, when it spreads
    /// over more than `spread` times the word's length, or for a word of one
    /// letter (which is a substring or nothing).
    static func positions(_ word: [Unicode.Scalar], in title: [Unicode.Scalar]) -> [Int]? {
        guard word.count >= 2, word.count <= title.count else { return nil }
        var best: [Int]?
        for start in title.indices where title[start] == word[0] {
            var positions = [start]
            var at = start + 1
            for scalar in word.dropFirst() {
                guard let found = title[at...].firstIndex(of: scalar) else { break }
                positions.append(found)
                at = found + 1
            }
            // A later start finds the rest no sooner: none will complete.
            guard positions.count == word.count else { break }
            if best.map({ positions[positions.count - 1] - start < $0[$0.count - 1] - $0[0] }) ?? true { best = positions }
        }
        guard let best, best[best.count - 1] - best[0] + 1 <= spread * word.count else { return nil }
        return best
    }
}

// MARK: - From the store

extension SearchCandidate {
    /// What a timeline says in words: the person's messages, the answers, and
    /// the transcript's rows from before; not thoughts, tool calls or notices.
    static func messages(of timeline: Timeline, provider: String) -> [SearchMessage] {
        let assistant = provider == "codex" ? "Codex" : "Claude"
        return timeline.items.compactMap { item -> SearchMessage? in
            switch item.content {
            case .person(let text?, _, _) where !text.isEmpty:
                return SearchMessage(itemID: item.id, author: "You", text: text)
            case .text(let text, _) where !text.isEmpty:
                return SearchMessage(itemID: item.id, author: assistant, text: text)
            case .history(let role, let text, .none) where !text.isEmpty:
                return SearchMessage(itemID: item.id, author: role == "user" ? "You" : assistant, text: text)
            default:
                return nil
            }
        }
    }
}

extension ConversationStoreState {
    /// Every conversation and native session the palette can find, whatever the
    /// sidebar's search text and provider filter: the loaded conversations with
    /// their timelines' words, the loaded catalog with each session's first
    /// prompt, then `sessions`, catalog items the daemon found beyond the loaded
    /// page (those a conversation already continues are left out).
    func searchCandidates(sessions: [CatalogItem] = []) -> [SearchCandidate] {
        var unfiltered = self
        unfiltered.searchQuery = ""
        unfiltered.providerFilter = nil
        var prompts: [String: String] = [:]
        for item in (catalog?.items ?? []) + sessions {
            if prompts[item.id] == nil, let prompt = item.first_prompt { prompts[item.id] = prompt }
        }
        func candidate(_ entry: SidebarEntry) -> SearchCandidate {
            switch entry.target {
            case .conversation(let id):
                return SearchCandidate(entry: entry, messages: timelines[id].map {
                    SearchCandidate.messages(of: $0, provider: entry.provider)
                } ?? [])
            case .native:
                let prompt = prompts[entry.id]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                return SearchCandidate(entry: entry, messages: prompt.isEmpty ? []
                                       : [SearchMessage(itemID: nil, author: "First prompt", text: prompt)])
            }
        }
        // Session ids are compared in lower case: a binding may spell one either way (C-26.3).
        let bound = Set(conversations.compactMap { conversation in
            conversation.native_session_id.map { "\(conversation.provider):\($0.lowercased())" }
        })
        var out = unfiltered.sidebarEntries().filter { entry in
            guard case .native(let native) = entry.target else { return true }
            return !bound.contains("\(native.provider):\(native.session_id.lowercased())")
        }.map(candidate)
        guard !sessions.isEmpty else { return out }
        let known = Set(out.map { $0.entry.id.lowercased() })
        var extra = ConversationStoreState()
        extra.catalog = Catalog(complete: true, items: sessions.filter {
            !bound.contains("\($0.provider):\($0.native_session_id.lowercased())") && !known.contains($0.id.lowercased())
        })
        out += extra.sidebarEntries().map(candidate)
        return out
    }
}
