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
                if let conversation = model.state.focusedConversation {
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
                Image(systemName: "rectangle.on.rectangle").foregroundStyle(.secondary).help("Open in another app")
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

    var body: some View {
        let timeline = model.state.timelines[conversation.conversation_id]
        VStack(spacing: 0) {
            header
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
                                Task { await model.loadHistory(conversation.conversation_id) }
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
                    }
                    .padding(16)
                    .frame(maxWidth: 900, alignment: .leading)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: timeline?.items.count ?? 0) { _, _ in
                    withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            Divider()
            ComposerView(model: model, conversation: conversation)
        }
        .sheet(isPresented: Binding(get: { approval != nil }, set: { if !$0 { approval = nil } })) {
            if let approval {
                ApprovalSheet(model: model, card: approval.card, approvalID: approval.id) { self.approval = nil }
            }
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
        }
    }
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
    @AppStorage("lastProvider") private var provider = "claude"
    @State private var modelValue = ""
    @State private var permission = PermissionPolicy.ask.rawValue
    @State private var title = ""
    @State private var message = ""
    @State private var confirmWiden = false

    var body: some View {
        let models = model.state.models[provider] ?? []
        let writableCodex = model.state.availability.capabilities?.codex_writable == true
        VStack(alignment: .leading, spacing: 12) {
            Text("New conversation").font(.title3.bold())
            Picker("Provider", selection: $provider) {
                Text("Claude").tag("claude")
                Text("Codex").tag("codex")
            }.pickerStyle(.segmented)
            HStack {
                TextField("Workspace", text: $workspace)
                Button("Choose…") {
                    let panel = NSOpenPanel()
                    panel.canChooseDirectories = true
                    panel.canChooseFiles = false
                    panel.directoryURL = URL(fileURLWithPath: workspace)
                    if panel.runModal() == .OK, let url = panel.url { workspace = url.path }
                }
            }
            Picker("Model", selection: $modelValue) {
                ForEach(models) { entry in Text(entry.id).tag(entry.value) }
            }
            Picker("Permission", selection: $permission) {
                ForEach(PermissionPolicy.allCases, id: \.rawValue) { policy in
                    Text(policy.label).tag(policy.rawValue)
                        .disabled(provider == "codex" && policy != .readOnly && !writableCodex)
                }
            }
            if provider == "codex" && !writableCodex {
                Text("Codex conversations are read-only until the never-rules check is recorded on this daemon.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: permission) {
                Toggle("I understand the agent will act without asking", isOn: $confirmWiden).font(.callout)
            }
            TextField("Title (optional)", text: $title)
            TextField("First message", text: $message, axis: .vertical).lineLimit(3...8)
            HStack {
                Button("Cancel", role: .cancel) { isPresented = false }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Start") {
                    let settings = ConversationSettings(model: modelValue, permission: permission)
                    model.create(provider: provider, workspace: workspace, settings: settings,
                                 title: title.isEmpty ? nil : title, firstMessage: message, staged: [],
                                 confirmWiden: confirmWiden)
                    isPresented = false
                }
                .keyboardShortcut(.defaultAction)
                .disabled(modelValue.isEmpty || workspace.isEmpty
                          || (PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: permission) && !confirmWiden))
            }
        }
        .padding(18)
        .frame(width: 520)
        .onAppear(perform: pickDefaults)
        .onChange(of: provider) { _, _ in pickDefaults() }
    }

    private func pickDefaults() {
        let models = model.state.models[provider] ?? []
        if !models.contains(where: { $0.value == modelValue }) { modelValue = models.first?.value ?? "" }
        if provider == "codex" && model.state.availability.capabilities?.codex_writable != true {
            permission = PermissionPolicy.readOnly.rawValue
        } else if permission == PermissionPolicy.readOnly.rawValue && provider == "claude" {
            permission = PermissionPolicy.ask.rawValue
        }
    }
}
#endif
