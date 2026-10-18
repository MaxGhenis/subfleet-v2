// Subfleet: the unified diff `turn.diff` and `conversation.diff` return
// (C-26.14, design D-25), split into one section per file and one row per line
// for the Changes pane. The daemon writes it with `git diff-tree -p` and the
// prefixes `a/` and `b/`; lines it scrubbed keep their `+`, `-` or space.
// Foundation only.

import Foundation

struct DiffLine: Equatable, Identifiable {
    enum Kind: String {
        /// `@@ -a,b +c,d @@`
        case hunk
        case context
        case added
        case removed
        /// `\ No newline at end of file`, or a line outside any hunk.
        case meta
    }

    /// The row's place in the whole diff, so rows keep their identity across sections.
    var id: Int
    var kind: Kind
    /// Without its `+`, `-` or space prefix; a hunk or meta line whole.
    var text: String
    /// Line numbers in the old and the new file.
    var old: Int?
    var new: Int?
}

struct DiffSection: Equatable, Identifiable {
    /// The file's path after the change (before it, for a deleted file),
    /// relative to the checkout's top level.
    var path: String
    /// The lines before the first hunk: `diff --git`, modes, `index`, renames.
    var header: [String]
    var lines: [DiffLine]
    var binary: Bool

    var id: String { path }
}

enum UnifiedDiff {
    static func parse(_ text: String) -> [DiffSection] {
        var sections: [DiffSection] = []
        var current: DiffSection?
        var inHunk = false
        var oldLine = 0
        var newLine = 0
        var nextID = 0
        var removedPath: String?

        func row(_ kind: DiffLine.Kind, _ text: String, old: Int? = nil, new: Int? = nil) {
            current?.lines.append(DiffLine(id: nextID, kind: kind, text: text, old: old, new: new))
            nextID += 1
        }

        var lines = text.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        for line in lines {
            if line.hasPrefix("diff --git ") {
                if let done = current { sections.append(done) }
                current = DiffSection(path: gitLinePath(String(line.dropFirst("diff --git ".count))) ?? "",
                                      header: [line], lines: [], binary: false)
                inHunk = false
                removedPath = nil
                continue
            }
            guard current != nil else { continue }
            if line.hasPrefix("@@") {
                inHunk = true
                (oldLine, newLine) = hunkStarts(line)
                row(.hunk, line)
                continue
            }
            if inHunk {
                switch line.first {
                case "+":
                    row(.added, String(line.dropFirst()), new: newLine)
                    newLine += 1
                case "-":
                    row(.removed, String(line.dropFirst()), old: oldLine)
                    oldLine += 1
                case " ":
                    row(.context, String(line.dropFirst()), old: oldLine, new: newLine)
                    oldLine += 1
                    newLine += 1
                case nil:
                    // An empty context line whose space was lost on the way.
                    row(.context, "", old: oldLine, new: newLine)
                    oldLine += 1
                    newLine += 1
                default:
                    row(.meta, line)
                }
                continue
            }
            // Before the first hunk: the file's header.
            if line.hasPrefix("--- ") {
                removedPath = headerPath(String(line.dropFirst(4)), prefix: "a/")
            } else if line.hasPrefix("+++ ") {
                if let path = headerPath(String(line.dropFirst(4)), prefix: "b/") ?? removedPath {
                    current?.path = path
                }
            } else if line.hasPrefix("rename to ") || line.hasPrefix("copy to ") {
                current?.path = unquote(String(line.drop(while: { $0 != " " }).dropFirst().drop(while: { $0 != " " }).dropFirst()))
            } else if line.hasPrefix("Binary files ") {
                current?.binary = true
            }
            current?.header.append(line)
        }
        if let done = current { sections.append(done) }
        return sections
    }

    /// `@@ -12,7 +12,9 @@ context` → (12, 12).
    static func hunkStarts(_ line: String) -> (Int, Int) {
        func start(after marker: Character) -> Int {
            guard let at = line.firstIndex(of: marker) else { return 0 }
            let digits = line[line.index(after: at)...].prefix { $0.isNumber }
            return Int(digits) ?? 0
        }
        return (start(after: "-"), start(after: "+"))
    }

    /// `b/src/a.swift` → `src/a.swift`; `/dev/null` → nil.
    static func headerPath(_ value: String, prefix: String) -> String? {
        let path = unquote(value.hasSuffix("\t") ? String(value.dropLast()) : value)
        if path == "/dev/null" { return nil }
        return path.hasPrefix(prefix) ? String(path.dropFirst(prefix.count)) : path
    }

    /// The path in `diff --git a/X b/Y` when nothing later names it (a binary
    /// file, a mode change): X and Y are equal then, so the halves split evenly.
    static func gitLinePath(_ rest: String) -> String? {
        if rest.hasPrefix("\"") {
            // Quoted: `"a/x y" "b/x y"`, or one side quoted.
            let parts = quotedTokens(rest)
            return parts.last.map { $0.hasPrefix("b/") ? String($0.dropFirst(2)) : $0 }
        }
        let count = rest.count
        if count >= 5, (count - 5) % 2 == 0 {
            let half = (count - 5) / 2
            let old = rest.dropFirst(2).prefix(half)
            if rest.hasPrefix("a/") && rest.hasSuffix(" b/" + old) { return String(old) }
        }
        if let split = rest.range(of: " b/", options: .backwards) { return String(rest[split.upperBound...]) }
        return nil
    }

    /// Git's C-style quoting (`core.quotepath=false` still quotes `"`, `\` and
    /// control characters): `"a\tb\\c\303\251"` → the path's own bytes.
    static func unquote(_ value: String) -> String {
        guard value.count >= 2, value.hasPrefix("\""), value.hasSuffix("\"") else { return value }
        var bytes: [UInt8] = []
        var chars = Array(value.dropFirst().dropLast().utf8)[...]
        while let byte = chars.popFirst() {
            guard byte == UInt8(ascii: "\\"), let next = chars.popFirst() else { bytes.append(byte); continue }
            switch next {
            case UInt8(ascii: "n"): bytes.append(10)
            case UInt8(ascii: "t"): bytes.append(9)
            case UInt8(ascii: "r"): bytes.append(13)
            case UInt8(ascii: "a"): bytes.append(7)
            case UInt8(ascii: "b"): bytes.append(8)
            case UInt8(ascii: "f"): bytes.append(12)
            case UInt8(ascii: "v"): bytes.append(11)
            case UInt8(ascii: "0")...UInt8(ascii: "7"):
                var octal = Int(next - UInt8(ascii: "0"))
                for _ in 0..<2 {
                    guard let digit = chars.first, (UInt8(ascii: "0")...UInt8(ascii: "7")).contains(digit) else { break }
                    octal = octal * 8 + Int(digit - UInt8(ascii: "0"))
                    chars.removeFirst()
                }
                bytes.append(UInt8(truncatingIfNeeded: octal))
            default: bytes.append(next)
            }
        }
        return String(decoding: bytes, as: UTF8.self)
    }

    private static func quotedTokens(_ rest: String) -> [String] {
        var tokens: [String] = []
        var index = rest.startIndex
        while index < rest.endIndex {
            if rest[index] == " " { index = rest.index(after: index); continue }
            if rest[index] == "\"" {
                var end = rest.index(after: index)
                while end < rest.endIndex, rest[end] != "\"" {
                    if rest[end] == "\\" { end = rest.index(after: end) }
                    if end < rest.endIndex { end = rest.index(after: end) }
                }
                let stop = end < rest.endIndex ? rest.index(after: end) : end
                tokens.append(unquote(String(rest[index..<stop])))
                index = stop
            } else {
                let end = rest[index...].firstIndex(of: " ") ?? rest.endIndex
                tokens.append(String(rest[index..<end]))
                index = end
            }
        }
        return tokens
    }
}

/// "3 files, +20 −4"; "1 file, binary".
func diffStatsWords(_ stats: DiffStats) -> String {
    let files = "\(stats.files) file\(stats.files == 1 ? "" : "s")"
    if stats.additions == 0 && stats.deletions == 0 { return files }
    return "\(files)  +\(stats.additions) −\(stats.deletions)"
}

/// Why a diff has nothing to show, in the person's words; the daemon's own
/// detail follows where it says more than the reason.
func diffUnavailableWords(_ result: DiffResult) -> String {
    let words: String
    switch result.reason {
    case "no-turn": words = "This message has not started a turn yet."
    case "read-only-turn": words = "This turn was read-only: it could not change files."
    case "no-snapshot":
        words = result.message_id == nil
            ? "No writable turn of this conversation has started in a git checkout with a commit."
            : "The workspace was not a git checkout with a commit when this turn started, so there is nothing to compare."
    case "snapshot-failed": words = "The snapshot could not be taken: \(result.detail ?? "no detail")."
    case "snapshot-pruned": words = "The repository no longer holds this snapshot, so the changes cannot be shown."
    case "workspace-gone": words = "The workspace is no longer a git checkout (moved or removed)."
    default: words = result.detail ?? "Nothing to compare."
    }
    let sharing = diffSharedWords(result).map { [$0] } ?? []
    return ([words] + sharing).joined(separator: " ")
}

/// C-26.14 (2026-09-29): conversations share folders, so a diff never claims one
/// conversation made it. When other conversations' turns wrote in the folder while
/// this turn ran (or since the conversation's first turn), the pane says so above
/// the diff; nil when none did or the daemon is older than the field.
func diffSharedWords(_ result: DiffResult) -> String? {
    guard let shared = result.shared, !shared.isEmpty else { return nil }
    let names = shared.map { sharer -> String in
        let name = sharer.title.flatMap(sharedTitleWords) ?? "an untitled conversation"
        return sharer.to == nil ? "\(name) (still running)" : name
    }
    let who: String
    switch names.count {
    case 1: who = names[0]
    case 2: who = "\(names[0]) and \(names[1])"
    default: who = names.dropLast().joined(separator: ", ") + " and " + names[names.count - 1]
    }
    let when = result.message_id == nil ? "since this conversation's first turn began" : "during this turn"
    let whose = shared.count == 1 ? "its" : "their"
    return "This folder was also changed by \(who) \(when); the diff may include \(whose) edits."
}

/// The most of a title the note above a diff quotes, in characters.
let sharedTitleLimit = 60

/// A title as that note quotes it: one line, at most `sharedTitleLimit` characters
/// with an ellipsis where it was cut (a title is whatever a person or a native
/// session gave it, of any length); nil for a blank one, which reads as untitled.
func sharedTitleWords(_ title: String) -> String? {
    let line = title.split(whereSeparator: \.isNewline).joined(separator: " ").trimmingCharacters(in: .whitespaces)
    if line.isEmpty { return nil }
    let short = line.count > sharedTitleLimit ? line.prefix(sharedTitleLimit - 1) + "\u{2026}" : Substring(line)
    return "\u{201C}\(short)\u{201D}"
}

/// What the daemon cut or hid, so a short diff is not taken for the whole one:
/// the listing's and the text's cuts, scrubbed values, and the nested
/// repositories with no commit a live snapshot left out (`to.skipped`, C-13.1),
/// the first `diffSkippedShown` by name.
func diffNotes(_ result: DiffResult) -> [String] {
    var notes: [String] = []
    if result.files_truncated { notes.append("Only the first \(result.files.count) files are listed.") }
    if !result.stats.complete { notes.append("Git's listing was cut; the counts cover the listed files only.") }
    if result.truncated { notes.append("The diff is cut at its size limit; the rest is not shown.") }
    if result.scrubbed > 0 {
        notes.append("\(result.scrubbed) value\(result.scrubbed == 1 ? "" : "s") that looked like credentials are replaced.")
    }
    if let skipped = result.to?.skipped, !skipped.isEmpty {
        let one = skipped.count == 1
        let more = skipped.count - diffSkippedShown
        let names = skipped.prefix(diffSkippedShown).joined(separator: ", ") + (more > 0 ? " and \(more) more" : "")
        notes.append("\(skipped.count) nested repositor\(one ? "y" : "ies") with no commit \(one ? "is" : "are") not shown: \(names).")
    }
    return notes
}

/// An empty comparison still discloses other conversations' overlapping writes:
/// edits can cancel out, leaving no net changes to show (C-26.14).
func diffEmptyWords(_ result: DiffResult) -> String {
    let notes = diffNotes(result)
    let sharing = diffSharedWords(result).map { [$0] } ?? []
    return ([notes.isEmpty ? "No changes." : "No changes to show."] + sharing + notes).joined(separator: " ")
}

/// How many left-out repositories `diffNotes` names, as the daemon's notice does.
let diffSkippedShown = 5

/// What the Changes pane shows: a whole conversation, or one turn of it.
enum ChangesScope: Hashable {
    case conversation(String)
    case turn(conversationID: String, messageID: String)

    var conversationID: String {
        switch self {
        case .conversation(let id): return id
        case .turn(let id, _): return id
        }
    }
}

/// The pane's last answer for a scope, parsed once when it arrives.
enum ChangesLoad: Equatable {
    case loading
    case loaded(DiffResult, [DiffSection])
    case failed(String)
}
