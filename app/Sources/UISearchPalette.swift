// Subfleet: the ⌘K palette (C-29.12), a search field over the main window that
// finds conversations and sessions. SearchPalette.swift matches and ranks; this
// file keeps the palette's state, asks the daemon's catalog for sessions past
// the loaded page, and draws the field, the results and the keys.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

/// A message to scroll to once its conversation shows.
struct SearchReveal: Equatable {
    var conversationID: String
    var itemID: String
    /// Choosing the same message again scrolls again.
    var token = UUID()
}

/// Tells a search running off the main thread that a newer one replaced it.
final class SearchCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }
}

@MainActor
final class SearchPaletteModel: ObservableObject {
    /// Results listed at most; the footer counts the rest.
    nonisolated static let resultLimit = 50
    /// Sessions asked of the daemon's catalog per query.
    nonisolated static let sessionLimit = 100
    /// How long typing pauses before the daemon is asked.
    nonisolated static let sessionDelay: UInt64 = 300_000_000
    /// The palette's own item after the results: start a new conversation.
    nonisolated static let newConversationID = "action:new-conversation"

    @Published var isPresented = false
    @Published var query = "" {
        didSet { if query != oldValue { queryChanged() } }
    }
    @Published private(set) var outcome = SearchOutcome.empty
    @Published var selection: String?
    /// An index is being built or a search runs.
    @Published private(set) var searching = false
    /// Whether an index exists yet for this opening.
    @Published private(set) var ready = false
    /// The daemon's catalog answered for the word it was asked (`sessionsWord`),
    /// and listed as many sessions as it was asked for, so more may match.
    @Published private(set) var sessionsWord: String?
    @Published private(set) var sessionsCapped = false
    /// A message the conversation view scrolls to (SearchRevealer).
    @Published var reveal: SearchReveal?

    weak var model: UIModel?
    /// The window the field is in: focus goes back there when the palette closes.
    weak var window: NSWindow?
    private weak var previousResponder: NSResponder?
    private var index: SearchIndex?
    private var sessions: [CatalogItem] = []
    private let cache = SearchFoldCache()
    private var building: SearchCancellation?
    private var running: SearchCancellation?
    private var sessionsTask: Task<Void, Never>?

    var results: [SearchResult] { outcome.results }
    var selectedResult: SearchResult? { results.first { $0.id == selection } }
    /// What Up and Down move through: the results, then the new-conversation item.
    var itemIDs: [String] { results.map(\.id) + [SearchPaletteModel.newConversationID] }

    func toggle(_ model: UIModel) {
        if isPresented { close() } else { present(model) }
    }

    /// Opens over the window with an empty query (the recent list), searching
    /// what the model holds now.
    func present(_ model: UIModel) {
        self.model = model
        previousResponder = SearchPaletteModel.editingResponder(NSApp.keyWindow?.firstResponder)
        sessions = []
        sessionsWord = nil
        sessionsCapped = false
        query = ""
        outcome = .empty
        selection = nil
        ready = false
        reveal = nil
        isPresented = true
        rebuild(model.state.searchCandidates())
    }

    func close(restoringFocus: Bool = true) {
        guard isPresented else { return }
        isPresented = false
        building?.cancel()
        running?.cancel()
        sessionsTask?.cancel()
        index = nil
        searching = false
        let responder = previousResponder
        previousResponder = nil
        guard restoringFocus, let window, let responder else { return }
        // After SwiftUI has taken the field away.
        DispatchQueue.main.async {
            if (responder as? NSView)?.window === window { window.makeFirstResponder(responder) }
        }
    }

    /// Moves the selection, stopping at the ends.
    func move(_ delta: Int) {
        let ids = itemIDs
        let current = ids.firstIndex { $0 == selection } ?? (delta > 0 ? -1 : ids.count)
        selection = ids[min(max(current + delta, 0), ids.count - 1)]
    }

    /// Opens a result (the selected one by default): its conversation or
    /// session, scrolled to the message it matched, with the composer focused.
    /// With the new-conversation item selected, starts a conversation instead.
    func activate(_ result: SearchResult? = nil) {
        if result == nil && selection == SearchPaletteModel.newConversationID {
            startConversation()
            return
        }
        guard let result = result ?? selectedResult else { return }
        close(restoringFocus: false)
        model?.select(result.entry)
        if case .conversation(let id) = result.entry.target, let item = result.snippet?.itemID {
            reveal = SearchReveal(conversationID: id, itemID: item)
        }
        focusComposer()
    }

    /// The new-conversation sheet, as File > New conversation opens it.
    func startConversation() {
        close(restoringFocus: false)
        NotificationCenter.default.post(name: .subfleetNewConversation, object: nil)
    }

    /// Shows an answer; the probes call it directly.
    func show(_ outcome: SearchOutcome, keepSelection: Bool = false) {
        self.outcome = outcome
        if !(keepSelection && selection.map(itemIDs.contains) == true) {
            selection = outcome.results.first?.id ?? SearchPaletteModel.newConversationID
        }
    }

    // MARK: Searching

    private func rebuild(_ candidates: [SearchCandidate]) {
        building?.cancel()
        let token = SearchCancellation()
        building = token
        searching = true
        let cache = self.cache
        Task.detached(priority: .userInitiated) { [weak self] in
            let index = SearchIndex(candidates, cache: cache)
            await self?.install(index, token: token)
        }
    }

    private func install(_ index: SearchIndex, token: SearchCancellation) {
        guard !token.isCancelled, isPresented else { return }
        self.index = index
        ready = true
        search(keepSelection: true)
    }

    private func search(keepSelection: Bool) {
        guard let index else { return }
        running?.cancel()
        let token = SearchCancellation()
        running = token
        searching = true
        let query = self.query
        let limit = SearchPaletteModel.resultLimit
        Task.detached(priority: .userInitiated) { [weak self] in
            guard let outcome = index.search(query, limit: limit, isCancelled: { token.isCancelled }) else { return }
            await self?.deliver(outcome, token: token, keepSelection: keepSelection)
        }
    }

    private func deliver(_ outcome: SearchOutcome, token: SearchCancellation, keepSelection: Bool) {
        guard !token.isCancelled, isPresented else { return }
        searching = false
        show(outcome, keepSelection: keepSelection)
    }

    private func queryChanged() {
        guard isPresented else { return }
        search(keepSelection: false)
        askSessions()
    }

    /// The sidebar holds the catalog's newest sessions only; after a pause in
    /// typing the daemon is asked for sessions anywhere in its catalog whose
    /// title, folder or first prompt hold the query's longest word (it matches
    /// one string; the index checks the other words). Found ones join the index.
    private func askSessions() {
        sessionsTask?.cancel()
        let words = query.split(whereSeparator: \.isWhitespace).map(String.init)
        guard let word = words.max(by: { $0.count < $1.count })?.lowercased(), word.count >= 2 else {
            // Sessions found for an earlier query leave with it, so the recent
            // list is the sidebar's again.
            if !sessions.isEmpty, let model {
                sessions = []
                sessionsWord = nil
                sessionsCapped = false
                rebuild(model.state.searchCandidates())
            }
            return
        }
        guard word != sessionsWord else { return }
        sessionsTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: SearchPaletteModel.sessionDelay)
            guard !Task.isCancelled, let model = self?.model,
                  let found = await model.searchSessions(word, limit: SearchPaletteModel.sessionLimit),
                  !Task.isCancelled, let self, self.isPresented else { return }
            self.sessionsWord = word
            self.sessionsCapped = found.count >= SearchPaletteModel.sessionLimit
            guard found != self.sessions else { return }
            self.sessions = found
            self.rebuild(model.state.searchCandidates(sessions: found))
        }
    }

    // MARK: Focus

    /// A text field's editor stands in for the field while it is edited.
    private static func editingResponder(_ responder: NSResponder?) -> NSResponder? {
        if let editor = responder as? NSTextView, editor.isFieldEditor, let field = editor.delegate as? NSResponder {
            return field
        }
        return responder
    }

    /// The composer takes the keys once the chosen conversation shows, unless
    /// the person has put them somewhere else meanwhile.
    private func focusComposer() {
        guard let window else { return }
        Task { @MainActor [weak window] in
            for delay: UInt64 in [100_000_000, 300_000_000, 800_000_000] {
                try? await Task.sleep(nanoseconds: delay)
                guard let window, window.isVisible else { return }
                guard window.firstResponder == nil || window.firstResponder === window else { return }
                if let composer = window.contentView?.firstDescendant(ComposerNSTextView.self) {
                    window.makeFirstResponder(composer)
                    return
                }
            }
        }
    }
}

extension NSView {
    /// The first view of `type` in this view's subtree, breadth first.
    func firstDescendant<T: NSView>(_ type: T.Type) -> T? {
        var queue: [NSView] = [self]
        while !queue.isEmpty {
            let view = queue.removeFirst()
            if let match = view as? T { return match }
            queue.append(contentsOf: view.subviews)
        }
        return nil
    }
}

extension UIModel {
    /// Native sessions anywhere in the daemon's catalog whose title, folder or
    /// first prompt hold `word` (`conversation.list` with `query`), including
    /// those past the page the sidebar loaded; nil when the daemon cannot answer.
    func searchSessions(_ word: String, limit: Int) async -> [CatalogItem]? {
        guard let engine, state.availability.isReady else { return nil }
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: (try? engine.list(query: word, provider: nil, limit: limit))?.catalog?.items)
            }
        }
    }
}

private struct SearchPaletteKey: EnvironmentKey {
    static let defaultValue: SearchPaletteModel? = nil
}

extension EnvironmentValues {
    /// The window's palette, for views that follow what it opens.
    var searchPalette: SearchPaletteModel? {
        get { self[SearchPaletteKey.self] }
        set { self[SearchPaletteKey.self] = newValue }
    }
}

// MARK: - Views

/// While the palette is open, what is behind it takes no clicks or shortcuts
/// (⌘↩ would send the composer's draft).
struct PaletteModal: ViewModifier {
    @ObservedObject var palette: SearchPaletteModel

    func body(content: Content) -> some View {
        content.disabled(palette.isPresented)
    }
}

/// The palette over the window, with a scrim that closes it when clicked.
struct SearchPaletteOverlay: View {
    @ObservedObject var palette: SearchPaletteModel

    var body: some View {
        if palette.isPresented {
            ZStack(alignment: .top) {
                Color.black.opacity(0.18)
                    .contentShape(Rectangle())
                    .onTapGesture { palette.close() }
                    .accessibilityHidden(true)
                SearchPaletteView(palette: palette)
                    .padding(.top, 48)
                    .padding([.horizontal, .bottom], 24)
            }
            .onExitCommand { palette.close() }
        }
    }
}

struct SearchPaletteView: View {
    @ObservedObject var palette: SearchPaletteModel
    @Environment(\.textScale) private var scale
    @State private var hovered: String?

    /// The list's height at most, at actual size.
    static let listHeight: CGFloat = 420

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary).readingFont(.subheading)
                PaletteSearchField(text: $palette.query, placeholder: "Search conversations and messages",
                                   font: ReadingStyle.subheading.nsFont(scale: scale).withWeight(.regular),
                                   onMove: { palette.move($0) }, onSubmit: { palette.activate() },
                                   onCancel: { palette.close() }, onWindow: { palette.window = $0 })
                if palette.searching { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 14).padding(.vertical, 12)
            Divider()
            content
            Divider()
            newConversationRow
            Divider()
            footer
        }
        .frame(maxWidth: 680)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .windowBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.secondary.opacity(0.25)))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .shadow(color: .black.opacity(0.25), radius: 24, y: 10)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Search conversations")
    }

    @ViewBuilder private var content: some View {
        if palette.results.isEmpty {
            Text(emptyWords).readingFont(.secondary).foregroundStyle(.secondary)
                .padding(.horizontal, 16).padding(.vertical, 14)
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            // As tall as its rows up to a cap, or, in a window too short for
            // that, as tall as there is room for.
            ViewThatFits(in: .vertical) {
                list.fixedSize(horizontal: false, vertical: true)
                list
            }
        }
    }

    private var list: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if palette.outcome.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Text("Recent").readingFont(.caption, weight: .semibold).foregroundStyle(.secondary)
                            .padding(.horizontal, 14).padding(.top, 6).padding(.bottom, 2)
                    }
                    ForEach(palette.results) { result in
                        SearchResultRow(result: result, selected: result.id == palette.selection,
                                        hovered: result.id == hovered)
                            .id(result.id)
                            .onTapGesture { palette.activate(result) }
                            .onHover { inside in
                                if inside { hovered = result.id } else if hovered == result.id { hovered = nil }
                            }
                    }
                }
                .padding(.vertical, 4)
            }
            .frame(maxHeight: SearchPaletteView.listHeight * max(1, scale))
            .onChange(of: palette.selection) { _, id in
                if let id { proxy.scrollTo(id) }
            }
        }
    }

    private var newConversationRow: some View {
        let selected = palette.selection == SearchPaletteModel.newConversationID
        return HStack(spacing: 10) {
            Image(systemName: "square.and.pencil").foregroundStyle(.secondary).frame(width: 22)
            Text("Start a new conversation").readingFont(.body)
            Spacer()
            Text("⌘N").readingFont(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 7).fill(selected ? Color.accentColor.opacity(0.2) : .clear)
            .padding(.horizontal, 6))
        .contentShape(Rectangle())
        .onTapGesture { palette.startConversation() }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isSelected, .isButton] : .isButton)
    }

    private var emptyWords: String {
        let query = palette.query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !palette.ready { return "Getting ready to search…" }
        if query.isEmpty { return "No conversations or sessions yet." }
        return "Nothing matches “\(query)”."
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 14) {
                KeyHint(keys: "↑ ↓", action: "move")
                KeyHint(keys: "↩", action: "open")
                KeyHint(keys: "esc", action: "close")
                Spacer()
                if let count = countWords {
                    Text(count).readingFont(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
            }
            Text(coverageWords).readingFont(.footnote).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
    }

    private var countWords: String? {
        let outcome = palette.outcome
        guard palette.ready, !outcome.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        if outcome.total > outcome.results.count { return "\(outcome.results.count) of \(outcome.total) — type more to narrow" }
        return "\(outcome.total) result\(outcome.total == 1 ? "" : "s")"
    }

    private var coverageWords: String {
        var words = "Titles, folders and providers of every conversation and session; the messages of conversations "
            + "opened since Subfleet started, and each session's first prompt."
        if palette.outcome.ignoredWords > 0 {
            words += " Only the first \(SearchQuery.maximumWords) words are searched."
        }
        if palette.sessionsCapped, let word = palette.sessionsWord {
            words += " The catalog listed its newest \(SearchPaletteModel.sessionLimit) sessions holding “\(word)”."
        }
        return words
    }
}

private struct KeyHint: View {
    let keys: String
    let action: String

    var body: some View {
        HStack(spacing: 4) {
            Text(keys).readingFont(.caption, weight: .semibold)
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: 4).fill(Color.secondary.opacity(0.14)))
            Text(action).readingFont(.caption)
        }
        .foregroundStyle(.secondary)
    }
}

struct SearchResultRow: View {
    let result: SearchResult
    let selected: Bool
    var hovered = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            ProviderBadge(provider: result.entry.provider).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(highlightedText(result.entry.title, result.titleHighlights))
                        .readingFont(.body, weight: .medium).lineLimit(1)
                    if case .native = result.entry.target {
                        Text("Session").readingFont(.footnote).foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.secondary.opacity(0.14)))
                            .help("An existing \(providerName) session; opening it continues it here")
                    }
                }
                Text(detailLine).readingFont(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                if let snippet = result.snippet {
                    Text(snippetLine(snippet)).readingFont(.secondary).lineLimit(2)
                }
            }
            Spacer(minLength: 8)
            if result.entry.pendingApprovals > 0 {
                Image(systemName: "hand.raised.fill").foregroundStyle(.orange).help("Waiting for your approval")
            }
            if result.entry.active { ProgressView().controlSize(.mini) }
            if !result.entry.continuable {
                Image(systemName: "lock").foregroundStyle(.secondary).help(result.entry.continueBlocker ?? "Cannot continue here")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 7).fill(background).padding(.horizontal, 6))
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isSelected, .isButton] : .isButton)
    }

    private var background: Color {
        selected ? Color.accentColor.opacity(0.2) : hovered ? Color.secondary.opacity(0.08) : .clear
    }

    private var providerName: String { result.entry.provider == "codex" ? "Codex" : "Claude" }

    /// "Claude · ~/code/app · 3 hr. ago": the provider when the query named it,
    /// the workspace (matched words highlighted), and when it was last active.
    private var detailLine: AttributedString {
        var parts: [AttributedString] = []
        if result.providerMatched { parts.append(highlightedText(providerName, [providerName.startIndex..<providerName.endIndex])) }
        if !result.entry.subtitle.isEmpty {
            parts.append(highlightedText(result.entry.subtitle, result.workspaceHighlights))
        }
        if let date = result.entry.date { parts.append(AttributedString(relativeWords(date))) }
        var line = AttributedString()
        for (index, part) in parts.enumerated() {
            if index > 0 { line += AttributedString(" · ") }
            line += part
        }
        return line
    }

    private func snippetLine(_ snippet: SearchSnippet) -> AttributedString {
        var author = AttributedString(snippet.author + ": ")
        author.swiftUI.foregroundColor = .secondary
        return author + highlightedText(snippet.text, snippet.highlights)
    }
}

/// Matched words in bold on a highlight.
func highlightedText(_ text: String, _ ranges: [Range<String.Index>]) -> AttributedString {
    var out = AttributedString()
    for segment in SearchHighlight.segments(text, ranges) {
        var run = AttributedString(String(segment.text))
        if segment.highlighted {
            run.inlinePresentationIntent = .stronglyEmphasized
            run.swiftUI.backgroundColor = Color.yellow.opacity(0.4)
        }
        out += run
    }
    return out
}

/// "3 hr. ago", "yesterday".
func relativeWords(_ date: Date, now: Date = Date()) -> String {
    let formatter = RelativeDateTimeFormatter()
    formatter.unitsStyle = .short
    formatter.dateTimeStyle = .named
    return formatter.localizedString(for: date, relativeTo: now)
}

// MARK: - The field

final class PaletteNSTextField: NSTextField {
    var onWindow: (NSWindow?) -> Void = { _ in }
    var onSubmit: () -> Void = {}

    /// ⌘↩ opens the selection too; it must not reach the composer's Send
    /// under the palette.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if (event.keyCode == 36 || event.keyCode == 76) && modifiers == .command && currentEditor() != nil {
            onSubmit()
            return true
        }
        return super.performKeyEquivalent(with: event)
    }

    /// Takes the keys as soon as it is in a window.
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        onWindow(window)
        guard let window else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, self.window === window else { return }
            window.makeFirstResponder(self)
        }
    }
}

/// The palette's search field: typing searches; Up and Down (Control-P and
/// Control-N too, Tab and Shift-Tab) move the selection, Page Up and Page Down
/// move by a page, Return (and ⌘↩) opens, Escape closes. Tab never takes the
/// keys out of the palette to what is behind it.
struct PaletteSearchField: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var font: NSFont
    var onMove: (Int) -> Void
    var onSubmit: () -> Void
    var onCancel: () -> Void
    var onWindow: (NSWindow?) -> Void = { _ in }

    func makeNSView(context: Context) -> PaletteNSTextField {
        let field = PaletteNSTextField()
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.usesSingleLineMode = true
        field.lineBreakMode = .byTruncatingTail
        field.cell?.isScrollable = true
        field.cell?.wraps = false
        field.placeholderString = placeholder
        field.font = font
        field.stringValue = text
        field.delegate = context.coordinator
        field.setAccessibilityLabel("Search")
        field.onWindow = onWindow
        field.onSubmit = onSubmit
        return field
    }

    func updateNSView(_ field: PaletteNSTextField, context: Context) {
        context.coordinator.parent = self
        field.onWindow = onWindow
        field.onSubmit = onSubmit
        if field.stringValue != text { field.stringValue = text }
        if field.font != font { field.font = font }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    /// Which key commands the field takes, and what each does.
    static func perform(_ selector: Selector, move: (Int) -> Void, submit: () -> Void, cancel: () -> Void) -> Bool {
        switch selector {
        case #selector(NSResponder.moveUp(_:)), #selector(NSResponder.insertBacktab(_:)): move(-1)
        case #selector(NSResponder.moveDown(_:)), #selector(NSResponder.insertTab(_:)): move(1)
        case #selector(NSResponder.scrollPageUp(_:)), #selector(NSResponder.pageUp(_:)): move(-8)
        case #selector(NSResponder.scrollPageDown(_:)), #selector(NSResponder.pageDown(_:)): move(8)
        case #selector(NSResponder.insertNewline(_:)): submit()
        case #selector(NSResponder.cancelOperation(_:)): cancel()
        default: return false
        }
        return true
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: PaletteSearchField

        init(_ parent: PaletteSearchField) { self.parent = parent }

        func controlTextDidChange(_ notification: Notification) {
            guard let field = notification.object as? NSTextField else { return }
            parent.text = field.stringValue
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            PaletteSearchField.perform(selector, move: parent.onMove, submit: parent.onSubmit, cancel: parent.onCancel)
        }
    }
}

// MARK: - Following a result into the conversation

extension View {
    /// The conversation's column: its widest grows with the text (C-29.13), and
    /// it scrolls to the message a search result matched (C-29.12).
    func conversationColumn(_ conversationID: String, proxy: ScrollViewProxy) -> some View {
        readingColumn().background(SearchRevealer(conversationID: conversationID, proxy: proxy))
    }
}

/// Scrolls the conversation to the message a search result matched. It is the
/// timeline's background inside its ScrollViewReader, so it takes no room.
struct SearchRevealer: View {
    @Environment(\.searchPalette) private var palette
    let conversationID: String
    let proxy: ScrollViewProxy

    var body: some View {
        if let palette {
            Follower(palette: palette, conversationID: conversationID, proxy: proxy)
        }
    }

    private struct Follower: View {
        @ObservedObject var palette: SearchPaletteModel
        let conversationID: String
        let proxy: ScrollViewProxy

        struct Key: Equatable {
            var reveal: SearchReveal?
            var conversationID: String
        }

        var body: some View {
            Color.clear.accessibilityHidden(true)
                .task(id: Key(reveal: palette.reveal, conversationID: conversationID)) {
                    guard let reveal = palette.reveal, reveal.conversationID == conversationID else { return }
                    // After the switch's own scroll to the end, and again once the
                    // history page that opening reads may have moved the rows.
                    for delay: UInt64 in [250_000_000, 550_000_000] {
                        try? await Task.sleep(nanoseconds: delay)
                        // Gone, or another conversation shown: this reveal is done with.
                        if Task.isCancelled { break }
                        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(reveal.itemID, anchor: .center) }
                    }
                    if palette.reveal == reveal { palette.reveal = nil }
                }
        }
    }
}

private extension NSFont {
    func withWeight(_ weight: NSFont.Weight) -> NSFont { .systemFont(ofSize: pointSize, weight: weight) }
}
#endif
