// A reading projection only; the full request and decision handlers stay intact.
import Foundation

enum ApprovalPresentation {
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
            return request["input"]?["command"]?.string ?? request["command"]?.string
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
}
