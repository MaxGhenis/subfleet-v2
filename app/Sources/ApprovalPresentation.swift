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

    /// Keep the exact, masked request's fields visible, including future fields.
    /// Flatten containers for reading; only the raw JSON belongs in Details.
    static func grantedFields(_ card: ApprovalCard, request: JSONValue?) -> [(key: String, value: String)] {
        let source = request ?? .object(card.display.fields)
        // Omit only the command actually shown in the command block. Other
        // command fields may describe additional scope and must remain visible.
        let commandPath: String? = {
            if request != nil, source["params"]?["command"]?.string != nil { return "params.command" }
            if request == nil, source["command"]?.string != nil { return "command" }
            if source["input"]?["command"]?.string != nil { return "input.command" }
            return source["command"]?.string != nil ? "command" : nil
        }()
        func childPath(_ path: String, key: String) -> String {
            if key.isEmpty || key.contains(".") || key.contains("[") || key.contains("]") {
                let encoder = JSONEncoder()
                encoder.outputFormatting = [.withoutEscapingSlashes]
                let quoted = String(data: try! encoder.encode(key), encoding: .utf8)!
                return path + "[" + quoted + "]"
            }
            return path.isEmpty ? key : path + "." + key
        }
        func flatten(_ value: JSONValue, path: String) -> [(key: String, value: String)] {
            if let object = value.object, !object.isEmpty {
                return object.keys.sorted().flatMap { key in
                    flatten(object[key]!, path: childPath(path, key: key))
                }
            }
            if let array = value.array, !array.isEmpty {
                return array.enumerated().flatMap { flatten($0.element, path: "\(path)[\($0.offset)]") }
            }
            if value.isNull { return [(path, "null")] }
            if path == commandPath, let text = value.string, !text.isEmpty { return [] }
            return [(path, value.string == "" ? "\"\"" : value.displayText)]
        }
        return flatten(source, path: "")
    }
}
