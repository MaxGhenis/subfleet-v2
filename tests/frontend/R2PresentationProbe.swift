import Foundation

@main struct R2PresentationProbe {
    static func main() throws {
        let commands = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).array!
        let commandRows = commands.map { entry -> [String: Any] in
            let command = entry["command"]!.string!
            let activity = ToolActivity(name: "command", summary: command, hidden: false, state: .running)
            return ["script": ShellCommandPresentation.script(command), "label": activity.label]
        }
        let script = commands.last!["script"]!.string!
        let quoted = "'" + script.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let wrappers = ["/bin/zsh -c", "/bin/zsh -lc", "/bin/zsh -l -c", "/bin/bash -c", "/bin/bash -lc", "/bin/bash -l -c", "sh -c", "sh -lc", "sh -l -c"]
            .map { ShellCommandPresentation.script($0 + " " + quoted) }
        let card = ApprovalCard(approvalID: "a", kind: "tool", display: ApprovalDisplay(), options: ["allow", "deny"], state: .pending)
        func fields(_ request: JSONValue) -> [String: String] {
            Dictionary(uniqueKeysWithValues: ApprovalPresentation.grantedFields(card, request: request).map { ($0.key, $0.value) })
        }
        var hidden: [String: Any] = [:]
        for (container, keys) in [("", ApprovalPresentation.hiddenRequestKeys), ("params", ApprovalPresentation.hiddenParameterKeys),
                                   ("input", ApprovalPresentation.hiddenInputKeys)] {
            for key in keys {
                let value = JSONValue.object([key: .string("plumbing"), "newGrant": .string("/future/root")])
                hidden[container + "." + key] = fields(container.isEmpty ? value : .object([container: value]))
            }
        }
        var displayHidden: [String: Any] = [:]
        var emptyAmendments: [String: Any] = [:]
        for key in ApprovalPresentation.emptyAmendmentKeys {
            emptyAmendments[key] = fields(.object([key: .array([]), "newGrant": .array([])]))
        }
        for key in ApprovalPresentation.hiddenDisplayKeys {
            var summary = card
            summary.display = ApprovalDisplay(fields: [key: .string("summary copy"), "newGrant": .string("/future/root")])
            displayHidden[key] = Dictionary(uniqueKeysWithValues: ApprovalPresentation.grantedFields(summary, request: nil).map { ($0.key, $0.value) })
        }
        let orderRequest: JSONValue = .object([
            "input": .object(["content": .string("bulk text"), "command": .string("echo [MASKED]"), "file_path": .string("/etc/hosts")]),
            "cwd": .string("/repo"), "grantRoot": .string("/"),
            "permissions": .object(["fileSystem": .object(["write": .array([.string("/repo/output")])]), "network": .object(["enabled": .bool(true)])]),
            "futureGrant": .object(["method": .string("must stay visible")]), "nullable": .null
        ])
        let questions = [ApprovalQuestion(question: "Which scope?", options: [.init(label: "Local")])]
        let questionJSON = try JSONValue.from(questions)
        var question = ApprovalCard(requestID: "r", approvalID: "q", kind: "question",
            display: ApprovalDisplay(fields: ["questions": questionJSON, "input": .string("{\"questions\":[{\"question\":\"Which scope?\"}]}")]),
            options: ["answer", "deny"], state: .pending)
        let questionRequest: JSONValue = .object(["input": .object(["questions": questionJSON]), "blocked_path": .null])
        var timeline = Timeline(conversationID: "c")
        timeline.attach(approvals: [ApprovalView(approval_id: "q", message_id: "m", conversation_id: "c", provider_request_id: "r",
            kind: "question", display: question.display, options: question.options, created_at: "2026-10-05T10:00:00Z", state: "pending")])
        timeline.noteApprovalAnswer(approvalID: "q", answers: ["Which scope?": "Local"])
        _ = timeline.apply(events: [ConversationEvent(seq: 1, message_id: "m", kind: "approval.resolved",
            data: .object(["request_id": .string("r"), "decision": .string("answer")]))])
        question = timeline.items.compactMap(\.card).first!
        print(String(data: try JSONSerialization.data(withJSONObject: [
            "commands": commandRows, "wrappers": wrappers, "hidden": hidden, "display_hidden": displayHidden,
            "empty_amendments": emptyAmendments,
            "order": ApprovalPresentation.grantedFields(card, request: orderRequest).map(\.key), "fields": fields(orderRequest),
            "question_fields": ApprovalPresentation.grantedFields(question, request: questionRequest).map(\.key),
            "question_summary_fields": ApprovalPresentation.grantedFields(question, request: nil).map(\.key),
            "answers": question.answers, "question_pending": question.isPending
        ]), encoding: .utf8)!)
    }
}
