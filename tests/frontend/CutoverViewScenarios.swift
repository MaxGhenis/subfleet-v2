// Isolated first-use flows. Never start UIModel's timers, watch or event loops.
import AppKit
import SwiftUI

// Test preferences stay in memory; no ~/Library/Preferences writes.
final class CutoverDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

final class CutoverClient: DaemonCalling, @unchecked Sendable {
    let input: JSONValue
    private(set) var calls: [[String: Any]] = []
    init(_ input: JSONValue) { self.input = input }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let value = try JSONValue.parse(JSONEncoder().encode(args))
        let data = try JSONEncoder().encode(args)
        calls.append(["op": op.name, "args": try JSONSerialization.jsonObject(with: data)])
        if op.name == "workspace.check" {
            let path = value["workspace"]!.string!
            let result = input["checks"]?[path] ?? input["fallback_check"]!
            return try result.decode(R.self)
        }
        throw DaemonClientError.unavailable("Unexpected op in cutover probe: \(op.name)")
    }
}

@MainActor func cutoverSnapshot(_ model: UIModel) throws -> [String: Any] {
    var draft = try JSONSerialization.jsonObject(with: JSONEncoder().encode(model.newDraft)) as! [String: Any]
    draft["workspace"] = model.newDraft.workspace as Any? ?? NSNull()
    draft["can_start"] = model.newDraft.canStart
    draft["resolution"] = model.newDraft.startResolution
    draft["exists"] = model.newDraft.resolvedWorkspace.map { FileManager.default.fileExists(atPath: $0) } ?? false
    return ["draft": draft, "failed": model.failedDrafts.map { draft in
        ["id": draft.id, "text": draft.text, "reason": draft.failure.message,
         "message_ids": draft.messages.map(\.key)] as [String: Any]
    }, "selected": model.selectedFailedDraftID as Any? ?? NSNull(),
       "footer": model.problem as Any? ?? NSNull()]
}

@MainActor func runCutover(_ path: String) async throws -> [String: Any] {
    let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: path)))
    let root = URL(fileURLWithPath: input["root"]!.string!)
    let defaults = CutoverDefaults()
    defaults.set("claude", forKey: "providerChoice")
    defaults.set("accept-edits", forKey: "lastPermission")
    defaults.set(NSHomeDirectory(), forKey: "lastWorkspace")
    for (provider, model) in input["remembered"]?.object ?? [:] {
        defaults.set(model.string, forKey: "lastModel.\(provider)")
    }
    var state = ConversationStoreState()
    if let list = input["list"] { state.apply(list: try list.decode(ConversationListResult.self)) }
    for provider in ["claude", "codex"] {
        if let models = input["models"]?[provider] {
            state.apply(models: try models.decode(ModelsListResult.self), provider: provider)
        }
    }
    if let capabilities = input["availability"] { state.availability = .ready(try capabilities.decode(Capabilities.self)) }
    let client = CutoverClient(input)
    let model = UIModel(paths: .rooted(at: root), client: client, defaults: defaults, state: state)
    var snapshots: [[String: Any]] = [try cutoverSnapshot(model)]
    for step in input["steps"]?.array ?? [] {
        switch step["action"]?.string {
        case "open": model.openNewDraft()
        case "scratch": model.useNewScratchFolder()
        case "folder":
            model.newDraft.workspace = step["path"]!.string!
            model.validateNewDraftWorkspace()
        case "provider": model.selectNewDraftProvider(step["provider"]!.string!)
        case "start": model.sendNewDraft(stayHere: true)
        case "send-existing":
            model.send(conversationID: step["id"]!.string!, text: "Existing conversation message", staged: [],
                       settings: try step["settings"]!.decode(ConversationSettings.self))
        case "select-failure": model.selectFailedDraft(step["id"]?.string)
        case "change-failure": model.changeFailedDraftFolder(model.failedDrafts.first { $0.id == step["id"]?.string }!)
        case "discard": model.discardFailedDraft(step["id"]!.string!)
        default: break
        }
        if let text = step["text"]?.string { model.newDraft.text = text }
        if let confirm = step["confirm"]?.bool { model.newDraft.confirmWiden = confirm }
        // Every queue task has a visible terminal result, and is awaited before
        // the probe exits. No daemon, process or detached loop is started.
        for _ in 0..<500 {
            let done: Bool
            switch step["action"]?.string {
            case "discard": done = !model.failedDrafts.contains { $0.id == step["id"]?.string }
            case "start": done = !model.newDraft.isSubmitting && model.newDraft.text.isEmpty && model.newDraft.workspaceCheck != nil
            case "send-existing": done = defaults.string(forKey: "lastModel.claude") == step["settings"]?["model"]?.string
            default: done = !model.newDraft.isPresented || model.newDraft.workspaceCheck != nil
            }
            if done { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        snapshots.append(try cutoverSnapshot(model))
    }
    return ["snapshots": snapshots, "calls": client.calls,
            "remembered_workspace": defaults.string(forKey: "lastWorkspace") as Any? ?? NSNull(),
            "visible_windows": NSApp.windows.filter(\.isVisible).count]
}
