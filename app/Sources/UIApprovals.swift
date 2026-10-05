#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct ApprovalCardView: View {
    @ObservedObject var model: UIModel
    let conversationID: String
    let card: ApprovalCard
    let review: () -> Void
    @State private var sending = false
    @State private var detail: ApprovalDetail?
    @State private var loadFailed = false
    @State private var detailsExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: card.kind == "question" ? "questionmark.bubble.fill" : "hand.raised.fill")
                   .foregroundStyle(Theme.state.attention)
                Text(ApprovalPresentation.headline(card)).bold()
                Spacer()
                switch card.state {
                case .pending: EmptyView()
                case .answered(let decision):
                    Text(card.kind == "question" && decision == "deny" ? "Skipped" : "Answered")
                        .readingFont(.caption)
                case .withdrawn: Text("Withdrawn").readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                }
            }
            if card.kind == "question" {
                if card.isActionable {
                    QuestionCardView(model: model, conversationID: conversationID, card: card)
                        .disabled(detail == nil)
                } else {
                    ForEach(Array(card.questions.enumerated()), id: \.offset) { _, question in
                        VStack(alignment: .leading, spacing: Theme.space.step) {
                            Text(question.question).readingFont(.secondary)
                            if let answer = card.answers[question.question] {
                                Text("Answer: \(answer)").readingFont(.secondary)
                            }
                        }
                    }
                }
            }
            if !ApprovalPresentation.grantedFields(card, request: detail?.request).isEmpty {
                ScrollView {
                    ApprovalGrantView(card: card, request: detail?.request)
                }
                .frame(minHeight: 40, maxHeight: 240)
                .scrollIndicators(.visible)
                .modifier(RequestScrollFocus())
                .accessibilityLabel("Requested scope and content")
            }
            if card.isPending && detail == nil {
                if loadFailed {
                    Button("Reload request") { Task { await load() } }
                } else {
                    ProgressView("Loading the request…").controlSize(.small)
                }
            }
            if card.kind == "question" {
                requestDetails
            } else {
                if let detail, !detail.masked.isEmpty {
                    Label("Some values are masked. Allow opens details for review.", systemImage: "eye.slash")
                        .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                }
                if card.isActionable {
                    HStack {
                        Button("Add a note", action: review).buttonStyle(.link)
                        Spacer()
                        if card.options.contains("deny") {
                            Button("Deny") { Task { await respond("deny") } }.buttonStyle(.bordered)
                        }
                        if let allowing = card.options.first(where: { ["allow", "allow-turn"].contains($0) }) {
                            Button("Allow") { Task { await respond(allowing) } }.buttonStyle(.borderedProminent)
                                .disabled(detail == nil)
                        }
                        if sending { ProgressView().controlSize(.small) }
                    }.disabled(sending)
                }
                requestDetails
            }
            if card.isPending && !card.isActionable {
                Text("Waiting for the request to be identified…").readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
            }
        }
        .readingFont(.body)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(Theme.surface.raised.color))
        .task(id: card.approvalID ?? card.requestID) {
            if card.isPending { await load() }
        }
    }

    private func load() async {
        loadFailed = false
        guard let id = await model.approvalID(for: card, conversationID: conversationID),
              let fresh = await model.approvalDetail(id, reveal: false) else {
            loadFailed = true
            return
        }
        detail = fresh
    }

    private func respond(_ decision: String) async {
        guard !sending else { return }
        sending = true
        defer { sending = false }
        if detail == nil { await load() }
        guard let detail else { return }
        if decision != "deny" && !detail.masked.isEmpty {
            review()
            return
        }
        _ = await model.respond(detail, decision: decision, answers: nil, message: nil, reviewedMasked: false)
    }

    private var requestDetails: some View {
        DisclosureGroup("Details", isExpanded: $detailsExpanded) {
            ScrollView {
                Text(prettyApprovalRequest(detail?.request ?? .object(card.display.fields)))
                    .readingFont(.code, design: .monospaced)
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 240)
            .modifier(RequestScrollFocus())
            .accessibilityLabel("Raw request")
        }.readingFont(.secondary)
            .onChange(of: detailsExpanded) { _, expanded in
                if expanded && detail == nil && card.approvalID != nil { Task { await load() } }
            }
    }
}

/// Draft answers belong to this stable timeline row, independently of composer sends.
struct QuestionCardView: View {
    @ObservedObject var model: UIModel
    let conversationID: String
    let card: ApprovalCard
    @State private var state: QuestionCardState
    @State private var sending = false
    @State private var needsMaskedReview = false
    @State private var reviewedMasked = false
    @State private var reviewDetail: ApprovalDetail?
    @FocusState private var focus: Focus?
    private enum Focus: Hashable { case options, other }

    init(model: UIModel, conversationID: String, card: ApprovalCard) {
        self.model = model
        self.conversationID = conversationID
        self.card = card
        _state = State(initialValue: QuestionCardState(questions: card.questions))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let question = state.currentQuestion, let draft = state.currentAnswer {
                HStack {
                    Text("\(state.answeredCount) of \(state.questions.count) questions answered")
                    Spacer()
                    Text("\(state.currentIndex + 1) / \(state.questions.count)")
                }.readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                if let header = question.header { Text(header).readingFont(.caption, weight: .bold).foregroundStyle(Theme.text.secondary.color) }
                Text(question.question).readingFont(.subheading).textSelection(.enabled)
                Text(question.multiSelect == true ? "Choose all that apply" : "Choose one")
                    .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array((question.options ?? []).enumerated()), id: \.offset) { index, option in
                        optionButton(index, option: option, selected: draft.selectedOptionIndices.contains(index),
                                     multiple: question.multiSelect == true)
                    }
                    Button {
                        state.selectOther()
                        focus = state.currentAnswer?.usesOther == true ? .other : .options
                    } label: {
                        Label("Other", systemImage: selectionSymbol(draft.usesOther, multiple: question.multiSelect == true))
                            .frame(maxWidth: .infinity, alignment: .leading).padding(8)
                            .contentShape(Rectangle())
                    }.buttonStyle(QuietButtonStyle())
                        .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(Theme.surface.hover.color))
                        .accessibilityValue(draft.usesOther ? "Selected" : "Not selected")
                }
                .focusable().focused($focus, equals: .options)
                .onKeyPress(characters: CharacterSet(charactersIn: "123456789"), phases: .down) { press in
                    guard focus == .options, press.modifiers.isEmpty, !sending,
                          let number = Int(press.characters), state.selectNumber(number) else { return .ignored }
                    return .handled
                }
                if draft.usesOther {
                    TextField("Type your own answer here", text: Binding(
                        get: { state.currentAnswer?.otherText ?? "" }, set: { state.setOtherText($0) }), axis: .vertical)
                        .textFieldStyle(.roundedBorder).lineLimit(2...6).focused($focus, equals: .other)
                }
                if draft.skipped { Text("Skipped").readingFont(.caption).foregroundStyle(Theme.text.secondary.color) }
                Text("Use number keys 1–9 while the choices are focused.")
                    .readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                if needsMaskedReview {
                    if let detail = reviewDetail {
                        DisclosureGroup("Details") {
                            Text(prettyApprovalRequest(detail.request))
                                .readingFont(.code, design: .monospaced).textSelection(.enabled)
                            Button("Reveal masked values") {
                                Task {
                                    if let revealed = await model.approvalDetail(detail.approval.approval_id, reveal: true) {
                                        reviewDetail = revealed
                                        reviewedMasked = true
                                    }
                                }
                            }
                        }
                    }
                    Toggle("I have reviewed the masked values in this question", isOn: $reviewedMasked)
                        .readingFont(.secondary)
                }
                HStack {
                    Button("Back") { state.goBack(); focus = .options }.disabled(!state.hasPrevious)
                    Button("Skip") {
                        state.skipCurrent()
                        if !state.isLastQuestion { state.advance() }
                        focus = .options
                    }
                    Spacer()
                    if sending { ProgressView().controlSize(.small) }
                    if state.isLastQuestion {
                        Button("Submit answers") { Task { await submit() } }
                            .buttonStyle(.borderedProminent)
                            .disabled(!state.canSubmit || (needsMaskedReview && !reviewedMasked && state.submissionDecision == "answer"))
                    } else {
                        Button("Next") { state.advance(); focus = .options }
                            .buttonStyle(.borderedProminent).disabled(!state.canContinue)
                    }
                }
            } else {
                Text("No questions were supplied.").foregroundStyle(Theme.text.secondary.color)
                Button("Skip") { Task { await submitEmpty() } }
            }
        }.disabled(sending)
            .onChange(of: card.questions) { _, questions in
                if state.questions != questions { state = QuestionCardState(questions: questions) }
            }
    }

    private func optionButton(_ index: Int, option: ApprovalQuestion.Option, selected: Bool, multiple: Bool) -> some View {
        Button {
            state.selectOption(index)
            focus = .options
        } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: selectionSymbol(selected, multiple: multiple))
                if index < 9 { Text("\(index + 1)").monospacedDigit().foregroundStyle(Theme.text.secondary.color) }
                VStack(alignment: .leading, spacing: 4) {
                    Text(option.label).bold()
                    if let description = option.description { Text(description).readingFont(.secondary).foregroundStyle(Theme.text.secondary.color) }
                    if let preview = option.preview {
                        Text(preview).readingFont(.code, design: .monospaced).foregroundStyle(Theme.text.secondary.color)
                    }
                }
                Spacer(minLength: 0)
            }.padding(10).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }
        .buttonStyle(QuietButtonStyle())
        .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(selected ? Theme.surface.selected.color : Theme.surface.hover.color))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius.control).stroke(selected ? Theme.accent : Theme.clear))
        .accessibilityValue(selected ? "Selected" : "Not selected")
    }

    private func selectionSymbol(_ selected: Bool, multiple: Bool) -> String {
        multiple ? (selected ? "checkmark.square.fill" : "square") : (selected ? "largecircle.fill.circle" : "circle")
    }

    private func submit() async {
        guard state.canSubmit else { return }
        await respond(decision: state.submissionDecision, answers: state.answers)
    }

    private func submitEmpty() async { await respond(decision: "deny", answers: [:]) }

    private func respond(decision: String, answers: [String: String]) async {
        guard !sending else { return }
        sending = true
        defer { sending = false }
        guard let id = await model.approvalID(for: card, conversationID: conversationID),
              let detail = await model.approvalDetail(id, reveal: false) else { return }
        if decision == "answer" && !detail.masked.isEmpty && !reviewedMasked {
            reviewDetail = detail
            needsMaskedReview = true
            return
        }
        _ = await model.respond(detail, decision: decision, answers: decision == "answer" ? answers : nil,
                                message: decision == "deny" ? "Skipped the questions." : nil,
                                reviewedMasked: reviewedMasked)
    }
}

struct ApprovalGrantView: View {
    let card: ApprovalCard
    let request: JSONValue?
    var body: some View {
        VStack(alignment: .leading, spacing: Theme.space.step) {
            ForEach(ApprovalPresentation.grantedFields(card, request: request), id: \.key) { field in
                if field.key == ApprovalPresentation.commandFieldKey(card, request: request) {
                    ApprovalCommandView(command: field.value)
                } else {
                    Text("\(field.key): \(field.value)")
                        .readingFont(.code, design: .monospaced).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}

private struct RequestScrollFocus: ViewModifier {
    @FocusState private var focused: Bool
    func body(content: Content) -> some View {
        content.focusable().focused($focused)
            .overlay(RoundedRectangle(cornerRadius: Theme.radius.control)
                .stroke(focused ? Theme.accent : Theme.clear, lineWidth: 2))
    }
}

private struct ApprovalCommandView: View {
    let command: String?
    var body: some View {
        if let command, !command.isEmpty {
            Text(command).readingFont(.code, design: .monospaced).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(Theme.space.inset).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: Theme.radius.card).fill(Theme.surface.hover.color))
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
    @State private var note = ""
    @State private var sending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(ApprovalPresentation.headline(card)).readingFont(.subheading, weight: .bold)
            if let detail {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ApprovalGrantView(card: card, request: detail.request)
                        DisclosureGroup("Details") {
                            Text(prettyApprovalRequest(detail.request)).readingFont(.code, design: .monospaced)
                                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .frame(minHeight: 40, maxHeight: 240)
                .scrollIndicators(.visible)
                .modifier(RequestScrollFocus())
                .accessibilityLabel("Requested scope and content; Details contains the raw request")
                if !detail.masked.isEmpty && !revealed {
                    HStack {
                        Label("\(detail.masked.count) value(s) that look like secrets are masked",
                              systemImage: "eye.slash").readingFont(.secondary)
                        Spacer()
                        Button("Reveal") { Task { await load(reveal: true) } }
                    }.fixedSize(horizontal: false, vertical: true)
                    Toggle("I have reviewed the masked values", isOn: $confirmMasked).readingFont(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("approval.confirmMasked")
                }
                TextField("Note to the agent (optional)", text: $note)
                HStack {
                    Button("Cancel", role: .cancel, action: done).keyboardShortcut(.cancelAction)
                    Spacer()
                    ForEach(detail.approval.options.reversed(), id: \.self) { option in
                        Button(label(option)) { Task { await answer(option, detail: detail) } }
                            .accessibilityIdentifier("approval.option.\(option)")
                            .disabled(sending || !canChoose(option, detail: detail))
                            .buttonStyle(.bordered)
                            .tint(option == primaryOption(detail) ? Theme.accent : nil)
                    }
                }.fixedSize(horizontal: false, vertical: true)
            } else {
                ProgressView("Loading the request…")
            }
        }
        .readingFont(.body)
        .padding(18)
        .frame(width: 560)
        .frame(maxHeight: 400)
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
        return true
    }

    private func answer(_ option: String, detail: ApprovalDetail) async {
        sending = true
        defer { sending = false }
        if await model.respond(detail, decision: option, answers: nil, message: note.isEmpty ? nil : note,
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

}

#if SUBFLEET_VIEW_TEST
extension ApprovalSheet {
    /// Render the actual confirmation state without showing a window or
    /// depending on SwiftUI's unavailable offscreen accessibility tree.
    func confirmingMaskedValuesForSnapshot() -> Self {
        var view = self
        view._confirmMasked = State(initialValue: true)
        return view
    }
}
#endif

private func prettyApprovalRequest(_ value: JSONValue) -> String {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    return (try? encoder.encode(value)).flatMap { String(data: $0, encoding: .utf8) } ?? "\(value)"
}

#endif
