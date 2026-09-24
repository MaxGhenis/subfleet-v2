// Subfleet: Markdown for conversation text, parsed into blocks the UI renders.
//
// Blocks: ATX and setext headings, paragraphs, bullet, ordered and task lists
// (nested by indentation), fenced and indented code, block quotes, GFM tables
// and thematic breaks. Inlines: code spans, emphasis, strong, strikethrough,
// links, autolinks and bare URLs, images (shown as links; nothing is fetched),
// escapes, soft and hard breaks. An unclosed fence runs to the end, so a code
// block streams as code. Raw HTML is shown as text. `MarkdownBounds` keeps the
// rendering cost bounded (C-29.8). Foundation only.

import Foundation

indirect enum MarkdownInline: Equatable {
    case text(String)
    case code(String)
    case emphasis([MarkdownInline])
    case strong([MarkdownInline])
    case strikethrough([MarkdownInline])
    case link([MarkdownInline], destination: String)
    case image(alt: String, source: String)
    case softBreak
    case hardBreak
}

enum MarkdownAlignment: String, Equatable {
    case none, left, center, right
}

struct MarkdownListItem: Equatable {
    var blocks: [MarkdownBlock]
    /// A task list item's box: nil for an ordinary item.
    var checked: Bool?
}

enum MarkdownBlock: Equatable {
    case heading(level: Int, content: [MarkdownInline])
    case paragraph([MarkdownInline])
    case list(ordered: Bool, start: Int, tight: Bool, items: [MarkdownListItem])
    /// `closed` is false while a streamed fence has not ended yet.
    case code(language: String?, text: String, closed: Bool)
    case quote([MarkdownBlock])
    case table(header: [[MarkdownInline]], alignments: [MarkdownAlignment], rows: [[[MarkdownInline]]])
    case rule
}

enum Markdown {
    static func parse(_ text: String) -> [MarkdownBlock] {
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
        let lines = normalized.split(separator: "\n", omittingEmptySubsequences: false).map { expandTabs(String($0)) }
        return BlockParser(lines: lines).parse()
    }

    static func parseInlines(_ text: String) -> [MarkdownInline] {
        InlineParser(text).parse()
    }

    /// Tabs to spaces, with tab stops of 4, for indentation arithmetic.
    static func expandTabs(_ line: String) -> String {
        guard line.contains("\t") else { return line }
        var out = ""
        var column = 0
        for character in line {
            if character == "\t" {
                let spaces = 4 - column % 4
                out += String(repeating: " ", count: spaces)
                column += spaces
            } else {
                out.append(character)
                column += 1
            }
        }
        return out
    }

    /// The text of inlines without formatting (alt text, search, accessibility).
    static func plainText(_ inlines: [MarkdownInline]) -> String {
        inlines.map { inline -> String in
            switch inline {
            case .text(let text), .code(let text): return text
            case .emphasis(let inner), .strong(let inner), .strikethrough(let inner): return plainText(inner)
            case .link(let label, _): return plainText(label)
            case .image(let alt, _): return alt
            case .softBreak: return "\n"
            case .hardBreak: return "\n"
            }
        }.joined()
    }

    /// Links a click may open: web and mail only.
    static func isOpenable(_ destination: String) -> Bool {
        guard let scheme = URL(string: destination)?.scheme?.lowercased() else { return false }
        return ["http", "https", "mailto"].contains(scheme)
    }

    /// Inlines as an AttributedString with Foundation's presentation intents
    /// (SwiftUI's Text renders them). Soft breaks are shown as line breaks, as
    /// chat transcripts are read.
    static func attributed(_ inlines: [MarkdownInline]) -> AttributedString {
        var out = AttributedString()
        func add(_ inlines: [MarkdownInline], intent: InlinePresentationIntent, link: URL?) {
            for inline in inlines {
                switch inline {
                case .text(let text):
                    var piece = AttributedString(text)
                    if !intent.isEmpty { piece.inlinePresentationIntent = intent }
                    if let link { piece.link = link }
                    out += piece
                case .code(let text):
                    var piece = AttributedString(text)
                    piece.inlinePresentationIntent = intent.union(.code)
                    if let link { piece.link = link }
                    out += piece
                case .emphasis(let inner): add(inner, intent: intent.union(.emphasized), link: link)
                case .strong(let inner): add(inner, intent: intent.union(.stronglyEmphasized), link: link)
                case .strikethrough(let inner): add(inner, intent: intent.union(.strikethrough), link: link)
                case .link(let label, let destination):
                    add(label, intent: intent, link: isOpenable(destination) ? URL(string: destination) : link)
                case .image(let alt, let source):
                    add([.text(alt.isEmpty ? source : alt)], intent: intent, link: isOpenable(source) ? URL(string: source) : link)
                case .softBreak, .hardBreak:
                    out += AttributedString("\n")
                }
            }
        }
        add(inlines, intent: [], link: nil)
        return out
    }
}

/// C-29.8: long messages and code blocks show a bounded part first, with the
/// rest behind an expand control.
enum MarkdownBounds {
    static let codeLines = 40
    static let blocks = 200

    static func code(_ text: String, maxLines: Int = codeLines) -> (shown: String, hiddenLines: Int) {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard lines.count > maxLines else { return (text, 0) }
        return (lines.prefix(maxLines).joined(separator: "\n"), lines.count - maxLines)
    }

    static func blocks(_ blocks: [MarkdownBlock], max: Int = MarkdownBounds.blocks) -> (shown: [MarkdownBlock], hidden: Int) {
        guard blocks.count > max else { return (blocks, 0) }
        return (Array(blocks.prefix(max)), blocks.count - max)
    }
}

// MARK: - Blocks

private func leadingSpaces(_ line: String) -> Int {
    var count = 0
    for character in line {
        if character == " " { count += 1 } else { break }
    }
    return count
}

private func isBlank(_ line: String) -> Bool { line.allSatisfy { $0 == " " } }

private func dropLeading(_ line: String, _ count: Int) -> String {
    var dropped = 0
    var index = line.startIndex
    while dropped < count, index < line.endIndex, line[index] == " " {
        index = line.index(after: index)
        dropped += 1
    }
    return String(line[index...])
}

private struct ListMarker {
    var ordered: Bool
    var number: Int
    /// `-`, `+`, `*`, or the ordered delimiter `.` / `)`.
    var symbol: Character
    var indent: Int
    var contentOffset: Int
    var content: String
}

private struct BlockParser {
    let lines: [String]

    func parse() -> [MarkdownBlock] { parseBlocks(lines) }

    func parseBlocks(_ lines: [String]) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var i = 0

        func flush() {
            guard !paragraph.isEmpty else { return }
            let text = paragraph.map { dropLeading($0, 3) }.joined(separator: "\n")
            blocks.append(.paragraph(Markdown.parseInlines(text.trimmingCharacters(in: .whitespaces))))
            paragraph = []
        }

        while i < lines.count {
            let line = lines[i]
            if isBlank(line) {
                flush()
                i += 1
                continue
            }
            let indent = leadingSpaces(line)
            let stripped = dropLeading(line, 3)

            if indent < 4, let fence = openingFence(stripped) {
                flush()
                let fenceIndent = min(indent, 3)
                var body: [String] = []
                var closed = false
                i += 1
                while i < lines.count {
                    let candidate = dropLeading(lines[i], 3)
                    if leadingSpaces(lines[i]) < 4, closesFence(candidate, fence.character, fence.length) {
                        closed = true
                        i += 1
                        break
                    }
                    body.append(dropLeading(lines[i], fenceIndent))
                    i += 1
                }
                blocks.append(.code(language: fence.language, text: body.joined(separator: "\n"), closed: closed))
                continue
            }
            if indent >= 4 && paragraph.isEmpty {
                var body: [String] = []
                while i < lines.count, isBlank(lines[i]) || leadingSpaces(lines[i]) >= 4 {
                    body.append(isBlank(lines[i]) ? "" : dropLeading(lines[i], 4))
                    i += 1
                }
                while body.last == "" { body.removeLast() }
                blocks.append(.code(language: nil, text: body.joined(separator: "\n"), closed: true))
                continue
            }
            if indent < 4, let heading = atxHeading(stripped) {
                flush()
                blocks.append(.heading(level: heading.level, content: Markdown.parseInlines(heading.text)))
                i += 1
                continue
            }
            if indent < 4, !paragraph.isEmpty, let level = setextLevel(stripped) {
                let text = paragraph.map { dropLeading($0, 3) }.joined(separator: "\n").trimmingCharacters(in: .whitespaces)
                blocks.append(.heading(level: level, content: Markdown.parseInlines(text)))
                paragraph = []
                i += 1
                continue
            }
            if indent < 4, isThematicBreak(stripped) {
                flush()
                blocks.append(.rule)
                i += 1
                continue
            }
            if indent < 4, i + 1 < lines.count, line.contains("|"), let alignments = delimiterRow(lines[i + 1]),
               tableCells(line).count == alignments.count {
                flush()
                let header = tableCells(line).map(Markdown.parseInlines)
                var rows: [[[MarkdownInline]]] = []
                i += 2
                while i < lines.count, !isBlank(lines[i]), !startsOtherBlock(lines[i]) {
                    var cells = tableCells(lines[i]).map(Markdown.parseInlines)
                    if cells.count < alignments.count {
                        cells += Array(repeating: [], count: alignments.count - cells.count)
                    }
                    rows.append(Array(cells.prefix(alignments.count)))
                    i += 1
                }
                blocks.append(.table(header: header, alignments: alignments, rows: rows))
                continue
            }
            if indent < 4, stripped.hasPrefix(">") {
                flush()
                var inner: [String] = []
                var lastWasText = false
                while i < lines.count {
                    let current = lines[i]
                    let body = dropLeading(current, 3)
                    if leadingSpaces(current) < 4, body.hasPrefix(">") {
                        var rest = String(body.dropFirst())
                        if rest.hasPrefix(" ") { rest.removeFirst() }
                        inner.append(rest)
                        lastWasText = !isBlank(rest)
                        i += 1
                    } else if lastWasText, !isBlank(current), !startsOtherBlock(current) {
                        inner.append(current)   // lazy continuation of the quoted paragraph
                        i += 1
                    } else {
                        break
                    }
                }
                blocks.append(.quote(parseBlocks(inner)))
                continue
            }
            if indent < 4, let marker = listMarker(line),
               paragraph.isEmpty || (!isBlank(marker.content) && (!marker.ordered || marker.number == 1)) {
                flush()
                let (block, next) = parseList(lines, from: i)
                blocks.append(block)
                i = next
                continue
            }
            paragraph.append(line)
            i += 1
        }
        flush()
        return blocks
    }

    func parseList(_ lines: [String], from start: Int) -> (MarkdownBlock, Int) {
        guard let first = listMarker(lines[start]) else { return (.paragraph([]), start + 1) }
        var items: [MarkdownListItem] = []
        var tight = true
        var i = start
        while i < lines.count, let marker = listMarker(lines[i]), marker.ordered == first.ordered,
              marker.symbol == first.symbol, marker.indent < first.contentOffset {
            var body = [marker.content]
            var sawBlank = false
            i += 1
            while i < lines.count {
                let line = lines[i]
                if isBlank(line) {
                    body.append("")
                    sawBlank = true
                    i += 1
                    continue
                }
                let indent = leadingSpaces(line)
                if indent >= marker.contentOffset {
                    body.append(dropLeading(line, marker.contentOffset))
                    sawBlank = false
                    i += 1
                    continue
                }
                if sawBlank || listMarker(line) != nil || startsOtherBlock(line) { break }
                body.append(dropLeading(line, 3))       // lazy continuation
                i += 1
            }
            while body.last == "" { body.removeLast() }
            if body.contains("") { tight = false }
            if sawBlank, i < lines.count, let next = listMarker(lines[i]), next.ordered == first.ordered,
               next.symbol == first.symbol, next.indent < first.contentOffset {
                tight = false
            }
            var checked: Bool?
            if let head = body.first, let task = taskBox(head) {
                checked = task.checked
                body[0] = task.rest
            }
            items.append(MarkdownListItem(blocks: parseBlocks(body), checked: checked))
        }
        return (.list(ordered: first.ordered, start: first.number, tight: tight, items: items), i)
    }

    // MARK: Line tests

    func openingFence(_ line: String) -> (character: Character, length: Int, language: String?)? {
        guard let character = line.first, character == "`" || character == "~" else { return nil }
        let length = line.prefix { $0 == character }.count
        guard length >= 3 else { return nil }
        let info = line.dropFirst(length).trimmingCharacters(in: .whitespaces)
        if character == "`" && info.contains("`") { return nil }
        let language = info.split(separator: " ").first.map(String.init)
        return (character, length, language?.isEmpty == false ? language : nil)
    }

    func closesFence(_ line: String, _ character: Character, _ length: Int) -> Bool {
        let run = line.prefix { $0 == character }.count
        return run >= length && line.dropFirst(run).allSatisfy { $0 == " " }
    }

    func atxHeading(_ line: String) -> (level: Int, text: String)? {
        let hashes = line.prefix { $0 == "#" }.count
        guard (1...6).contains(hashes) else { return nil }
        let rest = line.dropFirst(hashes)
        guard rest.isEmpty || rest.first == " " else { return nil }
        var text = rest.trimmingCharacters(in: .whitespaces)
        // An optional closing run of #s preceded by a space.
        if let range = text.range(of: #"(^|\s)#+\s*$"#, options: .regularExpression) {
            text = String(text[..<range.lowerBound]).trimmingCharacters(in: .whitespaces)
        }
        return (hashes, text)
    }

    func setextLevel(_ line: String) -> Int? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        if trimmed.allSatisfy({ $0 == "=" }) { return 1 }
        if trimmed.allSatisfy({ $0 == "-" }) { return 2 }
        return nil
    }

    func isThematicBreak(_ line: String) -> Bool {
        let characters = line.filter { $0 != " " }
        guard characters.count >= 3, let first = characters.first, "-*_".contains(first) else { return false }
        return characters.allSatisfy { $0 == first }
    }

    func listMarker(_ line: String) -> ListMarker? {
        let indent = leadingSpaces(line)
        let body = Array(line.dropFirst(indent))
        guard !body.isEmpty else { return nil }
        var ordered = false
        var number = 1
        var symbol: Character
        var width: Int
        if "-+*".contains(body[0]) {
            symbol = body[0]
            width = 1
        } else {
            let digits = body.prefix { $0.isASCII && $0.isNumber }
            guard (1...9).contains(digits.count), digits.count < body.count, body[digits.count] == "." || body[digits.count] == ")" else {
                return nil
            }
            ordered = true
            number = Int(String(digits)) ?? 1
            symbol = body[digits.count]
            width = digits.count + 1
        }
        let after = body.dropFirst(width)
        if after.isEmpty {
            return ListMarker(ordered: ordered, number: number, symbol: symbol, indent: indent,
                              contentOffset: indent + width + 1, content: "")
        }
        guard after.first == " " else { return nil }
        let spaces = after.prefix { $0 == " " }.count
        let content = String(after.dropFirst(spaces))
        if content.isEmpty {
            return ListMarker(ordered: ordered, number: number, symbol: symbol, indent: indent,
                              contentOffset: indent + width + 1, content: "")
        }
        // Five or more spaces: the item starts with indented code; its offset is one space.
        let padding = spaces >= 5 ? 1 : spaces
        let text = spaces >= 5 ? String(after.dropFirst(1)) : content
        // A thematic break is not a list item ("- - -", "* * *").
        if !ordered && isThematicBreak(String(body)) { return nil }
        return ListMarker(ordered: ordered, number: number, symbol: symbol, indent: indent,
                          contentOffset: indent + width + padding, content: text)
    }

    func taskBox(_ line: String) -> (checked: Bool, rest: String)? {
        let characters = Array(line)
        guard characters.count >= 4, characters[0] == "[", characters[2] == "]", characters[3] == " " else { return nil }
        switch characters[1] {
        case " ": return (false, String(characters.dropFirst(4)))
        case "x", "X": return (true, String(characters.dropFirst(4)))
        default: return nil
        }
    }

    func startsOtherBlock(_ line: String) -> Bool {
        guard leadingSpaces(line) < 4 else { return false }
        let stripped = dropLeading(line, 3)
        return openingFence(stripped) != nil || atxHeading(stripped) != nil || stripped.hasPrefix(">")
            || isThematicBreak(stripped)
    }

    func delimiterRow(_ line: String) -> [MarkdownAlignment]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.contains("-"), leadingSpaces(line) < 4 else { return nil }
        let cells = tableCells(trimmed)
        guard !cells.isEmpty, trimmed.contains("|") || cells.count > 1 else { return nil }
        var alignments: [MarkdownAlignment] = []
        for cell in cells {
            let c = cell.trimmingCharacters(in: .whitespaces)
            let left = c.hasPrefix(":"), right = c.hasSuffix(":")
            let dashes = c.dropFirst(left ? 1 : 0).dropLast(right && c.count > 1 ? 1 : 0)
            guard !dashes.isEmpty, dashes.allSatisfy({ $0 == "-" }) else { return nil }
            alignments.append(left && right ? .center : left ? .left : right ? .right : .none)
        }
        return alignments
    }

    /// Cells of a table row: outer pipes dropped, split on unescaped pipes.
    func tableCells(_ line: String) -> [String] {
        var text = line.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("|") { text.removeFirst() }
        if text.hasSuffix("|") && !text.hasSuffix("\\|") { text.removeLast() }
        var cells: [String] = []
        var current = ""
        var escaped = false
        var inCode = 0
        for character in text {
            if escaped {
                if character != "|" { current.append("\\") }
                current.append(character)
                escaped = false
            } else if character == "\\" {
                escaped = true
            } else if character == "`" {
                inCode = inCode == 0 ? 1 : 0
                current.append(character)
            } else if character == "|" && inCode == 0 {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
            } else {
                current.append(character)
            }
        }
        if escaped { current.append("\\") }
        cells.append(current.trimmingCharacters(in: .whitespaces))
        return cells
    }
}

// MARK: - Inlines

private let asciiPunctuation = Set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

private func isPunctuation(_ character: Character?) -> Bool {
    guard let character else { return false }
    return asciiPunctuation.contains(character) || character.isPunctuation || character.isSymbol
}

private func isWhitespace(_ character: Character?) -> Bool {
    guard let character else { return true }
    return character.isWhitespace
}

private struct Delimiter: Equatable {
    var character: Character
    var count: Int
    var original: Int
    var canOpen: Bool
    var canClose: Bool
}

private enum InlineToken: Equatable {
    case text(String)
    case node(MarkdownInline)
    case delimiter(Delimiter)
}

private struct InlineParser {
    let characters: [Character]

    init(_ text: String) {
        characters = Array(text)
    }

    func parse() -> [MarkdownInline] {
        var tokens: [InlineToken] = []
        var text = ""
        var i = 0

        func flushText() {
            if !text.isEmpty {
                tokens.append(.text(decodeEntities(text)))
                text = ""
            }
        }

        while i < characters.count {
            let character = characters[i]
            switch character {
            case "\\":
                if i + 1 < characters.count, characters[i + 1] == "\n" {
                    flushText()
                    tokens.append(.node(.hardBreak))
                    i += 2
                } else if i + 1 < characters.count, asciiPunctuation.contains(characters[i + 1]) {
                    text.append(characters[i + 1])
                    i += 2
                } else {
                    text.append(character)
                    i += 1
                }
            case "\n":
                var hard = false
                if text.hasSuffix("  ") { hard = true }
                while text.hasSuffix(" ") { text.removeLast() }
                flushText()
                tokens.append(.node(hard ? .hardBreak : .softBreak))
                i += 1
                while i < characters.count, characters[i] == " " { i += 1 }
            case "`":
                let run = count(of: "`", at: i)
                if let close = findBacktickRun(length: run, from: i + run) {
                    flushText()
                    var code = String(characters[(i + run)..<close]).replacingOccurrences(of: "\n", with: " ")
                    if code.count >= 2, code.hasPrefix(" "), code.hasSuffix(" "), !code.allSatisfy({ $0 == " " }) {
                        code = String(code.dropFirst().dropLast())
                    }
                    tokens.append(.node(.code(code)))
                    i = close + run
                } else {
                    text += String(repeating: "`", count: run)
                    i += run
                }
            case "!" where i + 1 < characters.count && characters[i + 1] == "[":
                if let (label, destination, end) = linkAt(i + 1) {
                    flushText()
                    tokens.append(.node(.image(alt: Markdown.plainText(InlineParser(label).parse()), source: destination)))
                    i = end
                } else {
                    text.append(character)
                    i += 1
                }
            case "[":
                if let (label, destination, end) = linkAt(i) {
                    flushText()
                    tokens.append(.node(.link(InlineParser(label).parse(), destination: destination)))
                    i = end
                } else {
                    text.append(character)
                    i += 1
                }
            case "<":
                if let (destination, end) = autolinkAt(i) {
                    flushText()
                    let label = destination.hasPrefix("mailto:") ? String(destination.dropFirst(7)) : destination
                    tokens.append(.node(.link([.text(label)], destination: destination)))
                    i = end
                } else {
                    text.append(character)
                    i += 1
                }
            case "*", "_", "~":
                let run = count(of: character, at: i)
                let before = i > 0 ? characters[i - 1] : nil
                let after = i + run < characters.count ? characters[i + run] : nil
                let left = !isWhitespace(after) && (!isPunctuation(after) || isWhitespace(before) || isPunctuation(before))
                let right = !isWhitespace(before) && (!isPunctuation(before) || isWhitespace(after) || isPunctuation(after))
                var canOpen = left, canClose = right
                if character == "_" {
                    canOpen = left && (!right || isPunctuation(before))
                    canClose = right && (!left || isPunctuation(after))
                }
                if character == "~" && run > 2 {
                    text += String(repeating: "~", count: run)
                } else {
                    flushText()
                    tokens.append(.delimiter(Delimiter(character: character, count: run, original: run,
                                                       canOpen: canOpen, canClose: canClose)))
                }
                i += run
            default:
                if "hHwW".contains(character), let (destination, label, end) = bareURLAt(i) {
                    flushText()
                    tokens.append(.node(.link([.text(label)], destination: destination)))
                    i = end
                } else {
                    text.append(character)
                    i += 1
                }
            }
        }
        flushText()
        return InlineParser.resolve(tokens)
    }

    private func count(of character: Character, at index: Int) -> Int {
        var end = index
        while end < characters.count, characters[end] == character { end += 1 }
        return end - index
    }

    private func findBacktickRun(length: Int, from start: Int) -> Int? {
        var i = start
        while i < characters.count {
            if characters[i] == "`" {
                let run = count(of: "`", at: i)
                if run == length { return i }
                i += run
            } else {
                i += 1
            }
        }
        return nil
    }

    /// `[label](destination "title")` starting at the `[`.
    private func linkAt(_ open: Int) -> (String, String, Int)? {
        var depth = 0
        var i = open
        var close: Int?
        while i < characters.count {
            let character = characters[i]
            if character == "\\" { i += 2; continue }
            if character == "`" {
                let run = count(of: "`", at: i)
                if let end = findBacktickRun(length: run, from: i + run) { i = end + run; continue }
                i += run
                continue
            }
            if character == "[" { depth += 1 }
            if character == "]" {
                depth -= 1
                if depth == 0 { close = i; break }
            }
            i += 1
        }
        guard let close, close + 1 < characters.count, characters[close + 1] == "(" else { return nil }
        var j = close + 2
        while j < characters.count, characters[j] == " " { j += 1 }
        var destination = ""
        if j < characters.count, characters[j] == "<" {
            j += 1
            while j < characters.count, characters[j] != ">", characters[j] != "\n" { destination.append(characters[j]); j += 1 }
            guard j < characters.count, characters[j] == ">" else { return nil }
            j += 1
        } else {
            var parens = 0
            while j < characters.count {
                let character = characters[j]
                if character == " " || character == "\n" { break }
                if character == "(" { parens += 1 }
                if character == ")" {
                    if parens == 0 { break }
                    parens -= 1
                }
                if character == "\\", j + 1 < characters.count, asciiPunctuation.contains(characters[j + 1]) {
                    destination.append(characters[j + 1])
                    j += 2
                    continue
                }
                destination.append(character)
                j += 1
            }
        }
        while j < characters.count, characters[j] == " " || characters[j] == "\n" { j += 1 }
        if j < characters.count, "\"'(".contains(characters[j]) {
            let closer: Character = characters[j] == "(" ? ")" : characters[j]
            j += 1
            while j < characters.count, characters[j] != closer { j += 1 }
            guard j < characters.count else { return nil }
            j += 1
            while j < characters.count, characters[j] == " " { j += 1 }
        }
        guard j < characters.count, characters[j] == ")" else { return nil }
        return (String(characters[(open + 1)..<close]), destination, j + 1)
    }

    /// `<https://…>` or `<name@host>`.
    private func autolinkAt(_ open: Int) -> (String, Int)? {
        var i = open + 1
        var body = ""
        while i < characters.count, characters[i] != ">" {
            if characters[i] == " " || characters[i] == "<" || characters[i] == "\n" { return nil }
            body.append(characters[i])
            i += 1
        }
        guard i < characters.count else { return nil }
        if body.range(of: #"^[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*$"#, options: .regularExpression) != nil {
            return (body, i + 1)
        }
        if body.range(of: #"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"#,
                      options: .regularExpression) != nil {
            return ("mailto:" + body, i + 1)
        }
        return nil
    }

    /// GFM's extended autolinks: `https://…`, `http://…`, `www.…` at a word start.
    private func bareURLAt(_ start: Int) -> (String, String, Int)? {
        let previous = start > 0 ? characters[start - 1] : nil
        guard previous == nil || isWhitespace(previous) || "(*_~\"'".contains(previous!) else { return nil }
        let rest = String(characters[start..<min(characters.count, start + 8)]).lowercased()
        let prefix: String
        let lead: Int
        if rest.hasPrefix("https://") { prefix = ""; lead = 8 }
        else if rest.hasPrefix("http://") { prefix = ""; lead = 7 }
        else if rest.hasPrefix("www.") { prefix = "https://"; lead = 4 }
        else { return nil }
        var end = start
        while end < characters.count, !characters[end].isWhitespace, characters[end] != "<" { end += 1 }
        var text = String(characters[start..<end])
        while let last = text.last {
            if "?!.,:*_~'\"".contains(last) {
                text.removeLast()
            } else if last == ")" && text.filter({ $0 == ")" }).count > text.filter({ $0 == "(" }).count {
                text.removeLast()
            } else {
                break
            }
        }
        guard text.count > lead, text.dropFirst(lead).contains(where: { $0.isLetter || $0.isNumber }) else { return nil }
        return (prefix + text, text, start + text.count)
    }

    // MARK: Emphasis (CommonMark's delimiter procedure, simplified)

    static func resolve(_ input: [InlineToken]) -> [MarkdownInline] {
        // The opener search is quadratic in the worst case; a pathological run of
        // delimiters is shown literally rather than risk a slow render.
        guard input.count <= 4000 else { return flatten(input) }
        var tokens = input
        var closerIndex = 0
        while closerIndex < tokens.count {
            guard case .delimiter(var closer) = tokens[closerIndex], closer.canClose, closer.count > 0 else {
                closerIndex += 1
                continue
            }
            var openerIndex: Int?
            var j = closerIndex - 1
            while j >= 0 {
                if case .delimiter(let opener) = tokens[j], opener.character == closer.character, opener.canOpen,
                   opener.count > 0 {
                    if closer.character == "~" {
                        if opener.count == closer.count { openerIndex = j; break }
                    } else {
                        let oddMatch = (opener.canClose || closer.canOpen) && (opener.original + closer.original) % 3 == 0
                            && !(opener.original % 3 == 0 && closer.original % 3 == 0)
                        if !oddMatch { openerIndex = j; break }
                    }
                }
                j -= 1
            }
            guard let o = openerIndex, case .delimiter(var opener) = tokens[o] else {
                closerIndex += 1
                continue
            }
            let use = closer.character == "~" ? closer.count : (closer.count >= 2 && opener.count >= 2 ? 2 : 1)
            let inner = flatten(Array(tokens[(o + 1)..<closerIndex]))
            let node: MarkdownInline = closer.character == "~" ? .strikethrough(inner)
                : use == 2 ? .strong(inner) : .emphasis(inner)
            opener.count -= use
            closer.count -= use
            var replacement: [InlineToken] = []
            if opener.count > 0 { replacement.append(.delimiter(opener)) }
            replacement.append(.node(node))
            if closer.count > 0 { replacement.append(.delimiter(closer)) }
            tokens.replaceSubrange(o...closerIndex, with: replacement)
            closerIndex = o + replacement.count - (closer.count > 0 ? 1 : 0)
        }
        return flatten(tokens)
    }

    /// Tokens to inlines: unmatched delimiters become their characters, and
    /// adjacent text merges.
    static func flatten(_ tokens: [InlineToken]) -> [MarkdownInline] {
        var out: [MarkdownInline] = []
        func appendText(_ text: String) {
            guard !text.isEmpty else { return }
            if case .text(let previous) = out.last {
                out[out.count - 1] = .text(previous + text)
            } else {
                out.append(.text(text))
            }
        }
        for token in tokens {
            switch token {
            case .text(let text): appendText(text)
            case .delimiter(let delimiter): appendText(String(repeating: delimiter.character, count: delimiter.count))
            case .node(let node): out.append(node)
            }
        }
        return out
    }
}

private let namedEntities: [String: String] = [
    "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": "\u{00A0}", "mdash": "\u{2014}",
    "ndash": "\u{2013}", "hellip": "\u{2026}", "copy": "\u{00A9}",
]

/// `&amp;`, `&#39;`, `&#x27;` and a few common names; anything else stays as written.
private func decodeEntities(_ text: String) -> String {
    guard text.contains("&") else { return text }
    var out = ""
    var i = text.startIndex
    while i < text.endIndex {
        if text[i] == "&", let semicolon = text[i...].prefix(12).firstIndex(of: ";") {
            let name = String(text[text.index(after: i)..<semicolon])
            var replacement: String?
            if name.hasPrefix("#x") || name.hasPrefix("#X"), let code = UInt32(name.dropFirst(2), radix: 16) {
                replacement = Unicode.Scalar(code).map { String(Character($0)) }
            } else if name.hasPrefix("#"), let code = UInt32(name.dropFirst()) {
                replacement = Unicode.Scalar(code).map { String(Character($0)) }
            } else {
                replacement = namedEntities[name]
            }
            if let replacement {
                out += replacement
                i = text.index(after: semicolon)
                continue
            }
        }
        out.append(text[i])
        i = text.index(after: i)
    }
    return out
}
