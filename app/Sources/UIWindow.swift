// Subfleet: the main window — conversations on the left, the focused
// conversation's timeline and composer on the right.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct MainWindow: View {
    @ObservedObject var model: UIModel
    /// The ⌘K palette (C-29.12); its overlay observes it.
    let palette: SearchPaletteModel
    /// C-29.13: every conversation text size follows this scale.
    @AppStorage(TextScale.defaultsKey) private var textScale = TextScale.actual
    @State private var selection: String?
    @State private var search = ""

    var body: some View {
        NavigationSplitView {
            SidebarView(model: model, selection: $selection)
                .navigationSplitViewColumnWidth(min: 240, ideal: 300, max: 420)
        } detail: {
            VStack(spacing: 0) {
                if let banner = model.state.availability.banner {
                    StatusBanner(title: banner.title, detail: banner.detail, symbol: "bolt.slash")
                }
                if model.newDraft.isPresented {
                    NewConversationDraftView(model: model)
                } else if let locked = model.lockedEntry {
                    LockedSessionView(entry: locked)
                } else if let conversation = model.state.focusedConversation {
                    ConversationView(model: model, conversation: conversation)
                } else {
                    VStack(spacing: 12) {
                        Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle).foregroundStyle(.secondary)
                        Text("Choose a conversation or start a new one").readingFont(.body).foregroundStyle(.secondary)
                        Text("Press ⌘K to search conversations and messages").readingFont(.caption).foregroundStyle(.tertiary)
                        Button("New conversation") { model.openNewDraft() }.keyboardShortcut("n")
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                if let problem = model.problem {
                    HStack {
                        Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                        Text(problem).readingFont(.secondary).lineLimit(2)
                        Spacer()
                        Button { model.problem = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless)
                    }
                    .padding(8)
                    .background(.bar)
                }
            }
        }
        .searchable(text: $search, placement: .sidebar, prompt: "Search conversations")
        .onChange(of: search) { _, value in model.setSearch(value) }
        .onChange(of: selection) { _, value in
            guard let value else { return }
            if let entry = model.state.sidebarEntries().first(where: { $0.id == value }) { model.select(entry) }
        }
        .onChange(of: model.state.focusedConversationID) { _, id in
            // Focus from anywhere (a notification, a continued session, a new
            // conversation) moves the highlight, so clicking a row always selects.
            if let id, selection != "cv:" + id { selection = "cv:" + id }
        }
        .toolbar {
            ToolbarItemGroup {
                Picker("Provider", selection: Binding(get: { model.state.providerFilter ?? "all" },
                                                      set: { model.setProviderFilter($0 == "all" ? nil : $0) })) {
                    Text("All").tag("all")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                }.pickerStyle(.segmented)
                Button { palette.toggle(model) } label: { Label("Search", systemImage: "magnifyingglass") }
                    .help("Search conversations and messages (⌘K)")
                Button { model.openNewDraft() } label: { Label("New", systemImage: "plus") }
                    .keyboardShortcut("n")
            }
        }
        .onChange(of: model.newDraft.isPresented) { _, visible in if visible { selection = nil } }
        .onAppear { model.start() }
        .onReceive(NotificationCenter.default.publisher(for: .subfleetNewConversation)) { _ in
            palette.close(restoringFocus: false)
            model.openNewDraft()
        }
        .modifier(PaletteModal(palette: palette))
        .overlay { SearchPaletteOverlay(palette: palette) }
        .onDisappear { palette.close(restoringFocus: false) }
        .background(TextScaleEqualsShortcut(scale: $textScale))
        .environment(\.textScale, textScale)
        .environment(\.searchPalette, palette)
    }
}

extension Notification.Name {
    static let subfleetNewConversation = Notification.Name("org.maxghenis.subfleet.new-conversation")
}

/// A session Subfleet lists but cannot continue (a Codex-app thread): what it
/// is and why, in place of a failed open.
struct LockedSessionView: View {
    let entry: SidebarEntry

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "lock").font(.largeTitle).foregroundStyle(.secondary)
            Text(entry.title).readingFont(.subheading).multilineTextAlignment(.center)
            if !entry.subtitle.isEmpty { Text(entry.subtitle).readingFont(.caption).foregroundStyle(.secondary) }
            Text(lockedWords(entry)).readingFont(.body).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .frame(maxWidth: 460)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

func lockedWords(_ entry: SidebarEntry) -> String {
    if entry.provider == "codex" && entry.continueBlocker == "codex-app thread: continue by handoff" {
        return "This thread lives in the Codex app's own home, not in a Subfleet lane. Keep using it there; "
            + "continuing it here takes a handoff, which this build does not offer yet."
    }
    switch entry.continueBlocker {
    case "tmp-workspace":
        return "This session's working directory is under /tmp, where Subfleet does not continue sessions."
    case "a Subfleet lane run":
        return "This is a run Subfleet started on a lane, not a conversation."
    case let blocker?:
        return "This session cannot continue here: \(blocker)."
    case nil:
        return "This session cannot continue here."
    }
}

struct StatusBanner: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol).foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).bold().readingFont(.body)
                Text(detail).readingFont(.secondary).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(10)
        .background(Color.orange.opacity(0.12))
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @ObservedObject var model: UIModel
    @Binding var selection: String?

    var body: some View {
        List(selection: $selection) {
            ForEach(model.state.sidebar()) { section in
                Section(section.title) {
                    ForEach(section.entries) { entry in
                        SidebarRow(entry: entry).tag(entry.id)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) {
            Picker("Group by", selection: Binding(get: { model.state.grouping }, set: { model.setGrouping($0) })) {
                Text("Recent").tag(SidebarGrouping.recency)
                Text("Workspace").tag(SidebarGrouping.workspace)
            }
            .pickerStyle(.segmented)
            .padding(8)
        }
    }
}

struct SidebarRow: View {
    let entry: SidebarEntry

    var body: some View {
        HStack(spacing: 8) {
            ProviderBadge(provider: entry.provider)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(entry.title).readingFont(.body).lineLimit(1)
                    if case .native = entry.target {
                        Image(systemName: "arrow.uturn.right.circle").readingFont(.footnote).foregroundStyle(.secondary)
                            .help("An existing \(entry.provider == "codex" ? "Codex" : "Claude") session; opening it continues it here")
                    }
                }
                Text(entry.subtitle).readingFont(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            if entry.pendingApprovals > 0 {
                Label("\(entry.pendingApprovals)", systemImage: "hand.raised.fill").labelStyle(.titleAndIcon)
                    .font(.caption).foregroundStyle(.orange).help("Waiting for your approval")
            }
            if entry.blockedBy != nil {
                Image(systemName: "exclamationmark.octagon").foregroundStyle(.red).help("Needs your decision")
            }
            if entry.active {
                ProgressView().controlSize(.mini)
            }
            if entry.liveElsewhere {
                Image(systemName: "rectangle.on.rectangle").foregroundStyle(.secondary)
                    .help("Also open in the Claude app or a terminal")
            }
            if !entry.continuable {
                Image(systemName: "lock").foregroundStyle(.secondary).help(entry.continueBlocker ?? "Cannot continue here")
            }
        }
        .padding(.vertical, 2)
    }
}

struct ProviderBadge: View {
    let provider: String
    @Environment(\.textScale) private var scale

    var body: some View {
        // 9 pt in a 22 × 16 badge at actual size, growing with the text (C-29.13).
        let size = ReadingStyle.footnote.pointSize(scale: scale) * 0.75
        Text(provider == "codex" ? "CX" : "CL")
            .font(.system(size: size, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: ceil(size * 22 / 9), height: ceil(size * 16 / 9))
            .background(RoundedRectangle(cornerRadius: 4).fill(provider == "codex" ? Color.teal : Color.orange))
            .help(provider == "codex" ? "Codex" : "Claude")
    }
}

// MARK: - Conversation

struct ConversationView: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    @State private var approval: (card: ApprovalCard, id: String)?
    /// Whether the end of the timeline is on screen: streamed text is followed
    /// only then, so reading further up is not interrupted.
    @State private var atBottom = true
    @State private var renaming = false
    @State private var renamedTitle = ""

    var body: some View {
        let timeline = model.state.timelines[conversation.conversation_id]
        VStack(spacing: 0) {
            header
            RunsStrip(runs: model.runs[conversation.conversation_id] ?? [])
            if let banner = model.state.blockedBanner(for: conversation.conversation_id) {
                VStack(alignment: .leading, spacing: 6) {
                    StatusBanner(title: banner.title, detail: banner.detail, symbol: "exclamationmark.octagon")
                    HStack {
                        ForEach(Array(banner.choices.enumerated()), id: \.offset) { _, choice in
                            Button(choice.label) { model.perform(choice, conversationID: conversation.conversation_id) }
                                .help(choice.detail)
                        }
                    }.padding(.horizontal, 10).padding(.bottom, 8)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if let timeline, !timeline.historyComplete, timeline.historyPagesLoaded > 0 {
                            Button("Load earlier") {
                                Task { await model.loadHistory(conversation.conversation_id, follow: true) }
                            }.buttonStyle(.link)
                        }
                        ForEach(timeline?.items ?? []) { item in
                            TimelineRow(model: model, conversation: conversation, item: item) { card in
                                Task {
                                    if let id = await model.approvalID(for: card, conversationID: conversation.conversation_id) {
                                        approval = (card, id)
                                    } else {
                                        model.problem = "That approval is no longer pending."
                                    }
                                }
                            }
                            .id(item.id)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                            .onAppear { atBottom = true }
                            .onDisappear { atBottom = false }
                    }
                    .padding(16)
                    .conversationColumn(conversation.conversation_id, proxy: proxy)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: timeline?.items.last?.id) { _, _ in
                    // A new last row: followed while the end is on screen, and always
                    // for the person's own message. Rows 'Load earlier' adds go first
                    // and change no last row.
                    let own = timeline?.items.last.map { if case .person = $0.content { return true } else { return false } } ?? false
                    if atBottom || own {
                        withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                }
                .onChange(of: timeline?.items.last.map(streamedLength) ?? 0) { _, _ in
                    // A text or thinking block growing in place adds no row.
                    if atBottom { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            Divider()
            if let timeline, let live = timeline.liveMessageID, let turn = timeline.turn(live), turn.outcome == nil {
                // The live turn's strip stays in view however far the timeline scrolls.
                LiveTurnStrip(model: model, conversation: conversation, turn: turn)
                    .padding(.horizontal, 14).padding(.top, 6)
            }
            if conversation.live_elsewhere == true {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "rectangle.on.rectangle").foregroundStyle(.orange)
                    // No fixedSize here: outside the scroll view, a text sized to its ideal
                    // height at the narrowest width set the window's minimum height, and the
                    // window's content overflowed it (2.1.2 build 7).
                    Text("Open in the Claude app or a terminal. Close it there to continue here; "
                         + "a message you send waits until then.")
                        .readingFont(.caption).foregroundStyle(.secondary).lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 14).padding(.top, 6)
            }
            if timeline?.pendingApprovalCards.contains(where: { $0.kind == "question" }) == true {
                Text("The agent is waiting on you. Pick a reply in the question card or type your own there.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14).padding(.top, 6)
            }
            ComposerView(model: model, conversation: conversation)
        }
        .task(id: conversation.conversation_id) {
            // Sub-agents change outside this conversation's event log: look every 10 s.
            while !Task.isCancelled {
                await model.refreshRuns(conversation.conversation_id)
                try? await Task.sleep(nanoseconds: 10_000_000_000)
            }
        }
        .sheet(isPresented: Binding(get: { approval != nil }, set: { if !$0 { approval = nil } })) {
            if let approval {
                ApprovalSheet(model: model, card: approval.card, approvalID: approval.id) { self.approval = nil }
            }
        }
        .alert("Rename conversation", isPresented: $renaming) {
            TextField("Title", text: $renamedTitle)
            Button("Rename") { model.renameConversation(conversation.conversation_id, title: renamedTitle) }
                .disabled(renamedTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("Cancel", role: .cancel) {}
        }
        .inspector(isPresented: Binding(get: { model.changesScope?.conversationID == conversation.conversation_id },
                                        set: { if !$0 { model.changesScope = nil } })) {
            if let scope = model.changesScope {
                ChangesPane(model: model, scope: scope, conversation: conversation)
                    .inspectorColumnWidth(min: 360, ideal: 560, max: 1100)
            }
        }
        .onChange(of: timeline?.liveMessageID) { _, _ in
            // A turn started or ended: an open pane compares again.
            if let scope = model.changesScope, scope.conversationID == conversation.conversation_id {
                Task { await model.loadChanges(scope) }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            ProviderBadge(provider: conversation.provider)
            VStack(alignment: .leading, spacing: 1) {
                Text(model.state.conversationTitle(conversation)).readingFont(.subheading).lineLimit(1)
                    .contextMenu {
                        Button("Rename…") {
                            renamedTitle = model.state.conversationTitle(conversation)
                            renaming = true
                        }
                    }
                Text(abbreviatedPath(conversation.workspace)).readingFont(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Button {
                renamedTitle = model.state.conversationTitle(conversation)
                renaming = true
            } label: { Image(systemName: "pencil") }
                .buttonStyle(.borderless).help("Rename conversation")
            if model.canShowChanges {
                Button { model.showChanges(.conversation(conversation.conversation_id)) } label: {
                    Label("Changes", systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.borderless)
                .help("What this conversation changed in its checkout since its first writable turn")
            }
            Text(PermissionPolicy(rawValue: conversation.settings.permission)?.label ?? conversation.settings.permission)
                .readingFont(.caption).padding(.horizontal, 6).padding(.vertical, 2)
                .background(Capsule().fill(Color.secondary.opacity(0.15)))
                .help("Permission policy")
            if conversation.origin == "native" || conversation.origin == "legacy" {
                Text(conversation.origin == "legacy" ? "Imported" : "Continued").readingFont(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(.bar)
    }
}

struct TimelineRow: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    let item: TimelineItem
    let review: (ApprovalCard) -> Void

    var body: some View {
        switch item.content {
        case .history(let role, let text, let tool):
            if role == "user" {
                PersonBubble(text: text, footer: nil)
            } else if let tool {
                Label(tool + (text.isEmpty ? "" : ": " + text), systemImage: "wrench.and.screwdriver")
                    .readingFont(.caption).foregroundStyle(.secondary).lineLimit(2)
            } else {
                MarkdownView(text: text)
            }
        case .person(let text, let attachments, _):
            let turn = item.messageID.flatMap { model.state.timelines[conversation.conversation_id]?.turn($0) }
            VStack(alignment: .trailing, spacing: 4) {
                PersonBubble(text: text ?? "(message text not available)", footer: attachments.isEmpty ? nil
                             : "\(attachments.count) image\(attachments.count == 1 ? "" : "s")")
                if let turn {
                    TurnStatusLine(model: model, conversation: conversation, turn: turn)
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        case .text(let text, let final):
            MarkdownView(text: text, streaming: !final)
        case .thinking(let text, let final):
            DisclosureGroup {
                Text(text).italic().readingFont(.secondary).foregroundStyle(.secondary).textSelection(.enabled)
            } label: {
                Label(final ? "Thought" : "Thinking…", systemImage: "brain").readingFont(.caption).foregroundStyle(.secondary)
            }
        case .tool(let activity):
            ToolRow(activity: activity)
        case .approval(let card):
            ApprovalCardView(model: model, conversationID: conversation.conversation_id, card: card,
                             review: { review(card) })
        case .error(let message, let kind, let willRetry):
            Label((kind.map { "\($0): " } ?? "") + message + (willRetry ? " (retrying)" : ""),
                  systemImage: "exclamationmark.triangle")
                .readingFont(.secondary).foregroundStyle(.red)
        case .notice(let words):
            Text(words).readingFont(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity)
        case .steered:
            // `Timeline.items` draws the steered message's own bubble in this place.
            EmptyView()
        }
    }
}

/// Read by the provider: a double check, as a messaging app marks it.
struct ReadMark: View {
    var body: some View {
        ZStack(alignment: .leading) {
            Image(systemName: "checkmark")
            Image(systemName: "checkmark").offset(x: 4)
        }
        .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
        .padding(.trailing, 4)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Read")
    }
}

struct PersonBubble: View {
    let text: String
    let footer: String?
    @Environment(\.textScale) private var scale

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text).readingFont(.body).textSelection(.enabled)
            if let footer { Label(footer, systemImage: "photo").readingFont(.caption).foregroundStyle(.secondary) }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.14)))
        .frame(maxWidth: ReadingStyle.bubbleWidth(scale: scale), alignment: .trailing)
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

struct TurnStatusLine: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    let turn: TurnTimeline

    var body: some View {
        HStack(spacing: 8) {
            let live = turn.messageState.map { [.waiting, .starting, .running, .approvalNeeded].contains($0) } ?? (turn.state == "sending")
            if turn.isReadSteer {
                ReadMark()
            } else if live || turn.isUnreadSteer {
                ProgressView().controlSize(.mini)
            }
            // A steered message reads as Claude Code's does: unread, by what the turn is doing, then Read.
            Text(model.state.timelines[conversation.conversation_id]?.statusText(
                of: turn.messageID, assistant: conversation.provider == "codex" ? "Codex" : "Claude") ?? turn.statusText)
                .readingFont(.caption).foregroundStyle(.secondary)
            if let chip = model.state.servedChip(conversationID: conversation.conversation_id, messageID: turn.messageID) {
                ServedChipView(chip: chip)
            }
            if live && !turn.steerRequested {
                Button("Stop") {
                    model.stop(stopAction(for: turn.messageID, state: turn.state, outboxEntry: nil))
                }.buttonStyle(.link).font(.caption)
            }
            if let stats = model.turnChanges[turn.messageID], stats.files > 0 {
                Button {
                    model.showChanges(.turn(conversationID: conversation.conversation_id, messageID: turn.messageID))
                } label: {
                    Label(diffStatsWords(stats), systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.link).readingFont(.caption)
                .help("What this turn changed")
            }
        }
        .task(id: askChanges ? turn.messageID : nil) {
            if askChanges { await model.loadTurnChanges(turn.messageID) }
        }
    }

    /// A finished turn of a conversation that may write: its counts are worth asking for.
    /// A steered message has no turn of its own; its host's line shows the changes.
    private var askChanges: Bool {
        guard model.canShowChanges, conversation.settings.permission != PermissionPolicy.readOnly.rawValue,
              let state = turn.messageState, state != .steered else { return false }
        return MessageState.terminal.contains(state)
    }
}

// MARK: - Changes

/// What a conversation, or one of its turns, changed in its checkout
/// (`conversation.diff`, `turn.diff`; C-26.14): the files, then the diff.
struct ChangesPane: View {
    @ObservedObject var model: UIModel
    let scope: ChangesScope
    let conversation: Conversation
    @State private var selected: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).readingFont(.subheading).lineLimit(1)
                    if let subtitle { Text(subtitle).readingFont(.caption).foregroundStyle(.secondary).lineLimit(2) }
                }
                Spacer()
                if case .turn = scope {
                    Button("Whole conversation") { model.showChanges(.conversation(conversation.conversation_id)) }
                        .buttonStyle(.link).readingFont(.caption)
                }
                Button { Task { await model.loadChanges(scope) } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).help("Compare again")
                Button { model.changesScope = nil } label: { Image(systemName: "xmark") }
                    .buttonStyle(.borderless).help("Close")
            }
            .padding(10)
            Divider()
            content
        }
        .onChange(of: scope) { _, _ in selected = nil }
    }

    private var title: String {
        switch scope {
        case .conversation: return "Changes in this conversation"
        case .turn: return "Changes in this turn"
        }
    }

    private var subtitle: String? {
        guard case .loaded(let result, _)? = model.changes[scope], result.available else { return nil }
        let when: String
        switch scope {
        case .conversation: when = "Since its first writable turn began, to the working tree now"
        case .turn: when = result.isLive ? "Since the turn began, to the working tree now (still running)"
                                         : "From the turn's start to its end"
        }
        return diffStatsWords(result.stats) + " · " + when
    }

    @ViewBuilder private var content: some View {
        switch model.changes[scope] {
        case nil, .loading?:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message)?:
            PaneNote(text: message, symbol: "exclamationmark.triangle")
        case .loaded(let result, let sections)?:
            if !result.available {
                PaneNote(text: diffUnavailableWords(result), symbol: "info.circle")
            } else if result.files.isEmpty {
                PaneNote(text: "No changes.", symbol: "checkmark.circle")
            } else {
                ForEach(diffNotes(result), id: \.self) { note in
                    Label(note, systemImage: "info.circle").readingFont(.caption).foregroundStyle(.secondary)
                        .padding(.horizontal, 10).padding(.top, 6)
                }
                List(selection: $selected) {
                    ForEach(result.files) { file in DiffFileRow(file: file).tag(file.path) }
                }
                .listStyle(.plain)
                .frame(minHeight: 80, idealHeight: min(CGFloat(result.files.count) * 24 + 8, 220), maxHeight: 220)
                .fixedSize(horizontal: false, vertical: true)
                if selected != nil {
                    Button("Show every file") { selected = nil }.buttonStyle(.link).readingFont(.caption)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                }
                Divider()
                DiffLinesView(sections: selected.map { path in sections.filter { $0.path == path } } ?? sections)
            }
        }
    }
}

/// What the daemon cut or hid, so a short diff is not taken for the whole one.
func diffNotes(_ result: DiffResult) -> [String] {
    var notes: [String] = []
    if result.files_truncated { notes.append("Only the first \(result.files.count) files are listed.") }
    if !result.stats.complete { notes.append("Git's listing was cut; the counts cover the listed files only.") }
    if result.truncated { notes.append("The diff is cut at its size limit; the rest is not shown.") }
    if result.scrubbed > 0 {
        notes.append("\(result.scrubbed) value\(result.scrubbed == 1 ? "" : "s") that looked like credentials are replaced.")
    }
    return notes
}

struct PaneNote: View {
    let text: String
    let symbol: String

    var body: some View {
        VStack {
            Label(text, systemImage: symbol).readingFont(.secondary).foregroundStyle(.secondary).padding(14)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct DiffFileRow: View {
    let file: DiffFile

    var body: some View {
        HStack(spacing: 6) {
            Text(diffStatusLetter(file.status)).bold().foregroundStyle(diffStatusColor(file.status)).frame(width: 14)
                .help(file.status)
            Text(file.from.map { "\($0) → \(file.path)" } ?? file.path).lineLimit(1).truncationMode(.middle)
            Spacer(minLength: 4)
            if file.binary {
                Text("binary").foregroundStyle(.secondary)
            } else {
                if let added = file.additions, added > 0 { Text("+\(added)").foregroundStyle(.green) }
                if let removed = file.deletions, removed > 0 { Text("−\(removed)").foregroundStyle(.red) }
            }
        }
        .readingFont(.caption, design: .monospaced)
    }
}

func diffStatusLetter(_ status: String) -> String {
    switch status {
    case "added": return "A"
    case "deleted": return "D"
    case "modified": return "M"
    case "renamed": return "R"
    case "copied": return "C"
    case "type-changed": return "T"
    default: return "?"
    }
}

func diffStatusColor(_ status: String) -> Color {
    switch status {
    case "added": return .green
    case "deleted": return .red
    case "renamed", "copied": return .blue
    default: return .orange
    }
}

/// The unified diff, one row per line with both line numbers, one section per file.
struct DiffLinesView: View {
    let sections: [DiffSection]

    /// How wide a row is at least: the pane's width, less a vertical scroller the
    /// system draws beside the content (the legacy style, with a mouse attached),
    /// so a pane of short lines never scrolls sideways. Overlay scrollers take no
    /// width. A line's colour runs to the edge; a longer line scrolls sideways.
    static func rowWidth(pane: CGFloat, scrollerStyle: NSScroller.Style = NSScroller.preferredScrollerStyle) -> CGFloat {
        let scroller = scrollerStyle == .legacy ? NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy) : 0
        return max(0, pane - scroller)
    }

    /// Read again when a mouse arrives or "Show scroll bars" changes, so rows never
    /// keep a width for the other style (review of 5aa2718).
    @State private var scrollerStyle = NSScroller.preferredScrollerStyle

    var body: some View {
        GeometryReader { geometry in
            let width = Self.rowWidth(pane: geometry.size.width, scrollerStyle: scrollerStyle)
            ScrollView([.vertical, .horizontal]) {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(sections) { section in
                        Text(section.path).readingFont(.caption, weight: .bold, design: .monospaced)
                            .padding(.horizontal, 8).padding(.vertical, 5)
                            .frame(minWidth: width, alignment: .leading)
                            .background(Color.secondary.opacity(0.12))
                        if section.lines.isEmpty {
                            Text(section.binary ? "Binary file: no text to show." : "No line changes (a mode or a rename).")
                                .readingFont(.caption).foregroundStyle(.secondary).padding(8)
                        }
                        ForEach(section.lines) { line in DiffLineRow(line: line, minWidth: width) }
                    }
                }
                .textSelection(.enabled)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSScroller.preferredScrollerStyleDidChangeNotification)) { _ in
            scrollerStyle = NSScroller.preferredScrollerStyle
        }
    }
}

struct DiffLineRow: View {
    let line: DiffLine
    var minWidth: CGFloat = 520
    @Environment(\.textScale) private var scale

    var body: some View {
        // Line numbers of five digits fit at every text size.
        let size = ReadingStyle.caption.pointSize(scale: scale)
        HStack(spacing: 0) {
            Text(line.old.map(String.init) ?? "").frame(width: ceil(size * 3.4), alignment: .trailing).foregroundStyle(.tertiary)
            Text(line.new.map(String.init) ?? "").frame(width: ceil(size * 3.4), alignment: .trailing).foregroundStyle(.tertiary)
            Text(marker).frame(width: ceil(size * 1.5)).foregroundStyle(markerColor)
            Text(shown).fixedSize().foregroundStyle(line.kind == .hunk || line.kind == .meta ? Color.secondary : Color.primary)
        }
        .readingFont(.caption, design: .monospaced)
        .padding(.trailing, 12)
        .frame(minWidth: minWidth, alignment: .leading)
        .background(background)
    }

    /// Tabs as four spaces, and an empty line kept a line high.
    private var shown: String {
        let text = line.text.replacingOccurrences(of: "\t", with: "    ")
        return text.isEmpty ? " " : text
    }

    private var marker: String {
        switch line.kind {
        case .added: return "+"
        case .removed: return "−"
        default: return ""
        }
    }

    private var markerColor: Color { line.kind == .added ? .green : line.kind == .removed ? .red : .secondary }

    private var background: Color {
        switch line.kind {
        case .added: return Color.green.opacity(0.12)
        case .removed: return Color.red.opacity(0.12)
        case .hunk: return Color.blue.opacity(0.07)
        default: return .clear
        }
    }
}

/// The live turn's status above the composer: where the model is and for how
/// long, counting up, so a long think, a compaction or a slow tool reads as work.
struct LiveTurnStrip: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    let turn: TurnTimeline

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.mini)
            if let since = turn.statusSince {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text("\(turn.statusText) · \(elapsedWords(from: since, to: context.date))")
                        .readingFont(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
            } else {
                Text(turn.statusText).readingFont(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Stop") {
                model.stop(stopAction(for: turn.messageID, state: turn.state, outboxEntry: nil))
            }.buttonStyle(.link).font(.caption)
        }
    }
}

/// The sub-agents a conversation dispatched: the live ones in a line under the
/// header (what each is, the lane and model it landed on), all of them on click.
struct RunsStrip: View {
    let runs: [RunSummary]
    @State private var showAll = false

    var body: some View {
        let live = runs.filter(\.isLive)
        if !runs.isEmpty {
            HStack(spacing: 8) {
                Image(systemName: "arrow.triangle.branch").foregroundStyle(.secondary)
                if live.isEmpty {
                    Text("\(runs.count) sub-agent run\(runs.count == 1 ? "" : "s"), none running")
                        .readingFont(.caption).foregroundStyle(.secondary)
                } else {
                    Text(live.prefix(3).map(runLine).joined(separator: "   "))
                        .readingFont(.caption).lineLimit(1).truncationMode(.tail)
                    if live.count > 3 { Text("+\(live.count - 3)").readingFont(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                Button(showAll ? "Hide" : "All runs") { showAll.toggle() }.buttonStyle(.link).readingFont(.caption)
            }
            .padding(.horizontal, 14).padding(.vertical, 4)
            .popover(isPresented: $showAll, arrowEdge: .bottom) { RunsList(runs: runs) }
        }
    }
}

/// "review-pr on codex-2 · gpt-6-astra", or where it waits.
func runLine(_ run: RunSummary) -> String {
    let name = run.name ?? run.task ?? run.job_id
    if run.state == "queued" || run.state == "waiting" {
        return "\(name) waiting" + (run.wait_reason.map { " (\($0))" } ?? "")
    }
    let model = run.model_served ?? run.model_requested
    return "\(name) on " + [run.lane_id, model].compactMap { $0 }.joined(separator: " · ")
}

struct RunsList: View {
    let runs: [RunSummary]

    var body: some View {
        List(runs) { run in
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: runSymbol(run.state)).foregroundStyle(runColor(run.state))
                VStack(alignment: .leading, spacing: 2) {
                    Text(run.name ?? run.job_id).readingFont(.secondary).lineLimit(1)
                    Text([run.task.map { t in run.tier.map { "\(t) · \($0)" } ?? t }, run.lane_id,
                          run.model_served ?? run.model_requested, run.state]
                        .compactMap { $0 }.joined(separator: "  ·  "))
                        .readingFont(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            .help(run.job_id)
        }
        .frame(width: 520, height: min(420, CGFloat(runs.count) * 44 + 20))
    }
}

func runSymbol(_ state: String) -> String {
    switch state {
    case "succeeded": return "checkmark.circle"
    case "failed", "lost": return "xmark.circle"
    case "cancelled": return "minus.circle"
    case "queued", "waiting": return "clock"
    default: return "circle.dotted"
    }
}

func runColor(_ state: String) -> Color {
    switch state {
    case "succeeded": return .green
    case "failed", "lost": return .red
    default: return .secondary
    }
}

/// How much text a timeline item holds; it grows while a block streams.
func streamedLength(_ item: TimelineItem) -> Int {
    switch item.content {
    case .text(let text, _), .thinking(let text, _): return text.count
    default: return 0
    }
}

/// "12s", "3m 05s", "1h 02m".
func elapsedWords(from start: Date, to now: Date) -> String {
    let seconds = max(0, Int(now.timeIntervalSince(start)))
    if seconds < 60 { return "\(seconds)s" }
    if seconds < 3600 { return String(format: "%dm %02ds", seconds / 60, seconds % 60) }
    return String(format: "%dh %02dm", seconds / 3600, (seconds % 3600) / 60)
}

struct ServedChipView: View {
    let chip: ServedChip

    var body: some View {
        let parts = [chip.account, chip.model, chip.effort, chip.fast].compactMap { $0 }.filter { !$0.isEmpty }
        HStack(spacing: 4) {
            if !parts.isEmpty {
                Text(parts.joined(separator: " · ")).readingFont(.footnote).foregroundStyle(.secondary)
            }
            ForEach(chip.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle").readingFont(.footnote).foregroundStyle(.orange)
            }
        }
        .padding(.horizontal, 6).padding(.vertical, 1)
        .background(Capsule().fill(Color.secondary.opacity(0.1)))
    }
}

struct ToolRow: View {
    let activity: ToolActivity
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                switch activity.state {
                case .running: ProgressView().controlSize(.mini)
                case .succeeded: Image(systemName: "checkmark.circle").foregroundStyle(.green)
                case .failed: Image(systemName: "xmark.circle").foregroundStyle(.red)
                case .unfinished: Image(systemName: "circle.dashed").foregroundStyle(.secondary)
                }
                Text(activity.name).readingFont(.caption, weight: .bold)
                Text(activity.summary).readingFont(.caption, design: .monospaced).lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(.secondary)
                Spacer()
                if activity.preview != nil {
                    Button { expanded.toggle() } label: { Image(systemName: expanded ? "chevron.up" : "chevron.down") }
                        .buttonStyle(.borderless)
                }
            }
            if expanded, let preview = activity.preview {
                Text(preview).readingFont(.caption, design: .monospaced).textSelection(.enabled)
                    .padding(6).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 4).fill(Color.secondary.opacity(0.08)))
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.2)))
    }
}

#endif
