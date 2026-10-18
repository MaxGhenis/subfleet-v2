// Subfleet: suggested conversations in the window (C-29.14): the card in the
// timeline of the conversation that suggested one, the sidebar's Suggested
// section, and the detail column a sidebar row opens.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

/// The newer of two views of one suggestion, keeping a whole brief over a preview.
func latestSuggestion(_ shown: Suggestion, _ known: Suggestion?) -> Suggestion {
    guard let known, known.suggestion_id == shown.suggestion_id else { return shown }
    let knownIsNewer = (suggestionRank(known.state), known.updated_at) >= (suggestionRank(shown.state), shown.updated_at)
    return knownIsNewer ? shown.merged(with: known) : known.merged(with: shown)
}

/// One suggested conversation: what it is, why, where it would start, its brief,
/// and Start and Dismiss while it is open (C-29.14).
struct SuggestionCardView: View {
    @ObservedObject var model: UIModel
    let suggestion: Suggestion
    /// In the detail column the card names the conversation it came from.
    var showsOrigin = false
    /// The brief starts open where the card is the whole page.
    var briefInitiallyExpanded = false
    @State private var briefExpanded: Bool?
    @State private var confirming: PermissionPolicy?

    var body: some View {
        let current = latestSuggestion(suggestion, model.state.suggestions[suggestion.suggestion_id])
        let acting = model.suggestionsActing.contains(current.suggestion_id)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: Theme.space.inset) {
                Image(systemName: "plus.bubble").foregroundStyle(Theme.text.secondary.color)
                Text("Suggested conversation").readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                Spacer()
                if let words = suggestionStateWords(current), current.state != "starting" {
                    Text(words).readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                }
            }
            Text(current.title).readingFont(.subheading).textSelection(.enabled)
            Text(current.tldr).readingFont(.secondary).foregroundStyle(Theme.text.secondary.color)
                .textSelection(.enabled)
            Label(suggestionWhereWords(current), systemImage: suggestionStartsInWorktree(current) ? "arrow.triangle.branch" : "folder")
                .readingFont(.caption).foregroundStyle(Theme.text.tertiary.color).lineLimit(1)
            if showsOrigin, let origin = current.origin_conversation_id {
                Button {
                    model.focus(origin)
                } label: {
                    Text("From \(model.state.suggestionOrigin(current) ?? "another conversation")")
                }
                .buttonStyle(.link).readingFont(.caption)
                .help("Open the conversation that suggested it")
            }
            brief(current)
            actions(current, acting: acting)
        }
        .readingFont(.body)
        .padding(Theme.space.inset * 1.5)
        .background(RoundedRectangle(cornerRadius: Theme.radius.card).fill(Theme.surface.raised.color))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius.card).strokeBorder(Theme.line.hairline))
        .confirmationDialog(confirming.map { "Start with \($0.label)?" } ?? "",
                            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                            presenting: confirming) { policy in
            Button("Start with \(policy.label)") { model.startSuggestion(current, permission: policy, confirmedWiden: true) }
            Button("Cancel", role: .cancel) {}
        } message: { policy in
            Text(policy == .bypass ? "The agent will act without asking, from its first message."
                 : "The agent will edit files without asking, from its first message.")
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Suggested conversation: \(current.title)")
    }

    @ViewBuilder
    private func brief(_ current: Suggestion) -> some View {
        let expanded = briefExpanded ?? briefInitiallyExpanded
        VStack(alignment: .leading, spacing: Theme.space.step) {
            if expanded {
                MarkdownView(text: current.brief)
                    .textSelection(.enabled)
                    .padding(Theme.space.inset)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(Theme.surface.conversation.color))
                if current.brief_truncated {
                    Text("The first \(current.brief.count) of \(current.brief_chars) characters").readingFont(.caption)
                        .foregroundStyle(Theme.text.tertiary.color)
                }
            }
            Button(expanded ? "Hide the brief" : "Show the brief") { briefExpanded = !expanded }
                .buttonStyle(.link).readingFont(.caption)
                .help("The brief is the new conversation's first message")
        }
        .task(id: expanded && current.brief_truncated ? current.suggestion_id : nil) {
            if expanded && current.brief_truncated { model.loadSuggestionBrief(current.suggestion_id) }
        }
    }

    @ViewBuilder
    private func actions(_ current: Suggestion, acting: Bool) -> some View {
        switch current.state {
        case "open":
            HStack(spacing: Theme.space.inset) {
                Spacer()
                if acting { ProgressView().controlSize(.small) }
                Button("Dismiss") { model.dismissSuggestion(current) }.buttonStyle(.bordered)
                    .help("Hide it; nothing starts")
                startButtons(current)
            }
            .disabled(acting || !model.canSuggest)
        case "starting":
            HStack(spacing: Theme.space.inset) {
                if current.start_error == nil || acting { ProgressView().controlSize(.small) }
                Text(suggestionStateWords(current) ?? "Starting…").readingFont(.caption)
                    .foregroundStyle(current.start_error == nil ? Theme.text.secondary.color : Theme.state.error)
                Spacer()
                if current.start_error != nil {
                    Button("Start again") { model.startSuggestion(current) }.buttonStyle(.borderedProminent)
                        .disabled(acting)
                }
            }
        case "started":
            if let started = current.started_conversation_id {
                Button { model.focus(started) } label: {
                    Label("Open the conversation", systemImage: "arrow.right.circle")
                }
                .buttonStyle(.link).readingFont(.caption)
            }
        default:
            EmptyView()
        }
    }

    @ViewBuilder
    private func startButtons(_ current: Suggestion) -> some View {
        let offered = suggestionStartPermissions(provider: current.provider,
                                                 capabilities: model.state.availability.capabilities)
        let first = offered.first ?? .ask
        Button("Start") { model.startSuggestion(current, permission: first) }
            .buttonStyle(.borderedProminent)
            .help(first == .readOnly ? "Start it read-only, with the brief as its first message"
                  : "Start it in Ask, with the brief as its first message")
        let wider = offered.filter { PermissionPolicy.widens(from: first.rawValue, to: $0.rawValue) }
        if !wider.isEmpty {
            Menu {
                ForEach(wider, id: \.self) { policy in
                    Button("Start with \(policy.label)…") { confirming = policy }
                }
            } label: {
                Image(systemName: "chevron.down")
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("Start with a wider permission")
            .accessibilityLabel("Start with another permission")
        }
    }
}

/// A row of the sidebar's Suggested section: its title, and where it came from.
struct SuggestionSidebarRow: View {
    let suggestion: Suggestion
    let origin: String?
    @Environment(\.textScale) private var scale

    var body: some View {
        HStack(spacing: Theme.space.inset) {
            Image(systemName: "plus.bubble").foregroundStyle(Theme.text.secondary.color)
            VStack(alignment: .leading, spacing: 1) {
                Text(suggestion.title).windowFont(.sidebar).lineLimit(1)
                Text(origin.map { "From \($0)" } ?? suggestion.workspace.map { abbreviatedPath($0) } ?? "No folder")
                    .windowFont(.heading).foregroundStyle(Theme.text.tertiary.color).lineLimit(1)
            }
            Spacer(minLength: Theme.space.step)
            if suggestion.isStarting { ProgressView().controlSize(.mini).accessibilityLabel("Starting") }
        }
        .frame(height: 46 * max(1, scale))
        .accessibilityElement(children: .combine)
    }
}

/// The detail column for a suggestion picked in the sidebar: its card, whole.
struct SuggestionDetailView: View {
    @ObservedObject var model: UIModel
    let suggestion: Suggestion

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.space.reply) {
                SuggestionCardView(model: model, suggestion: suggestion, showsOrigin: true, briefInitiallyExpanded: true)
            }
            .padding(Theme.space.column)
            .readingColumn()
            .frame(maxWidth: .infinity)
        }
        .background(Theme.surface.conversation.color)
    }
}

/// The sidebar's Suggested heading: its title and how many wait on the person.
struct SuggestedHeader: View {
    let count: Int

    var body: some View {
        HStack {
            Text("Suggested").windowFont(.heading).foregroundStyle(Theme.text.tertiary.color)
            Spacer()
            Text("\(count)").windowFont(.heading).foregroundStyle(Theme.text.secondary.color)
                .padding(.horizontal, 6).padding(.vertical, 1)
                .background(Capsule().fill(Theme.surface.raised.color))
                .accessibilityLabel("\(count) suggested conversation\(count == 1 ? "" : "s") waiting")
        }
    }
}
#endif
