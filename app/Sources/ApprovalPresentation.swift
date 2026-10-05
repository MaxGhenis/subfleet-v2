// A reading projection only; the full request and decision handlers stay intact.
import Foundation

enum ApprovalPresentation {
    // Only these named envelope/summary fields are omitted. Apply each list
    // at its own container, so e.g. futureGrant.method is still visible.
    static let hiddenRequestKeys: Set<String> = [
        "agent_id", "classifier_approvable", "subtype", "tool_use_id", "tool_name",
        "display_name", "title", "description", "decision_reason", "decision_reason_type",
        "suppress_always_allow_rule", "requires_user_interaction", "method"
    ]
    static let hiddenParameterKeys: Set<String> = ["threadId", "turnId", "itemId", "startedAtMs", "kind", "reason"]
    static let hiddenInputKeys: Set<String> = ["description"]
    static let hiddenDisplayKeys: Set<String> = ["tool", "title", "description", "reason", "input_kind"]
    static let emptyAmendmentKeys: Set<String> = ["permission_suggestions", "execpolicy_amendment", "network_amendments",
        "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments"]

    static func headline(_ card: ApprovalCard) -> String {
        for value in [card.display.description, card.display.title, card.display.reason] {
            if let value, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return value }
        }
        switch card.kind {
        case "question": return "Question"
        case "command": return "Run a command?"
        case "file-change": return "Change files?"
        case "permissions": return "Grant permissions?"
        default: return "Use \(card.display.tool ?? "a tool")?"
        }
    }

    static func command(_ card: ApprovalCard, request: JSONValue?) -> String? {
        // Prefer the person-only, masked request over a provider's display summary.
        if let request {
            return request["params"]?["command"]?.string ?? request["input"]?["command"]?.string ?? request["command"]?.string
        }
        if let command = card.display.command { return command }
        if let input = card.display.fields["input"] {
            if let command = input["command"]?.string { return command }
            if let text = input.string,
               let json = text.data(using: .utf8).flatMap({ try? JSONValue.parse($0) }) {
                return json["command"]?.string
            }
        }
        return nil
    }

    private static func source(_ card: ApprovalCard, request: JSONValue?) -> JSONValue {
        if let request { return request }
        var fields = card.display.fields
        // Older summaries store input as a JSON string. Decode it before
        // filtering so answered questions don't repeat the entire question tree.
        if let text = fields["input"]?.string,
           let input = text.data(using: .utf8).flatMap({ try? JSONValue.parse($0) }), input.object != nil {
            fields["input"] = input
        } else if card.kind == "question", fields["input"]?.string != nil {
            // Truncated string summaries can repeat the question tree without
            // valid JSON. Native input values still contain grant fields.
            fields.removeValue(forKey: "input")
        }
        return .object(fields)
    }

    static func commandFieldKey(_ card: ApprovalCard, request: JSONValue?) -> String? {
        let value = source(card, request: request)
        for (key, command) in [("params.command", value["params"]?["command"]),
                               ("input.command", value["input"]?["command"]), ("command", value["command"])] {
            if let text = command?.string, !text.isEmpty { return key }
        }
        return nil
    }

    /// Exact masked grant values, scope first; unknown fields remain visible.
    /// The complete envelope is retained by the caller under Details.
    static func grantedFields(_ card: ApprovalCard, request: JSONValue?) -> [(key: String, value: String)] {
        let source = source(card, request: request)
        func childPath(_ path: String, key: String) -> String {
            if key.isEmpty || key.contains(".") || key.contains("[") || key.contains("]") {
                let encoder = JSONEncoder()
                encoder.outputFormatting = [.withoutEscapingSlashes]
                let quoted = String(data: try! encoder.encode(key), encoding: .utf8)!
                return path + "[" + quoted + "]"
            }
            return path.isEmpty ? key : path + "." + key
        }
        func flatten(_ value: JSONValue, path: String, ancestors: [String] = []) -> [(key: String, value: String, priority: Int)] {
            if value.isNull { return [] }
            if let object = value.object, !object.isEmpty {
                return object.keys.sorted().flatMap { key -> [(key: String, value: String, priority: Int)] in
                    let hidden: Set<String> = path.isEmpty
                        ? hiddenRequestKeys.union(hiddenDisplayKeys).union(hiddenParameterKeys)
                        : path == "params" ? hiddenParameterKeys : path == "input" ? hiddenInputKeys : []
                    if hidden.contains(key) { return [] }
                    if ["", "params"].contains(path), emptyAmendmentKeys.contains(key), object[key]?.array?.isEmpty == true { return [] }
                    if card.kind == "question", ["", "input"].contains(path), ["questions", "answers"].contains(key) { return [] }
                    return flatten(object[key]!, path: childPath(path, key: key), ancestors: ancestors + [key])
                }
            }
            if let array = value.array, !array.isEmpty {
                return array.enumerated().flatMap { flatten($0.element, path: "\(path)[\($0.offset)]", ancestors: ancestors) }
            }
            let keys = ancestors.map { $0.lowercased() }
            let priority: Int
            if keys.contains(where: { ["content", "old_string", "new_string", "diff", "patch"].contains($0) }) { priority = 7 }
            else if keys.contains(where: { $0.contains("network") }) { priority = 4 }
            else if keys.contains(where: { $0.contains("permission") || $0.contains("filesystem") || $0.contains("execpolicy") }) { priority = 3 }
            else if keys.contains(where: { $0.contains("root") || $0 == "directories" }) { priority = 2 }
            else if keys.contains("cwd") { priority = 1 }
            else if keys.contains(where: { ["path", "file_path", "blocked_path", "notebook_path"].contains($0) }) { priority = 0 }
            else if keys.contains("command") { priority = 5 }
            else { priority = 6 }
            return [(path, value.string == "" ? "\"\"" : value.displayText, priority)]
        }
        return flatten(source, path: "").sorted {
            $0.priority == $1.priority ? $0.key < $1.key : $0.priority < $1.priority
        }.map { ($0.key, $0.value) }
    }
}
