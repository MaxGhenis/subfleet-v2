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
                var request = container.isEmpty ? value : .object([container: value])
                if container == "input" { request = .object(["tool_name": .string("Bash"), "input": value]) }
                hidden[container + "." + key] = fields(request)
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
        // claude_turn writes an absent blocked_path as null in every summary.
        var question = ApprovalCard(requestID: "r", approvalID: "q", kind: "question",
            display: ApprovalDisplay(fields: ["questions": questionJSON, "input": .string("{\"questions\":[{\"question\":\"Which scope?\"}]}"),
                                              "blocked_path": .null]),
            options: ["answer", "deny"], state: .pending)
        let summaryInput: JSONValue = .object(["questions": questionJSON, "newGrant": .string("/future/root")])
        var objectQuestion = question
        objectQuestion.display.fields["input"] = summaryInput
        var stringQuestion = question
        stringQuestion.display.fields["input"] = .string(String(data: try JSONEncoder().encode(summaryInput), encoding: .utf8)!)
        func questionSummaryFields(_ card: ApprovalCard) -> [String: String] {
            Dictionary(uniqueKeysWithValues: ApprovalPresentation.grantedFields(card, request: nil).map { ($0.key, $0.value) })
        }
        let questionRequest: JSONValue = .object(["input": .object(["questions": questionJSON]), "blocked_path": .null])
        var timeline = Timeline(conversationID: "c")
        timeline.attach(approvals: [ApprovalView(approval_id: "q", message_id: "m", conversation_id: "c", provider_request_id: "r",
            kind: "question", display: question.display, options: question.options, created_at: "2026-10-05T10:00:00Z", state: "pending")])
        timeline.noteApprovalAnswer(approvalID: "q", answers: ["Which scope?": "Local"])
        _ = timeline.apply(events: [ConversationEvent(seq: 1, message_id: "m", kind: "approval.resolved",
            data: .object(["request_id": .string("r"), "decision": .string("answer")]))])
        var resetTimeline = timeline
        resetTimeline.resetEvents()
        _ = resetTimeline.apply(events: [
            ConversationEvent(seq: 1, message_id: "m", kind: "approval.requested",
                data: .object(["request_id": .string("r"), "kind": .string("question"), "questions": questionJSON,
                               "options": .array([.string("answer"), .string("deny")])])),
            ConversationEvent(seq: 2, message_id: "m", kind: "approval.resolved",
                data: .object(["request_id": .string("r"), "decision": .string("answer")]))
        ])
        let replayedQuestion = resetTimeline.items.compactMap(\.card).first!
        question = timeline.items.compactMap(\.card).first!
        // Round-three review: Codex 0.159 `kind` (command | writeStdin), parsed actions, array order, cd preludes.
        let stdinParams: JSONValue = .object(["threadId": .string("t"), "turnId": .string("u"), "itemId": .string("i"),
            "startedAtMs": .int(1), "command": .string("/bin/zsh -lc 'python3 manage.py migrate'"),
            "cwd": .string("/repo"), "kind": .string("writeStdin")])
        let commandCard = ApprovalCard(approvalID: "s", kind: "command",
            display: ApprovalDisplay(fields: ["command": .string("/bin/zsh -lc 'python3 manage.py migrate'"),
                                              "cwd": .string("/repo"), "input_kind": .string("writeStdin")]),
            options: ["allow", "deny"], state: .pending)
        func fieldMap(_ card: ApprovalCard, _ request: JSONValue?) -> [String: String] {
            Dictionary(uniqueKeysWithValues: ApprovalPresentation.grantedFields(card, request: request).map { ($0.key, $0.value) })
        }
        let actions = JSONValue.array((0..<12).map { i in .object(["type": .string("read"), "command": .string("sed -n 1p part\(i).txt"),
                                                                      "name": .string("part\(i).txt"), "path": .string("/repo/part\(i).txt")]) })
        let chain: JSONValue = .object(["method": .string("item/commandExecution/requestApproval"), "params": .object([
            "command": .string("/bin/zsh -lc 'sed -n 1p part0.txt && sed -n 1p part1.txt'"), "cwd": .string("/repo"),
            "kind": .string("command"), "commandActions": actions])])
        let roots: JSONValue = .object(["params": .object(["cwd": .string("/repo"), "permissions": .object(["fileSystem": .object([
            "write": .array((0..<12).map { .string("/repo/out/\($0)") })])])])])
        let review: [String: Any] = [
            "write_stdin_fields": fieldMap(commandCard, .object(["method": .string("item/commandExecution/requestApproval"), "params": stdinParams])),
            "write_stdin_summary": fieldMap(commandCard, nil),
            "chain_order": ApprovalPresentation.grantedFields(commandCard, request: chain).map(\.key),
            "roots_order": ApprovalPresentation.grantedFields(card, request: roots).map(\.key),
            "label_cd_semicolon": ToolActivity(name: "command", summary: "/bin/zsh -lc 'cd app; rm -rf build && make'",
                                               hidden: false, state: .running).label,
        ]
        let schemaRequests = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))).array!
        let schemaFields = Dictionary(uniqueKeysWithValues: schemaRequests.map { fixture in
            let card = ApprovalCard(approvalID: fixture["id"]!.string!, kind: fixture["kind"]!.string!,
                display: ApprovalDisplay(fields: fixture["display"]!.object!), options: ["allow", "deny"], state: .pending)
            return (fixture["id"]!.string!, fieldMap(card, fixture["request"]!))
        })
        let nestedMetadata: JSONValue = .object([
            "futureGrant": .object(["approvalId": .string("grant-callback"),
                "commandActions": .array([.object(["path": .string("/future/root")])])]),
            "input": .object(["kind": .string("future-kind")]),
            "params": .object(["futureGrant": .object(["startedAtMs": .string("grant-value")])])
        ])
        let edgeCommands = ["cd app; rm -rf build && make", "cd app || exit 1\nbun test && bun run build",
            "(cd app && swift build)", "set -euo pipefail\ncd app && swift build", "cd 'a;b' && rm -rf build",
            "cd app || rm -rf build", "set -- dangerous; rm -rf build", "cd app\nrm -rf build && make"]
        let edgeLabels = Dictionary(uniqueKeysWithValues: edgeCommands.map { ($0, ShellCommandPresentation.label($0)) })
        print(String(data: try JSONSerialization.data(withJSONObject: [
            "commands": commandRows, "wrappers": wrappers, "hidden": hidden, "display_hidden": displayHidden,
            "empty_amendments": emptyAmendments,
            "order": ApprovalPresentation.grantedFields(card, request: orderRequest).map(\.key), "fields": fields(orderRequest),
            "question_fields": ApprovalPresentation.grantedFields(question, request: questionRequest).map(\.key),
            "question_summary_fields": ApprovalPresentation.grantedFields(question, request: nil).map(\.key),
            "question_object_input_fields": questionSummaryFields(objectQuestion),
            "question_string_input_fields": questionSummaryFields(stringQuestion),
            "answers": question.answers, "question_pending": question.isPending,
            "reset_answers": replayedQuestion.answers, "reset_approval_id": replayedQuestion.approvalID!,
            "reset_question_pending": replayedQuestion.isPending, "review_r3": review,
            "schema_requests": schemaFields, "nested_metadata": fields(nestedMetadata), "edge_labels": edgeLabels
        ]), encoding: .utf8)!)
    }
}
