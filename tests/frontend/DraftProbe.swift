// Foundation-only probe of the same draft state the window binds to.
import Foundation

@main
struct DraftProbe {
    static func main() throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let input = try JSONValue.parse(data)
        var draft = NewConversationDraft(workspace: input["remembered_workspace"]?.string)
        var snapshots: [[String: Any]] = []
        for step in input["steps"]?.array ?? [] {
            if let text = step["text"]?.string { draft.text = text }
            if step["workspace"] != nil { draft.workspace = step["workspace"]?.string }
            if let provider = step["provider"]?.string { draft.provider = provider }
            if let settings = step["settings"] { draft.settings = try settings.decode(ConversationSettings.self) }
            if let attachments = step["attachments"] { draft.attachments = try attachments.decode([StagedAttachment].self) }
            if let confirm = step["confirm_widen"]?.bool { draft.confirmWiden = confirm }
            if let submitting = step["submitting"]?.bool { draft.isSubmitting = submitting }
            if let models = step["models"] {
                draft.reconcile(models: try models.decode([ModelEntry].self),
                                capabilities: try step["capabilities"]?.decode(Capabilities.self))
            }
            switch step["action"]?.string {
            case "open": draft.open()
            case "leave": draft.leave()
            case "journaled": draft.journaled()
            case "restore":
                draft = try JSONDecoder().decode(NewConversationDraft.self, from: JSONEncoder().encode(draft))
                draft.restore()
            default: break
            }
            var snapshot = try JSONSerialization.jsonObject(with: JSONEncoder().encode(draft)) as! [String: Any]
            snapshot["can_send"] = draft.canSend
            snapshot["workspace"] = draft.workspace as Any? ?? NSNull()
            snapshots.append(snapshot)
        }
        let output = try JSONSerialization.data(withJSONObject: snapshots, options: [.sortedKeys])
        FileHandle.standardOutput.write(output)
    }
}
