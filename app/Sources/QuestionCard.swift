import Foundation

/// A question's in-progress answer, kept while the person moves back and forth.
struct QuestionAnswerDraft: Equatable {
    var selectedOptionIndices: Set<Int> = []
    var usesOther = false
    var otherText = ""
    var skipped = false
}

/// Foundation-only state for the inline AskUserQuestion card. Nothing is sent
/// until all questions have an answer or an explicit Skip and the card submits.
struct QuestionCardState: Equatable {
    let questions: [ApprovalQuestion]
    private(set) var currentIndex = 0
    private(set) var drafts: [QuestionAnswerDraft]

    init(questions: [ApprovalQuestion]) {
        self.questions = questions
        self.drafts = questions.map { _ in QuestionAnswerDraft() }
    }

    var currentQuestion: ApprovalQuestion? {
        questions.indices.contains(currentIndex) ? questions[currentIndex] : nil
    }

    var currentAnswer: QuestionAnswerDraft? {
        drafts.indices.contains(currentIndex) ? drafts[currentIndex] : nil
    }

    var hasPrevious: Bool { currentIndex > 0 }
    var isLastQuestion: Bool { !questions.isEmpty && currentIndex == questions.count - 1 }
    var answeredCount: Int { questions.indices.filter { answer(at: $0) != nil }.count }
    var canContinue: Bool { isResolved(at: currentIndex) }
    var canSubmit: Bool { !questions.isEmpty && questions.indices.allSatisfy { isResolved(at: $0) } }

    /// AskUserQuestion's existing approval.respond wire format is one string
    /// per question. Multi-select labels retain the provider's option order.
    var answers: [String: String] {
        var result: [String: String] = [:]
        for index in questions.indices {
            if let answer = answer(at: index) { result[questions[index].question] = answer }
        }
        return result
    }

    /// The driver requires a nonempty map for `answer`. Declining every
    /// question is a deny, which returns control to the agent without stopping.
    var submissionDecision: String { answers.isEmpty ? "deny" : "answer" }

    @discardableResult
    mutating func selectOption(_ index: Int) -> Bool {
        guard let question = currentQuestion, let options = question.options,
              options.indices.contains(index) else { return false }
        drafts[currentIndex].skipped = false
        if question.multiSelect == true {
            if drafts[currentIndex].selectedOptionIndices.contains(index) {
                drafts[currentIndex].selectedOptionIndices.remove(index)
            } else {
                drafts[currentIndex].selectedOptionIndices.insert(index)
            }
        } else {
            drafts[currentIndex].selectedOptionIndices = [index]
            drafts[currentIndex].usesOther = false
        }
        return true
    }

    /// UI callers suppress shortcuts while a text field has focus.
    @discardableResult
    mutating func selectNumber(_ number: Int) -> Bool {
        guard (1...9).contains(number) else { return false }
        return selectOption(number - 1)
    }

    mutating func selectOther() {
        guard let question = currentQuestion else { return }
        drafts[currentIndex].skipped = false
        if question.multiSelect == true {
            drafts[currentIndex].usesOther.toggle()
        } else {
            drafts[currentIndex].selectedOptionIndices = []
            drafts[currentIndex].usesOther = true
        }
    }

    mutating func setOtherText(_ text: String) {
        guard let question = currentQuestion else { return }
        drafts[currentIndex].skipped = false
        drafts[currentIndex].usesOther = true
        drafts[currentIndex].otherText = text
        if question.multiSelect != true { drafts[currentIndex].selectedOptionIndices = [] }
    }

    mutating func skipCurrent() {
        guard currentQuestion != nil else { return }
        drafts[currentIndex].skipped = true
        drafts[currentIndex].selectedOptionIndices = []
        drafts[currentIndex].usesOther = false
    }

    @discardableResult
    mutating func advance() -> Bool {
        guard canContinue, currentIndex + 1 < questions.count else { return false }
        currentIndex += 1
        return true
    }

    @discardableResult
    mutating func goBack() -> Bool {
        guard hasPrevious else { return false }
        currentIndex -= 1
        return true
    }

    private func isResolved(at index: Int) -> Bool {
        guard drafts.indices.contains(index) else { return false }
        return drafts[index].skipped || answer(at: index) != nil
    }

    private func answer(at index: Int) -> String? {
        guard drafts.indices.contains(index) else { return nil }
        let draft = drafts[index]
        guard !draft.skipped else { return nil }
        var values = (questions[index].options ?? []).enumerated().compactMap { offset, option in
            draft.selectedOptionIndices.contains(offset) ? option.label : nil
        }
        if draft.usesOther {
            let other = draft.otherText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !other.isEmpty else { return nil }
            values.append(other)
        }
        return values.isEmpty ? nil : values.joined(separator: ", ")
    }
}
