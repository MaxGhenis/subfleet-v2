import Foundation

@main struct ReviewFixProbe {
    static func main() throws {
        let fixtures = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).array!
        let recorded = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))).array!
        var approvals: [[String: Any]] = []
        for fixture in fixtures {
            let card = ApprovalCard(approvalID: fixture["id"]!.string!, kind: fixture["kind"]!.string!,
                display: ApprovalDisplay(fields: ["description": fixture["headline"]!, "command": .string("unmasked provider fallback")]),
                options: ["allow", "deny"], state: .pending)
            approvals.append(["id": fixture["id"]!.string!, "headline": ApprovalPresentation.headline(card),
                "command": ApprovalPresentation.command(card, request: fixture["request"]) as Any? ?? NSNull()])
        }
        var outcomes: [[String: Any]] = []
        for (state, reason) in [("failed", "model-mismatch"), ("failed", "continued-elsewhere"),
                                ("interrupted", "person-stopped"), ("cancelled", "withdrawn"),
                                ("complete", "stop-too-late"), ("complete", "")] {
            let turn = TurnTimeline(messageID: "m", state: state, stateReason: reason.isEmpty ? nil : reason)
            outcomes.append(["state": state, "reason": reason, "visible": turn.showsMessageAcknowledgment, "text": turn.statusText])
        }
        var failedTimeline = Timeline(conversationID: "c")
        failedTimeline.apply(receipt: Receipt(message_id: "m", state: "failed", state_reason: "model-mismatch"))
        _ = failedTimeline.apply(events: [ConversationEvent(seq: 1, message_id: "m", kind: "tool.started",
            data: .object(["id": .string("tool"), "name": .string("command"), "summary": recorded[3]["command"]!]))])
        let failedGroups = WorkPresentation.rows(in: failedTimeline).compactMap { row -> String? in
            if case .work(let group) = row { return group.label }; return nil
        }
        let cases = [("command", recorded[7]["command"]!.string!), ("command", recorded[3]["command"]!.string!),
                     ("web search", "query: SwiftUI keyboard navigation"), ("Grep", "path: /repo/src\npattern: TODO"),
                     ("Glob", "pattern: **/*.swift"), ("WebFetch", "url: https://example.com/docs"),
                     ("edit", "path: /repo/a.swift, /repo/b.swift"),
                     ("Bash", "cat <<'EOF'\ndescription: Wrong label\nEOF"),
                     ("Bash", "cat <<'EOF'\ndescription: Wrong label\nEOF\ndescription: Write the notes")]
        let tools = cases.map { ToolActivity(name: $0.0, summary: $0.1, hidden: false, state: .running).label }
        let command = ToolActivity(name: "command", summary: recorded[3]["command"]!.string!, hidden: false, state: .running)
        let group = WorkGroup(id: "commands", items: [TimelineItem(id: "t", content: .tool(command))], completed: false, duration: nil)
        print(String(data: try JSONSerialization.data(withJSONObject: ["approvals": approvals, "outcomes": outcomes,
            "tools": tools, "command_group": group.label, "failed_groups": failedGroups]), encoding: .utf8)!)
    }
}
