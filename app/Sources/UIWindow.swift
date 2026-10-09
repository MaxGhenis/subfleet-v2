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
    @State private var columnVisibility = NavigationSplitViewVisibility.all

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            SidebarView(model: model, selection: $selection, search: { palette.toggle(model) })
                .navigationSplitViewColumnWidth(min: 240, ideal: 280, max: 420)
        } detail: {
            VStack(spacing: 0) {
                if let banner = model.state.availability.banner {
                    StatusBanner(title: banner.title, detail: banner.detail, symbol: "bolt.slash")
                }
                if model.newDraft.isPresented {
                    NewConversationDraftView(model: model)
                } else if let draft = model.selectedFailedDraft {
                    FailedConversationDraftView(draft: draft, changeFolder: { model.changeFailedDraftFolder(draft) },
                                                copy: { model.copyFailedDraft(draft) },
                                                discard: { model.discardFailedDraft(draft.id) })
                } else if let locked = model.lockedEntry {
                    LockedSessionView(entry: locked)
                } else if let conversation = model.state.focusedConversation {
                    ConversationView(model: model, conversation: conversation)
                } else {
                    EmptyConversationView { model.openNewDraft() }
                }
                if let problem = model.problem {
                    NoticeRow(symbol: "exclamationmark.triangle") {
                        HStack {
                            if !model.failedDrafts.isEmpty {
                                Button(problem) { model.selectFailedDraft() }.buttonStyle(.link).readingFont(.secondary)
                            } else { Text(problem).readingFont(.secondary) }
                            Spacer()
                            Button { model.problem = nil } label: { Image(systemName: "xmark") }
                                .buttonStyle(.borderless).accessibilityLabel("Dismiss notice").help("Dismiss notice")
                        }
                    }.padding(Theme.space.inset)
                }
            }
        }
        .toolbar(removing: .sidebarToggle)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                Button {
                    columnVisibility = columnVisibility == .detailOnly ? .all : .detailOnly
                } label: { Label("Toggle sidebar", systemImage: "sidebar.left") }
                    .keyboardShortcut("s", modifiers: [.command, .control])
                    .help("Show or hide the sidebar (⌃⌘S)")
            }
        }
        .onChange(of: selection) { _, value in
            guard let value else { return }
            if value.hasPrefix("failed:") {
                model.selectFailedDraft(String(value.dropFirst("failed:".count)))
                return
            }
            if let entry = model.state.sidebarEntries().first(where: { $0.id == value }) { model.select(entry) }
        }
        .onChange(of: model.state.focusedConversationID) { _, id in
            // Focus from anywhere (a notification, a continued session, a new
            // conversation) moves the highlight, so clicking a row always selects.
            if let id, selection != "cv:" + id { selection = "cv:" + id }
        }
        .foregroundStyle(Theme.text.primary.color)
        .background(Theme.surface.conversation.color)
        .tint(Theme.accent)
        .toolbarBackground(Theme.surface.conversation.color, for: .windowToolbar)
        .onChange(of: model.newDraft.isPresented) { _, visible in if visible { selection = nil } }
        .onChange(of: model.selectedFailedDraftID) { _, id in
            if let id { selection = "failed:" + id }
        }
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
            Image(systemName: "lock").font(.largeTitle).foregroundStyle(Theme.text.secondary.color)
            Text(entry.title).readingFont(.subheading).multilineTextAlignment(.center)
            if !entry.subtitle.isEmpty { Text(entry.subtitle).readingFont(.caption).foregroundStyle(Theme.text.secondary.color) }
            Text(lockedWords(entry)).readingFont(.body).foregroundStyle(Theme.text.secondary.color).multilineTextAlignment(.center)
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
        NoticeRow(symbol: symbol) {
            VStack(alignment: .leading, spacing: Theme.space.step) {
                Text(title).readingFont(.body)
                Text(detail).readingFont(.secondary).foregroundStyle(Theme.text.secondary.color)
            }
        }
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @ObservedObject var model: UIModel
    @Binding var selection: String?
    var search: () -> Void = {}
    @FocusState private var listFocused: Bool

    private var sections: [SidebarSection] {
        let sections = model.state.sidebar()
        guard model.state.grouping == .recency else { return sections }
        let recent = sections.filter { ["Today", "Yesterday"].contains($0.title) }
        let earlier = sections.filter { !["Today", "Yesterday"].contains($0.title) }.flatMap(\.entries)
        return recent + (earlier.isEmpty ? [] : [SidebarSection(id: "earlier", title: "Earlier", entries: earlier)])
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.space.step) {
            Button { model.openNewDraft() } label: {
                Label("New conversation", systemImage: "square.and.pencil")
                    .frame(maxWidth: .infinity, alignment: .leading).frame(height: Theme.space.row)
            }.keyboardShortcut("n").buttonStyle(QuietButtonStyle()).windowFont(.sidebar)
            Button(action: search) {
                HStack {
                    Label("Search", systemImage: "magnifyingglass")
                    Spacer()
                    Text("⌘K").foregroundStyle(Theme.text.tertiary.color)
                }.frame(height: Theme.space.row)
            }.buttonStyle(QuietButtonStyle()).windowFont(.sidebar).help("Search conversations and messages (⌘K)")
            HStack {
                Menu {
                    Button("Recent") { model.setGrouping(.recency) }
                    Button("Workspace") { model.setGrouping(.workspace) }
                } label: {
                    Text(model.state.grouping == .recency ? "Recent" : "Workspace")
                }.menuStyle(.borderlessButton).fixedSize().help("Group conversations by date or workspace")
                Spacer()
                Menu {
                    Picker("Provider", selection: Binding(get: { model.state.providerFilter ?? "" }, set: {
                        model.setProviderFilter($0.isEmpty ? nil : $0)
                    })) {
                        Text("All providers").tag("")
                        Text("Claude").tag("claude")
                        Text("Codex").tag("codex")
                    }
                } label: {
                    Label(model.state.providerFilter?.capitalized ?? "All providers", systemImage: "line.3.horizontal.decrease")
                }.menuStyle(.borderlessButton).fixedSize().accessibilityLabel("Filter by provider")
            }.windowFont(.heading).foregroundColor(Theme.text.secondary.color).padding(.vertical, Theme.space.inset)
            List(selection: $selection) {
                if !model.failedDrafts.isEmpty {
                    Text("Drafts that need you").windowFont(.heading).foregroundStyle(Theme.text.tertiary.color)
                        .tag(nil as String?)
                        .selectionDisabled()
                    ForEach(model.failedDrafts) { draft in
                        Label(draft.text.isEmpty ? "Could not start" : draft.text, systemImage: "exclamationmark.triangle")
                            .windowFont(.sidebar).lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading).frame(height: Theme.space.row)
                            .tag("failed:" + draft.id).help(draft.failure.message)
                    }
                }
                ForEach(sections) { section in
                    Text(section.title).windowFont(.heading).foregroundStyle(Theme.text.tertiary.color)
                        .padding(.top, Theme.space.inset).padding(.bottom, Theme.space.step)
                        .tag(nil as String?)
                        .selectionDisabled()
                    ForEach(section.entries) { entry in
                        SidebarRow(entry: entry) {
                            if case .conversation(let id) = entry.target { model.revealApprovals(in: id) }
                            selection = entry.id
                        }
                        .padding(.horizontal, Theme.space.inset)
                        .tag(entry.id).help(entry.title + "\n" + entry.subtitle)
                        .listRowInsets(EdgeInsets())
                        .listRowSeparator(.hidden)
                    }
                }
            }
            .listStyle(.sidebar).scrollContentBackground(.hidden)
            .focused($listFocused)
            .background {
                Button("Focus conversations") { listFocused = true }
                    .keyboardShortcut("s", modifiers: [.command, .option])
                    .frame(width: 0, height: 0).opacity(0).accessibilityHidden(true)
            }
        }
        .padding(Theme.space.inset)
        .foregroundStyle(Theme.text.primary.color)
        .background(Theme.surface.sidebar.color)
    }
}

struct SidebarRow: View {
    let entry: SidebarEntry
    let showApprovals: () -> Void
    @Environment(\.textScale) private var scale

    var body: some View {
        HStack(spacing: Theme.space.inset) {
            Text(entry.title).windowFont(.sidebar).lineLimit(1)
            Spacer(minLength: Theme.space.step)
            if entry.pendingApprovals > 0 {
                Button(action: showApprovals) {
                    Label("\(entry.pendingApprovals)", systemImage: "hand.raised")
                        .windowFont(.heading).foregroundStyle(Theme.state.attention)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(Theme.surface.raised.color))
                }
                .buttonStyle(.borderless)
                .help("Waiting for your approval; click to show it")
                .accessibilityLabel(approvalsWaitingWords(entry.pendingApprovals))
            }
            if let words = entry.needsYouLabel {
                Image(systemName: "exclamationmark.octagon").foregroundStyle(Theme.state.attention)
                    .help("Needs your decision: " + words).accessibilityLabel("Needs your decision: " + words)
            }
            if entry.active {
                ProgressView().controlSize(.mini).help("A turn is running").accessibilityLabel("Running")
            }
            if entry.liveElsewhere {
                Image(systemName: "rectangle.on.rectangle").foregroundStyle(Theme.text.secondary.color)
                    .help("Open in another app or terminal").accessibilityLabel("Open elsewhere")
            }
            if !entry.continuable {
                Image(systemName: "lock").foregroundStyle(Theme.text.secondary.color)
                    .help(entry.continueBlocker ?? "Cannot continue here").accessibilityLabel("Cannot continue here")
            }
        }
        .frame(height: Theme.space.row * max(1, scale))
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
    /// The waiting cards already brought into view, so each is scrolled to once.
    @State private var approvals = ApprovalFollower()
    /// Useful for previews of the same expansion users can open.
    var initiallyExpandedWork = false

    var body: some View {
        let timeline = model.state.timelines[conversation.conversation_id]
        let pendingRows = timeline?.pendingApprovalItems ?? []
        VStack(spacing: 0) {
            header
            RunsStrip(runs: model.runs[conversation.conversation_id] ?? [])
            if let banner = model.state.blockedBanner(for: conversation.conversation_id) {
                BlockedConversationBanner(banner: banner) {
                    model.perform($0, conversationID: conversation.conversation_id)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: Theme.space.reply) {
                        if let timeline, !timeline.historyComplete, timeline.historyPagesLoaded > 0 {
                            Button("Load earlier") {
                                Task { await model.loadHistory(conversation.conversation_id, follow: true) }
                            }.buttonStyle(.link)
                        }
                        ForEach(timeline.map(WorkPresentation.rows) ?? []) { row in
                            switch row {
                            case .item(let item):
                                TimelineRow(model: model, conversation: conversation, item: item, review: review).id(item.id)
                            case .work(let group):
                                WorkGroupView(group: group, initiallyExpanded: initiallyExpandedWork,
                                    served: group.completed ? group.items.first?.messageID.flatMap {
                                        model.state.servedChip(conversationID: conversation.conversation_id, messageID: $0)
                                    } : nil).id(group.id)
                            }
                        }
                        Theme.clear.frame(height: 1).id("bottom")
                            .onAppear { atBottom = true }
                            .onDisappear { atBottom = false }
                    }
                    .padding(Theme.space.column)
                    .conversationColumn(conversation.conversation_id, proxy: proxy)
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
                .onChange(of: conversation.conversation_id) { _, _ in
                    atBottom = true
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
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
                    Image(systemName: "rectangle.on.rectangle").foregroundStyle(Theme.state.attention)
                    // No fixedSize here: outside the scroll view, a text sized to its ideal
                    // height at the narrowest width set the window's minimum height, and the
                    // window's content overflowed it (2.1.2 build 7).
                    Text("Open in the Claude app or a terminal. Close it there to continue here; "
                         + "a message you send waits until then.")
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color).lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 14).padding(.top, 6)
            }
            if timeline?.pendingApprovalCards.contains(where: { $0.kind == "question" }) == true {
                Text("The agent is waiting on you. Pick a reply in the question card or type your own there.")
                    .font(.caption).foregroundStyle(Theme.text.secondary.color)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14).padding(.top, 6)
            }
            ComposerView(model: model, conversation: conversation)
        }
        .foregroundStyle(Theme.text.primary.color)
        .background(Theme.surface.conversation.color)
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
    /// A question is answered on its own inline card, which this brings into view;
    /// the request sheet has no form for its answers (C-27.2).
    private func reviewOldest() {
        guard let card = model.state.timelines[conversation.conversation_id]?.pendingApprovalItems.first?.pendingCard
        else { return }
        model.revealApprovals(in: conversation.conversation_id)
        if reviewOpensRequestSheet(card) { review(card) }
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
        var reveal = model.approvalReveal?.conversationID == id
        let target = approvals.target(in: timeline, reveal: &reveal)
        if !reveal { model.revealed(id) }
        guard let target else { return }
        // After this update's own scrolling (to the end, for a row that just
        // arrived), so the view settles on the card.
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(target, anchor: .center) }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(model.state.conversationTitle(conversation)).windowFont(.title).lineLimit(1)
                    .contextMenu {
                        Button("Rename…") {
                            renamedTitle = model.state.conversationTitle(conversation)
                            renaming = true
                        }
                    }
                Text(abbreviatedPath(conversation.workspace)).windowFont(.heading).foregroundStyle(Theme.text.secondary.color).lineLimit(1)
            }
            Spacer()
            Button {
                renamedTitle = model.state.conversationTitle(conversation)
                renaming = true
            } label: { Image(systemName: "pencil") }
                .buttonStyle(.borderless).help("Rename").accessibilityLabel("Rename")
            if model.canShowChanges {
                Button { model.showChanges(.conversation(conversation.conversation_id)) } label: {
                    Label("Changes", systemImage: "plus.forwardslash.minus")
                }
                .buttonStyle(.borderless)
                .help("What this conversation changed in its checkout since its first writable turn")
            }
            AccountUsageChip(model: model, conversation: conversation)
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(Theme.surface.conversation.color)
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
                    .readingFont(.caption).foregroundStyle(Theme.text.secondary.color).lineLimit(2)
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
        case .thinking:
            EmptyView() // Visible summaries live only inside WorkGroupView.
        case .tool(let activity):
            ToolRow(activity: activity)
        case .approval(let card):
            ApprovalCardView(model: model, conversationID: conversation.conversation_id, card: card,
                             review: { review(card) })
        case .error(let message, let kind, let willRetry):
            NoticeRow(symbol: "exclamationmark.triangle") {
                Text((kind.map { "\($0): " } ?? "") + message + (willRetry ? " (retrying)" : ""))
            }
        case .notice(let words):
            VStack(alignment: .leading, spacing: Theme.space.step) {
                NoticeRow(symbol: "info.circle") { Text(words) }
                if item.id.hasPrefix("person:"), let id = item.messageID,
                   let turn = model.state.timelines[conversation.conversation_id]?.turn(id) {
                    TurnStatusLine(model: model, conversation: conversation, turn: turn)
                }
            }
        case .taskNotification(let notice):
            TaskNotificationView(notice: notice)
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
        .font(.caption2.weight(.semibold)).foregroundStyle(Theme.text.secondary.color)
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
            if let footer { Label(footer, systemImage: "photo").readingFont(.caption).foregroundStyle(Theme.text.secondary.color) }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: Theme.radius.container).fill(Theme.surface.raised.color))
        .frame(maxWidth: ReadingStyle.bubbleWidth(scale: scale), alignment: .trailing)
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

struct TurnStatusLine: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    let turn: TurnTimeline

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.space.step) {
            HStack(spacing: 8) {
                if turn.showsMessageAcknowledgment {
                    if turn.isReadSteer { ReadMark() }
                    else if turn.isUnreadSteer || turn.state == "sending" { ProgressView().controlSize(.mini) }
                    Text(model.state.timelines[conversation.conversation_id]?.statusText(
                        of: turn.messageID, assistant: conversation.provider == "codex" ? "Codex" : "Claude") ?? turn.statusText)
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
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
            if let chip = model.state.servedChip(conversationID: conversation.conversation_id, messageID: turn.messageID) {
                ServedChipView(chip: chip)
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
                    if let subtitle { Text(subtitle).readingFont(.caption).foregroundStyle(Theme.text.secondary.color).lineLimit(2) }
                }
                Spacer()
                if case .turn = scope {
                    Button("Whole conversation") { model.showChanges(.conversation(conversation.conversation_id)) }
                        .buttonStyle(.link).readingFont(.caption)
                }
                Button { Task { await model.loadChanges(scope) } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).help("Compare again").accessibilityLabel("Compare again")
                Button { model.changesScope = nil } label: { Image(systemName: "xmark") }
                    .buttonStyle(.borderless).help("Close").accessibilityLabel("Close")
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
                // Overlapping edits can cancel out. Disclose the sharing and
                // any nested repositories the snapshot could not show.
                PaneNote(text: diffEmptyWords(result),
                         symbol: diffNotes(result).isEmpty && diffSharedWords(result) == nil
                             ? "checkmark.circle" : "info.circle")
            } else {
                if let shared = diffSharedWords(result) {
                    // C-26.14: another conversation wrote in this folder meanwhile.
                    Label(shared, systemImage: "person.2")
                        .font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 10).padding(.top, 6)
                }
                ForEach(diffNotes(result), id: \.self) { note in
                    Label(note, systemImage: "info.circle").readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
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

struct PaneNote: View {
    let text: String
    let symbol: String

    var body: some View {
        VStack {
            Label(text, systemImage: symbol).readingFont(.secondary).foregroundStyle(Theme.text.secondary.color).padding(14)
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
                Text("binary").foregroundStyle(Theme.text.secondary.color)
            } else {
                if let added = file.additions, added > 0 { Text("+\(added)").foregroundStyle(Theme.state.success) }
                if let removed = file.deletions, removed > 0 { Text("−\(removed)").foregroundStyle(Theme.state.error) }
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
    case "added": return Theme.state.success
    case "deleted": return Theme.state.error
    case "renamed", "copied": return Theme.accent
    default: return Theme.state.attention
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
                            .background(Theme.surface.raised.color)
                        if section.lines.isEmpty {
                            Text(section.binary ? "Binary file: no text to show." : "No line changes (a mode or a rename).")
                                .readingFont(.caption).foregroundStyle(Theme.text.secondary.color).padding(8)
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
            Text(line.old.map(String.init) ?? "").frame(width: ceil(size * 3.4), alignment: .trailing).foregroundStyle(Theme.text.tertiary.color)
            Text(line.new.map(String.init) ?? "").frame(width: ceil(size * 3.4), alignment: .trailing).foregroundStyle(Theme.text.tertiary.color)
            Text(marker).frame(width: ceil(size * 1.5)).foregroundStyle(markerColor)
            Text(shown).fixedSize().foregroundStyle(line.kind == .hunk || line.kind == .meta ? Theme.text.secondary.color : Theme.text.primary.color)
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

    private var markerColor: Color { line.kind == .added ? Theme.state.success : line.kind == .removed ? Theme.state.error : Theme.text.secondary.color }

    private var background: Color {
        switch line.kind {
        case .added: return Theme.state.added
        case .removed: return Theme.state.removed
        case .hunk: return Theme.state.changed
        default: return Theme.clear
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
            ProgressView().controlSize(.mini).accessibilityLabel("Turn in progress")
            if let since = turn.statusSince {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text("\(turn.statusText) · \(elapsedWords(from: since, to: context.date))")
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color).monospacedDigit()
                }
            } else {
                Text(turn.statusText).readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
            }
            Spacer()
            if let label = reviewButtonLabel(pending: pendingApprovals) {
                Button(action: review) { Label(label, systemImage: "hand.raised.fill") }
                    .buttonStyle(.bordered).tint(Theme.accent).controlSize(.small)
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
                Image(systemName: "arrow.triangle.branch").foregroundStyle(Theme.text.secondary.color)
                if live.isEmpty {
                    Text("\(runs.count) sub-agent run\(runs.count == 1 ? "" : "s"), none running")
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                } else {
                    Text(live.prefix(3).map(runLine).joined(separator: "   "))
                        .readingFont(.caption).lineLimit(1).truncationMode(.tail)
                    if live.count > 3 { Text("+\(live.count - 3)").readingFont(.caption).foregroundStyle(Theme.text.secondary.color) }
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
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color).lineLimit(1)
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
    case "succeeded": return Theme.state.success
    case "failed", "lost": return Theme.state.error
    default: return Theme.text.secondary.color
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
        let parts = [chip.account, chip.model, chip.effort, chip.fast.map { "Fast " + $0 }].compactMap { $0 }.filter { !$0.isEmpty }
        VStack(alignment: .leading, spacing: Theme.space.step) {
            if !parts.isEmpty {
                Text(parts.joined(separator: " · ")).readingFont(.footnote).foregroundStyle(Theme.text.secondary.color)
            }
            ForEach(chip.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle").readingFont(.footnote).foregroundStyle(Theme.state.attention)
            }
        }
        .padding(.horizontal, 6).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(Theme.surface.raised.color))
    }
}

struct WorkGroupView: View {
    let group: WorkGroup
    let served: ServedChip?
    @State private var expanded: Bool
    init(group: WorkGroup, initiallyExpanded: Bool = false, served: ServedChip? = nil) {
        self.group = group
        self.served = served
        _expanded = State(initialValue: initiallyExpanded)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.space.inset) {
            Button { expanded.toggle() } label: {
                HStack(spacing: Theme.space.inset) {
                    if group.running != nil && !group.completed { ProgressView().controlSize(.mini) }
                    Text(group.label)
                    if group.failed > 0 {
                        Image(systemName: "xmark.circle").foregroundStyle(Theme.state.error)
                        Text("\(group.failed) failed")
                    }
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    if let running = group.running, !group.completed {
                        Text(running.label).lineLimit(1).truncationMode(.tail)
                    }
                    Spacer(minLength: 0)
                }
                .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                .contentShape(Rectangle())
            }
            .buttonStyle(QuietButtonStyle())
            .help(group.tooltip(served: served, expanded: expanded))
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            if expanded {
                ForEach(group.items) { item in
                    switch item.content {
                    case .tool(let activity): ToolRow(activity: activity)
                    case .thinking(let text, _):
                        Text(text).italic().readingFont(.secondary).foregroundStyle(Theme.text.secondary.color)
                            .textSelection(.enabled)
                    default: EmptyView()
                    }
                }
            }
        }
        .padding(.vertical, Theme.space.step)
        .overlay(alignment: .bottom) {
            if group.completed { Rectangle().fill(Theme.line.hairline).frame(height: 1).offset(y: Theme.space.inset) }
        }
    }
}

struct ToolRow: View {
    let activity: ToolActivity
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.space.step) {
            Button { expanded.toggle() } label: {
                HStack(spacing: Theme.space.inset) {
                    switch activity.state {
                    case .running: ProgressView().controlSize(.mini)
                    case .succeeded: Image(systemName: "checkmark").foregroundStyle(Theme.text.tertiary.color)
                    case .failed: Image(systemName: "xmark.circle").foregroundStyle(Theme.state.error)
                    case .unfinished: Image(systemName: "circle.dashed").foregroundStyle(Theme.text.tertiary.color)
                    }
                    Text(activity.label).readingFont(.caption, design: .monospaced).lineLimit(1)
                    Text(activity.name).readingFont(.footnote).foregroundStyle(Theme.text.tertiary.color)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                    Spacer(minLength: 0)
                }.foregroundStyle(Theme.text.secondary.color).contentShape(Rectangle())
            }
            .buttonStyle(QuietButtonStyle()).help("\(activity.state.rawValue): \(activity.label). Show input and output")
            .accessibilityValue(activity.state.rawValue)
            if expanded && !activity.hidden {
                Text(activity.summary).readingFont(.code, design: .monospaced).textSelection(.enabled)
                    .padding(Theme.space.inset).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: Theme.radius.card).fill(Theme.surface.raised.color))
                if let preview = activity.preview, !preview.isEmpty {
                    Text(preview).readingFont(.code, design: .monospaced).textSelection(.enabled)
                        .padding(Theme.space.inset).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: Theme.radius.card).fill(Theme.surface.raised.color))
                }
            }
        }
        .padding(.leading, Theme.space.inset)
    }
}

#endif
