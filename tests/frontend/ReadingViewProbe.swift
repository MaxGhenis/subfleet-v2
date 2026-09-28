// Host the real conversation views and the ⌘K palette without opening a window
// (C-29.12, C-29.13): sizes at each text scale, the palette's keys and layout.
import AppKit
import SwiftUI

@MainActor
func fitting<V: View>(_ view: V, width: CGFloat = 2000, height: CGFloat = 20000) -> CGSize {
    NSHostingController(rootView: view).sizeThatFits(in: CGSize(width: width, height: height))
}

@MainActor
func emit(_ value: Any) throws {
    print(String(data: try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), encoding: .utf8)!)
}

final class ScaleBox {
    var scale = TextScale.actual
}

final class Counter {
    var count = 0
}

@MainActor
func composerFontSize(scale: Double) -> Double {
    let host = NSHostingView(rootView: ComposerTextView(text: .constant("hello"), onSubmit: {}, onImage: { _ in })
        .environment(\.textScale, scale).frame(width: 400, height: 80))
    host.frame = NSRect(x: 0, y: 0, width: 400, height: 80)
    host.layoutSubtreeIfNeeded()
    return Double(host.firstDescendant(NSTextView.self)?.font?.pointSize ?? 0)
}

@MainActor
func sizesAtScales() -> [String: Any] {
    let paragraph = "A paragraph of body text long enough to measure."
    let entry = SidebarEntry(id: "cv:1", target: .conversation("1"), provider: "claude", title: "Title", subtitle: "~/code",
                             workspace: "/w", date: nil, pendingApprovals: 0, active: false, blockedBy: nil,
                             liveElsewhere: false, continuable: true, continueBlocker: nil)
    let result = SearchResult(entry: entry, tier: .message, snippet: SearchSnippet(itemID: "i", author: "You",
                                                                                  text: "a snippet", highlights: []))
    let chip = ServedChip(account: "a@b.c", model: "opus", effort: "high", fast: "off", warnings: [])
    let tool = ToolActivity(toolID: "t", name: "Bash", summary: "ls -la", hidden: false, state: .succeeded, preview: nil)
    var rows: [[String: Any]] = []
    let code = (1...100).map { "let line\($0) = \($0)" }.joined(separator: "\n")
    for scale in [TextScale.steps[0], 1.0, TextScale.steps[6], TextScale.steps[TextScale.steps.count - 1]] {
        func measure<V: View>(_ view: V) -> [Double] {
            let size = fitting(view.environment(\.textScale, scale).fixedSize())
            return [Double(size.width), Double(size.height)]
        }
        rows.append([
            "scale": scale,
            "paragraph": measure(MarkdownView(text: paragraph)),
            "heading": measure(MarkdownView(text: "# A heading")),
            "code": measure(MarkdownView(text: "```swift\nlet value = 42\n```")),
            "long_code": measure(CodeBlockView(language: "swift", text: code)),
            "bubble": measure(PersonBubble(text: paragraph, footer: nil)),
            "chip": measure(ServedChipView(chip: chip)),
            "tool": measure(ToolRow(activity: tool)),
            "banner": measure(StatusBanner(title: "Title", detail: "Detail words", symbol: "bolt")),
            "result_row": measure(SearchResultRow(result: result, selected: false)),
            "composer_font": composerFontSize(scale: scale),
        ])
    }
    return ["rows": rows, "system_body": [Double(fitting(Text(paragraph).font(.body).fixedSize()).width),
                                          Double(fitting(Text(paragraph).font(.body).fixedSize()).height)]]
}

/// ⌘= through the window's hidden button, as a key equivalent.
@MainActor
func equalsShortcut() -> [String: Any] {
    let box = ScaleBox()
    let binding = Binding(get: { box.scale }, set: { box.scale = $0 })
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 200, height: 100), styleMask: [.titled],
                          backing: .buffered, defer: true)
    window.isReleasedWhenClosed = false
    window.contentView = NSHostingView(rootView: TextScaleEqualsShortcut(scale: binding).frame(width: 200, height: 100))
    window.contentView?.layoutSubtreeIfNeeded()
    func press(_ characters: String) -> Bool {
        guard let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command, timestamp: 0,
                                           windowNumber: window.windowNumber, context: nil, characters: characters,
                                           charactersIgnoringModifiers: characters, isARepeat: false,
                                           keyCode: characters == "=" ? 24 : 27) else { return false }
        return window.performKeyEquivalent(with: event)
    }
    var out: [String: Any] = [:]
    out["handled"] = press("=")
    out["after_equals"] = box.scale
    out["handled_minus"] = press("-")
    out["after_minus"] = box.scale
    box.scale = TextScale.range.upperBound
    window.contentView?.layoutSubtreeIfNeeded()
    _ = press("=")
    out["at_largest"] = box.scale
    window.close()
    return out
}

@MainActor
func palette() -> [String: Any] {
    func candidate(_ id: String, _ title: String, date: Double, messages: [SearchMessage] = []) -> SearchCandidate {
        SearchCandidate(entry: SidebarEntry(id: "cv:" + id, target: .conversation(id), provider: "claude", title: title,
                                            subtitle: "~/code/app", workspace: "/w", date: Date(timeIntervalSince1970: date),
                                            pendingApprovals: 0, active: false, blockedBy: nil, liveElsewhere: false,
                                            continuable: true, continueBlocker: nil),
                        messages: messages)
    }
    let index = SearchIndex([
        candidate("a", "Deploy", date: 3),
        candidate("b", "Deploy notes", date: 2),
        candidate("c", "Other", date: 1, messages: [SearchMessage(itemID: "person:m1", author: "You", text: "please deploy")]),
    ])
    let model = SearchPaletteModel()
    model.isPresented = true
    model.show(index.search("deploy")!)
    var out: [String: Any] = ["first": model.selection as Any? ?? NSNull()]
    model.move(1)
    out["down"] = model.selection as Any? ?? NSNull()
    model.move(-5)
    out["up_past_top"] = model.selection as Any? ?? NSNull()
    model.move(100)
    out["down_past_end"] = model.selection as Any? ?? NSNull()
    model.move(-1)
    out["back_up"] = model.selection as Any? ?? NSNull()
    model.activate()
    out["closed"] = !model.isPresented
    out["reveal"] = model.reveal.map { [$0.conversationID, $0.itemID] } as Any? ?? NSNull()

    // The new-conversation item: selected when nothing matches, and Return opens the sheet.
    let posted = Counter()
    let observer = NotificationCenter.default.addObserver(forName: .subfleetNewConversation, object: nil, queue: nil) { _ in
        posted.count += 1
    }
    let empty = SearchPaletteModel()
    empty.isPresented = true
    empty.show(index.search("zebra")!)
    out["nothing_matches_selection"] = empty.selection as Any? ?? NSNull()
    empty.activate()
    out["new_conversation_posted"] = posted.count
    out["closed_after_new"] = !empty.isPresented
    NotificationCenter.default.removeObserver(observer)

    // The field's keys.
    var log: [String] = []
    let keys: [(String, Selector)] = [
        ("down", #selector(NSResponder.moveDown(_:))), ("up", #selector(NSResponder.moveUp(_:))),
        ("page-down", #selector(NSResponder.pageDown(_:))), ("scroll-page-up", #selector(NSResponder.scrollPageUp(_:))),
        ("return", #selector(NSResponder.insertNewline(_:))), ("escape", #selector(NSResponder.cancelOperation(_:))),
        ("tab", #selector(NSResponder.insertTab(_:))), ("left", #selector(NSResponder.moveLeft(_:))),
    ]
    var typed = ""
    let field = PaletteSearchField(text: Binding(get: { typed }, set: { typed = $0 }), placeholder: "Search",
                                   font: .systemFont(ofSize: 15), onMove: { log.append("move \($0)") },
                                   onSubmit: { log.append("submit") }, onCancel: { log.append("cancel") })
    let coordinator = field.makeCoordinator()
    var handled: [String: Bool] = [:]
    for (name, selector) in keys {
        handled[name] = coordinator.control(NSTextField(), textView: NSTextView(), doCommandBy: selector)
    }
    let source = NSTextField()
    source.stringValue = "café"
    coordinator.controlTextDidChange(Notification(name: NSControl.textDidChangeNotification, object: source))
    out["keys"] = handled
    out["log"] = log
    out["typed"] = typed

    // Layout: few results, many, and many at twice the size.
    func paletteSize(_ outcome: SearchOutcome, scale: Double, room: CGFloat = 20000) -> [Double] {
        let palette = SearchPaletteModel()
        palette.isPresented = true
        palette.show(outcome)
        let size = fitting(SearchPaletteView(palette: palette).environment(\.textScale, scale).frame(width: 640),
                           width: 640, height: room)
        return [Double(size.width), Double(size.height)]
    }
    let many = SearchIndex((0..<60).map { candidate("n\($0)", "Task \($0)", date: Double($0)) })
    out["few"] = paletteSize(index.search("deploy")!, scale: 1)
    out["many"] = paletteSize(many.search("task")!, scale: 1)
    out["many_large"] = paletteSize(many.search("task")!, scale: 2)
    // A short window: the list takes the room there is, not its full height.
    out["many_large_short_window"] = paletteSize(many.search("task")!, scale: 2, room: 460)
    out["few_short_window"] = paletteSize(index.search("deploy")!, scale: 1, room: 460)
    out["empty"] = paletteSize(index.search("zebra")!, scale: 1)

    let text = "Résumé polish"
    let attributed = highlightedText(text, SearchHighlight.ranges(of: [SearchFold.fold("resume").bytes], in: text))
    out["runs"] = attributed.runs.map { run -> [Any] in
        [String(attributed[run.range].characters), run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true]
    }
    return out
}

@main
struct ReadingViewProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var out: [String: Any]
        switch CommandLine.arguments[1] {
        case "sizes": out = sizesAtScales()
        case "equals": out = equalsShortcut()
        case "palette": out = palette()
        default: exit(2)
        }
        // A layout test must never leave a window on screen.
        out["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        try emit(out)
    }
}
