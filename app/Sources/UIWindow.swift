// Subfleet: the main window — conversations on the left, the focused
// conversation's timeline and composer on the right.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct MainWindow: View {
    @ObservedObject var model: UIModel
    @State private var selection: String?
    @State private var search = ""
    @State private var showNew = false

    var body: some View {
        NavigationSplitView {
            SidebarView(model: model, selection: $selection)
                .navigationSplitViewColumnWidth(min: 240, ideal: 300, max: 420)
        } detail: {
            VStack(spacing: 0) {
                if let banner = model.state.availability.banner {
                    StatusBanner(title: banner.title, detail: banner.detail, symbol: "bolt.slash")
                }
                if let locked = model.lockedEntry {
                    LockedSessionView(entry: locked)
                } else if let conversation = model.state.focusedConversation {
                    ConversationView(model: model, conversation: conversation)
                } else {
                    VStack(spacing: 12) {
                        Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle).foregroundStyle(.secondary)
                        Text("Choose a conversation or start a new one").foregroundStyle(.secondary)
                        Button("New conversation") { showNew = true }.keyboardShortcut("n")
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                if let problem = model.problem {
                    HStack {
                        Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                        Text(problem).font(.callout).lineLimit(2)
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
                Button { showNew = true } label: { Label("New conversation", systemImage: "square.and.pencil") }
                    .keyboardShortcut("n")
            }
        }
        .sheet(isPresented: $showNew) { NewConversationSheet(model: model, isPresented: $showNew) }
        .onAppear { model.start() }
        .onReceive(NotificationCenter.default.publisher(for: .subfleetNewConversation)) { _ in showNew = true }
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
            Text(entry.title).font(.headline).multilineTextAlignment(.center)
            if !entry.subtitle.isEmpty { Text(entry.subtitle).font(.caption).foregroundStyle(.secondary) }
            Text(lockedWords(entry)).foregroundStyle(.secondary).multilineTextAlignment(.center)
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
                Text(title).bold()
                Text(detail).font(.callout).foregroundStyle(.secondary)
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
                        SidebarRow(entry: entry) {
                            // The hand badge opens the conversation at its oldest waiting card.
                            if case .conversation(let id) = entry.target { model.approvalReveal = id }
                            selection = entry.id
                        }
                        .tag(entry.id)
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
    /// The hand badge's action: show the conversation's waiting cards.
    let showApprovals: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            ProviderBadge(provider: entry.provider)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(entry.title).lineLimit(1)
                    if case .native = entry.target {
                        Image(systemName: "arrow.uturn.right.circle").font(.caption2).foregroundStyle(.secondary)
                            .help("An existing \(entry.provider == "codex" ? "Codex" : "Claude") session; opening it continues it here")
                    }
                }
                Text(entry.subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            if entry.pendingApprovals > 0 {
                Button(action: showApprovals) {
                    Label("\(entry.pendingApprovals)", systemImage: "hand.raised.fill").labelStyle(.titleAndIcon)
                        .font(.caption).foregroundStyle(.orange)
                }
                .buttonStyle(.borderless)
                .help("Waiting for your approval; click to show it")
                .accessibilityLabel(approvalsWaitingWords(entry.pendingApprovals))
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
    var body: some View {
        Text(provider == "codex" ? "CX" : "CL")
            .font(.system(size: 9, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: 22, height: 16)
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
    /// The waiting cards already brought into view, so each is scrolled to once.
    @State private var approvals = ApprovalFollower()

    var body: some View {
        let timeline = model.state.timelines[conversation.conversation_id]
        let pendingRows = timeline?.pendingApprovalItems ?? []
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
                            TimelineRow(model: model, conversation: conversation, item: item, review: review)
                                .id(item.id)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                            .onAppear { atBottom = true }
                            .onDisappear { atBottom = false }
                    }
                    .padding(16)
                    .frame(maxWidth: 900, alignment: .leading)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: timeline?.items.last?.id) { _, _ in
                    // A new last row: followed while the end is on screen, and always
                    // for the message the person just sent. Rows 'Load earlier' adds go
                    // first and change no last row.
                    let own = timeline?.items.last.map {
                        if case .person(_, _, let state) = $0.content { return state == "sending" } else { return false }
                    } ?? false
                    if atBottom || own {
                        withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                }
                .onChange(of: timeline?.followedItem?.id) { _, _ in
                    // A new row of a turn that has started, above the messages still
                    // queued: followed while the end is on screen.
                    if atBottom { withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("bottom", anchor: .bottom) } }
                }
                .onChange(of: timeline?.followedItem.map(streamedLength) ?? 0) { _, _ in
                    // A text or thinking block growing in place adds no row.
                    if atBottom { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: approvalKey(timeline, pendingRows)) { _, _ in followApprovals(proxy) }
                .onChange(of: model.approvalReveal) { _, _ in followApprovals(proxy) }
                .onAppear {
                    proxy.scrollTo("bottom", anchor: .bottom)
                    followApprovals(proxy)
                }
            }
            Divider()
            if let timeline, let turn = timeline.pinnedTurn {
                // The live turn's strip stays in view however far the timeline scrolls,
                // and with it a Review for every card waiting on the person.
                let stop = stopAction(for: turn.messageID, state: turn.state, outboxEntry: nil)
                LiveTurnStrip(turn: turn, pendingApprovals: pendingRows.count, review: reviewOldest,
                              stop: stop == .none ? nil : { model.stop(stop) })
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
                        .font(.caption).foregroundStyle(.secondary).lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
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

    /// Opens a card's request for the person to answer.
    private func review(_ card: ApprovalCard) {
        Task {
            if let id = await model.approvalID(for: card, conversationID: conversation.conversation_id) {
                approval = (card, id)
            } else {
                model.problem = "That approval is no longer pending."
            }
        }
    }

    /// The strip's Review: the oldest waiting card, brought into view and opened.
    private func reviewOldest() {
        guard let card = model.state.timelines[conversation.conversation_id]?.pendingApprovalItems.first?.pendingCard
        else { return }
        model.approvalReveal = conversation.conversation_id
        review(card)
    }

    /// What moves the view to a card: the conversation, whether its log has been
    /// read, its history pages, and its waiting cards.
    private func approvalKey(_ timeline: Timeline?, _ pending: [TimelineItem]) -> [String] {
        [conversation.conversation_id, timeline?.caughtUp == true ? "read" : "reading",
         String(timeline?.historyPagesLoaded ?? 0)] + pending.map(\.id)
    }

    /// Brings a waiting card into view: each new one once, when it appears,
    /// wherever the person was reading (it had sat far above the end, under
    /// queued messages and a later turn: 2026-09-27); the oldest when the
    /// conversation opens or the person asks.
    private func followApprovals(_ proxy: ScrollViewProxy) {
        let id = conversation.conversation_id
        guard let timeline = model.state.timelines[id] else { return }
        var reveal = model.approvalReveal == id
        let target = approvals.target(in: timeline, reveal: &reveal)
        if model.approvalReveal == id && !reveal { model.approvalReveal = nil }
        guard let target else { return }
        // After this update's own scrolling (to the end, for a row that just
        // arrived), so the view settles on the card.
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(target, anchor: .center) }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            ProviderBadge(provider: conversation.provider)
            VStack(alignment: .leading, spacing: 1) {
                Text(model.state.conversationTitle(conversation)).font(.headline).lineLimit(1)
                Text(abbreviatedPath(conversation.workspace)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if model.canShowChanges {
                Button { model.showChanges(.conversation(conversation.conversation_id)) } label: {
                    Label("Changes", systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.borderless)
                .help("What this conversation changed in its checkout since its first writable turn")
            }
            Text(PermissionPolicy(rawValue: conversation.settings.permission)?.label ?? conversation.settings.permission)
                .font(.caption).padding(.horizontal, 6).padding(.vertical, 2)
                .background(Capsule().fill(Color.secondary.opacity(0.15)))
                .help("Permission policy")
            if conversation.origin == "native" || conversation.origin == "legacy" {
                Text(conversation.origin == "legacy" ? "Imported" : "Continued").font(.caption).foregroundStyle(.secondary)
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
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
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
                Text(text).font(.callout).italic().foregroundStyle(.secondary).textSelection(.enabled)
            } label: {
                Label(final ? "Thought" : "Thinking…", systemImage: "brain").font(.caption).foregroundStyle(.secondary)
            }
        case .tool(let activity):
            ToolRow(activity: activity)
        case .approval(let card):
            ApprovalCardView(card: card, review: { review(card) })
        case .error(let message, let kind, let willRetry):
            Label((kind.map { "\($0): " } ?? "") + message + (willRetry ? " (retrying)" : ""),
                  systemImage: "exclamationmark.triangle")
                .font(.callout).foregroundStyle(.red)
        case .notice(let words):
            Text(words).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity)
        }
    }
}

struct PersonBubble: View {
    let text: String
    let footer: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text).textSelection(.enabled)
            if let footer { Label(footer, systemImage: "photo").font(.caption).foregroundStyle(.secondary) }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.14)))
        .frame(maxWidth: 640, alignment: .trailing)
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
            if live { ProgressView().controlSize(.mini) }
            Text(turn.statusText).font(.caption).foregroundStyle(.secondary)
            if let chip = model.state.servedChip(conversationID: conversation.conversation_id, messageID: turn.messageID) {
                ServedChipView(chip: chip)
            }
            if live {
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
                .buttonStyle(.link).font(.caption)
                .help("What this turn changed")
            }
        }
        .task(id: askChanges ? turn.messageID : nil) {
            if askChanges { await model.loadTurnChanges(turn.messageID) }
        }
    }

    /// A finished turn of a conversation that may write: its counts are worth asking for.
    private var askChanges: Bool {
        guard model.canShowChanges, conversation.settings.permission != PermissionPolicy.readOnly.rawValue,
              let state = turn.messageState else { return false }
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
                    Text(title).font(.headline).lineLimit(1)
                    if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                }
                Spacer()
                if case .turn = scope {
                    Button("Whole conversation") { model.showChanges(.conversation(conversation.conversation_id)) }
                        .buttonStyle(.link).font(.caption)
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
                    Label(note, systemImage: "info.circle").font(.caption).foregroundStyle(.secondary)
                        .padding(.horizontal, 10).padding(.top, 6)
                }
                List(selection: $selected) {
                    ForEach(result.files) { file in DiffFileRow(file: file).tag(file.path) }
                }
                .listStyle(.plain)
                .frame(minHeight: 80, idealHeight: min(CGFloat(result.files.count) * 24 + 8, 220), maxHeight: 220)
                .fixedSize(horizontal: false, vertical: true)
                if selected != nil {
                    Button("Show every file") { selected = nil }.buttonStyle(.link).font(.caption)
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
            Label(text, systemImage: symbol).foregroundStyle(.secondary).padding(14)
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
        .font(.system(.caption, design: .monospaced))
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
                        Text(section.path).font(.system(.caption, design: .monospaced).bold())
                            .padding(.horizontal, 8).padding(.vertical, 5)
                            .frame(minWidth: width, alignment: .leading)
                            .background(Color.secondary.opacity(0.12))
                        if section.lines.isEmpty {
                            Text(section.binary ? "Binary file: no text to show." : "No line changes (a mode or a rename).")
                                .font(.caption).foregroundStyle(.secondary).padding(8)
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

    var body: some View {
        HStack(spacing: 0) {
            Text(line.old.map(String.init) ?? "").frame(width: 40, alignment: .trailing).foregroundStyle(.tertiary)
            Text(line.new.map(String.init) ?? "").frame(width: 40, alignment: .trailing).foregroundStyle(.tertiary)
            Text(marker).frame(width: 18).foregroundStyle(markerColor)
            Text(shown).fixedSize().foregroundStyle(line.kind == .hunk || line.kind == .meta ? Color.secondary : Color.primary)
        }
        .font(.system(size: 11, design: .monospaced))
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
/// While cards wait on the person, its Review opens the oldest: the timeline
/// may hold them far out of view (2026-09-27).
struct LiveTurnStrip: View {
    let turn: TurnTimeline
    /// Cards waiting on the person in this conversation; with none, no Review.
    var pendingApprovals = 0
    var review: () -> Void = {}
    /// Nil when Stop has nothing to act on (the turn of a waiting card that the
    /// daemon has already settled).
    let stop: (() -> Void)?

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.mini)
            if let since = turn.statusSince {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text("\(turn.statusText) · \(elapsedWords(from: since, to: context.date))")
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                }
            } else {
                Text(turn.statusText).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let label = reviewButtonLabel(pending: pendingApprovals) {
                Button(action: review) { Label(label, systemImage: "hand.raised.fill") }
                    .buttonStyle(.borderedProminent).tint(.orange).controlSize(.small)
                    .help("Open the oldest request waiting for your approval")
            }
            if let stop { Button("Stop", action: stop).buttonStyle(.link).font(.caption) }
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
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text(live.prefix(3).map(runLine).joined(separator: "   "))
                        .font(.caption).lineLimit(1).truncationMode(.tail)
                    if live.count > 3 { Text("+\(live.count - 3)").font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                Button(showAll ? "Hide" : "All runs") { showAll.toggle() }.buttonStyle(.link).font(.caption)
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
                    Text(run.name ?? run.job_id).font(.callout).lineLimit(1)
                    Text([run.task.map { t in run.tier.map { "\(t) · \($0)" } ?? t }, run.lane_id,
                          run.model_served ?? run.model_requested, run.state]
                        .compactMap { $0 }.joined(separator: "  ·  "))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
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
                Text(parts.joined(separator: " · ")).font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(chip.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle").font(.caption2).foregroundStyle(.orange)
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
                Text(activity.name).font(.caption.bold())
                Text(activity.summary).font(.system(.caption, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(.secondary)
                Spacer()
                if activity.preview != nil {
                    Button { expanded.toggle() } label: { Image(systemName: expanded ? "chevron.up" : "chevron.down") }
                        .buttonStyle(.borderless)
                }
            }
            if expanded, let preview = activity.preview {
                Text(preview).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .padding(6).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 4).fill(Color.secondary.opacity(0.08)))
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.2)))
    }
}

struct ApprovalCardView: View {
    let card: ApprovalCard
    let review: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
                Text(title).bold()
                Spacer()
                switch card.state {
                case .pending: Button("Review", action: review).buttonStyle(.borderedProminent)
                case .answered(let decision): Text(decision.map { "Answered: \($0)" } ?? "Answered").font(.caption)
                case .withdrawn: Text("Withdrawn").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let command = card.display.command ?? card.display.input {
                Text(command).font(.system(.callout, design: .monospaced)).lineLimit(6).textSelection(.enabled)
            }
            if let reason = card.display.reason ?? card.display.description {
                Text(reason).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(card.isPending ? 0.12 : 0.05)))
    }

    private var title: String {
        switch card.kind {
        case "question": return "Question"
        case "command": return "Run a command?"
        case "file-change": return "Change files?"
        case "permissions": return "Grant permissions?"
        default: return "Use \(card.display.tool ?? "a tool")?"
        }
    }
}

// MARK: - Approval sheet

struct ApprovalSheet: View {
    @ObservedObject var model: UIModel
    let card: ApprovalCard
    let approvalID: String
    let done: () -> Void
    @State private var detail: ApprovalDetail?
    @State private var revealed = false
    @State private var confirmMasked = false
    @State private var answers: [String: String] = [:]
    @State private var note = ""
    @State private var sending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(card.kind == "question" ? "Answer the question" : "Approve this request?").font(.title3.bold())
            if let detail {
                if card.kind == "question" {
                    ForEach(Array(card.questions.enumerated()), id: \.offset) { _, question in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(question.question).bold()
                            if let options = question.options, !options.isEmpty {
                                Picker(question.header ?? "Choice", selection: Binding(
                                    get: { answers[question.question] ?? "" },
                                    set: { answers[question.question] = $0 })) {
                                    Text("Choose…").tag("")
                                    ForEach(options, id: \.label) { option in
                                        Text(option.label + (option.description.map { " — \($0)" } ?? "")).tag(option.label)
                                    }
                                }.labelsHidden()
                            }
                            TextField("Or type an answer", text: Binding(get: { answers[question.question] ?? "" },
                                                                        set: { answers[question.question] = $0 }))
                        }
                    }
                } else {
                    ScrollView {
                        Text(pretty(detail.request)).font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(minHeight: 120, maxHeight: 320)
                    .padding(6)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
                    if !detail.masked.isEmpty && !revealed {
                        HStack {
                            Label("\(detail.masked.count) value(s) that look like secrets are masked",
                                  systemImage: "eye.slash").font(.callout)
                            Spacer()
                            Button("Reveal") { Task { await load(reveal: true) } }
                        }
                        Toggle("I have reviewed the masked values", isOn: $confirmMasked).font(.callout)
                    }
                }
                TextField("Note to the agent (optional)", text: $note)
                HStack {
                    Button("Cancel", role: .cancel, action: done).keyboardShortcut(.cancelAction)
                    Spacer()
                    ForEach(detail.approval.options.reversed(), id: \.self) { option in
                        Button(label(option)) { Task { await answer(option, detail: detail) } }
                            .disabled(sending || !canChoose(option, detail: detail))
                            .buttonStyle(.bordered)
                            .tint(option == primaryOption(detail) ? Color.accentColor : nil)
                    }
                }
            } else {
                ProgressView("Loading the request…")
            }
        }
        .padding(18)
        .frame(width: 560)
        .task { await load(reveal: false) }
    }

    private func load(reveal: Bool) async {
        if let fresh = await model.approvalDetail(approvalID, reveal: reveal) {
            detail = fresh
            if reveal { revealed = true }
        } else if detail == nil {
            done()
        }
    }

    private func primaryOption(_ detail: ApprovalDetail) -> String {
        detail.approval.options.first { ["allow", "answer", "allow-turn"].contains($0) } ?? detail.approval.options.first ?? ""
    }

    private func canChoose(_ option: String, detail: ApprovalDetail) -> Bool {
        let allowing = ["allow", "allow-session", "allow-turn", "answer"].contains(option)
        if allowing && !detail.masked.isEmpty && !revealed && !confirmMasked { return false }
        if option == "answer" { return !answers.values.allSatisfy { $0.isEmpty } }
        return true
    }

    private func answer(_ option: String, detail: ApprovalDetail) async {
        sending = true
        defer { sending = false }
        let chosen = option == "answer" ? answers.filter { !$0.value.isEmpty } : nil
        if await model.respond(detail, decision: option, answers: chosen, message: note.isEmpty ? nil : note,
                               reviewedMasked: revealed || confirmMasked) {
            done()
        }
    }

    private func label(_ option: String) -> String {
        switch option {
        case "allow": return "Allow"
        case "allow-session": return "Allow for this session"
        case "allow-turn": return "Allow for this turn"
        case "deny": return "Deny"
        case "cancel-turn": return "Deny and stop"
        case "answer": return "Answer"
        default: return option
        }
    }

    private func pretty(_ value: JSONValue) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return (try? encoder.encode(value)).flatMap { String(data: $0, encoding: .utf8) } ?? "\(value)"
    }
}

// MARK: - New conversation

struct NewConversationSheet: View {
    @ObservedObject var model: UIModel
    @Binding var isPresented: Bool
    @AppStorage("lastWorkspace") private var workspace = NSHomeDirectory()
    /// "auto", "claude" or "codex"; Auto takes whichever has more lanes ready.
    @AppStorage("providerChoice") private var providerChoice = "auto"
    @AppStorage("lastModel.claude") private var claudeModel = ""
    @AppStorage("lastModel.codex") private var codexModel = ""
    @AppStorage("lastPermission") private var lastPermission = PermissionPolicy.ask.rawValue
    @State private var permission = PermissionPolicy.ask.rawValue
    @State private var title = ""
    @State private var message = ""
    @State private var confirmWiden = false
    @State private var capacity: [String: Int] = [:]

    private var provider: String { providerChoice == "auto" ? autoProvider(capacity) : providerChoice }
    private var modelValue: Binding<String> { provider == "codex" ? $codexModel : $claudeModel }

    var body: some View {
        let models = model.state.models[provider] ?? []
        let writableCodex = model.state.availability.capabilities?.codex_writable == true
        VStack(alignment: .leading, spacing: 12) {
            Text("New conversation").font(.title3.bold())
            HStack {
                TextField("Folder", text: $workspace)
                Menu("Recent") {
                    ForEach(model.recentWorkspaces(), id: \.self) { path in
                        Button(abbreviatedPath(path)) { workspace = path }
                    }
                }
                .fixedSize()
                Button("Choose…") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true
                    panel.canChooseFiles = false
                    panel.directoryURL = URL(fileURLWithPath: workspace)
                    if panel.runModal() == .OK, let url = panel.url { workspace = url.path }
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                Picker("Provider", selection: $providerChoice) {
                    Text("Auto").tag("auto")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                }.pickerStyle(.segmented)
                Text(capacityWords).font(.caption).foregroundStyle(.secondary)
            }
            Picker("Model", selection: modelValue) {
                ForEach(models) { entry in Text(entry.id).tag(entry.value) }
            }
            Picker("Permission", selection: $permission) {
                ForEach(PermissionPolicy.allCases, id: \.rawValue) { policy in
                    Text(policy.label).tag(policy.rawValue)
                        .disabled(provider == "codex" && policy != .readOnly && !writableCodex)
                }
            }
            if PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: permission) {
                Toggle("I understand the agent will act without asking", isOn: $confirmWiden).font(.callout)
            }
            TextField("Title (optional)", text: $title)
            TextField("First message", text: $message, axis: .vertical).lineLimit(3...10)
            HStack {
                Button("Cancel", role: .cancel) { isPresented = false }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Start") {
                    let settings = ConversationSettings(model: modelValue.wrappedValue, permission: permission)
                    lastPermission = permission
                    model.create(provider: provider, workspace: workspace, settings: settings,
                                 title: title.isEmpty ? nil : title, firstMessage: message, staged: [],
                                 confirmWiden: confirmWiden)
                    isPresented = false
                }
                .keyboardShortcut(.defaultAction)
                .disabled(modelValue.wrappedValue.isEmpty || workspace.isEmpty
                          || (PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: permission) && !confirmWiden))
            }
        }
        .padding(18)
        .frame(width: 560)
        .onAppear {
            capacity = model.providerCapacity()
            permission = lastPermission
            pickDefaults()
        }
        .onChange(of: providerChoice) { _, _ in pickDefaults() }
    }

    private var capacityWords: String {
        guard !capacity.isEmpty else { return "Lane readiness unknown; Auto uses Claude." }
        let words = ["claude", "codex"].compactMap { name in
            capacity[name].map { "\(name == "claude" ? "Claude" : "Codex"): \($0) lane\($0 == 1 ? "" : "s") ready" }
        }.joined(separator: " · ")
        return providerChoice == "auto" ? words + " — Auto uses \(provider == "codex" ? "Codex" : "Claude")" : words
    }

    private func pickDefaults() {
        let models = model.state.models[provider] ?? []
        if !models.contains(where: { $0.value == modelValue.wrappedValue }) {
            modelValue.wrappedValue = models.first?.value ?? ""
        }
        if provider == "codex" && model.state.availability.capabilities?.codex_writable != true {
            permission = PermissionPolicy.readOnly.rawValue
        } else if permission == PermissionPolicy.readOnly.rawValue && provider == "claude"
                    && lastPermission != PermissionPolicy.readOnly.rawValue {
            permission = lastPermission
        }
    }
}
#endif
