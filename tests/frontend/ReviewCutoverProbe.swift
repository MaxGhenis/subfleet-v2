// First-use review regressions through UIModel with isolated storage and clients.
// Compiled with CutoverViewScenarios.swift for CutoverDefaults; no window or watch loop.
import AppKit

final class ReviewClient: DaemonCalling, @unchecked Sendable {
    let input: JSONValue
    var failChecks: Int
    var sequence: [JSONValue]
    var crashAfter: String?
    let socketClient: DaemonClient?
    private let lock = NSLock()
    private var recordedCalls: [[String: Any]] = []
    var calls: [[String: Any]] { lock.withLock { recordedCalls } }
    init(_ input: JSONValue) {
        self.input = input
        failChecks = input["fail_checks"]?.int ?? 0
        sequence = input["check_sequence"]?.array ?? []
        crashAfter = input["crash_after"]?.string
        socketClient = input["socket"]?.string.map { DaemonClient(transport: UnixSocketTransport(path: $0)) }
    }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let value = try JSONValue.parse(JSONEncoder().encode(args))
        lock.withLock { recordedCalls.append(["op": op.name, "args": value]) }
        if let socketClient {
            let result = try socketClient.call(op, args)
            if crashAfter == op.name { _exit(73) }
            return result
        }
        if let answer = input["answers"]?[op.name] { return try answer.decode(R.self) }
        if op.name == "workspace.check", let delay = input["check_delay_ms"]?.int {
            Thread.sleep(forTimeInterval: Double(delay) / 1000)
        }
        if op.name == "workspace.check" {
            if input["unknown_op_checks"]?.bool == true {
                throw DaemonClientError.daemon(DaemonError(code: 2, message: "unknown op workspace.check", fix: nil))
            }
            if failChecks > 0 {
                failChecks -= 1
                throw DaemonClientError.unavailable("the daemon is restarting")
            }
            if !sequence.isEmpty { return try sequence.removeFirst().decode(R.self) }
            let path = value["workspace"]!.string!
            return try (input["checks"]?[path] ?? input["fallback_check"]!).decode(R.self)
        }
        throw DaemonClientError.unavailable("not served by the review probe: \(op.name)")
    }
}

@MainActor var sendWaitMs: Double = -1

@MainActor func reviewSnapshot(_ model: UIModel, _ client: ReviewClient, root: URL) -> [String: Any] {
    let saved = root.appendingPathComponent("support/new-conversation-draft.json")
    let savedDraft = (try? JSONSerialization.jsonObject(with: Data(contentsOf: saved)) as? [String: Any])
    return [
        "text": model.newDraft.text, "provider": model.newDraft.provider,
        "provider_choice": model.newDraft.providerChoice as Any? ?? NSNull(),
        "model": model.newDraft.settings.model, "permission": model.newDraft.settings.permission,
        "workspace": model.newDraft.workspace as Any? ?? NSNull(),
        "check_ok": model.newDraft.workspaceCheck?.ok as Any? ?? NSNull(),
        "check_reason": model.newDraft.workspaceCheck?.reason as Any? ?? NSNull(),
        "can_start": model.newDraft.canStart, "resolution": model.newDraft.startResolution,
        "recovering": model.failedDraftKey as Any? ?? NSNull(),
        "failed": model.failedDrafts.map { ["id": $0.id, "text": $0.text, "messages": $0.messages.map(\.key)] as [String: Any] },
        "footer": model.problem as Any? ?? NSNull(), "saved_text": savedDraft?["text"] ?? NSNull(),
        "saved_workspace": savedDraft?["workspace"] ?? NSNull(),
        "saved_model": (savedDraft?["settings"] as? [String: Any])?["model"] ?? NSNull(),
        "scratch_workspace": model.newDraft.scratchWorkspace as Any? ?? NSNull(),
        "send_wait_ms": sendWaitMs,
        "check_calls": client.calls.filter { $0["op"] as? String == "workspace.check" }.count,
    ]
}

func reviewAvailability(_ name: String?, capabilities: Capabilities) -> DaemonAvailability {
    switch name {
    case "unknown": return .unknown
    case "down": return .down("the daemon is restarting")
    case "busy": return .busy("the daemon is serving its connection limit")
    case "incompatible": return .incompatible("Install matching releases")
    case "refused": return .refused("This endpoint is refused")
    default: return .ready(capabilities)
    }
}

@main struct ReviewCutoverProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let root = URL(fileURLWithPath: input["root"]!.string!)
        let defaults = CutoverDefaults()
        for (key, value) in input["defaults"]?.object ?? [:] { defaults.set(value.string, forKey: key) }
        var state = ConversationStoreState()
        if let list = input["list"] { state.apply(list: try list.decode(ConversationListResult.self)) }
        for provider in ["claude", "codex"] {
            if let models = input["models"]?[provider] {
                state.apply(models: try models.decode(ModelsListResult.self), provider: provider)
            }
        }
        if let capabilities = input["availability"] { state.availability = .ready(try capabilities.decode(Capabilities.self)) }
        if let name = input["initial_status"]?.string, let capabilities = state.availability.capabilities {
            state.availability = reviewAvailability(name, capabilities: capabilities)
        }
        let client = ReviewClient(input)
        var model = UIModel(paths: .rooted(at: root), client: client, defaults: defaults, state: state)
        var snapshots: [[String: Any]] = [reviewSnapshot(model, client, root: root)]
        for step in input["steps"]?.array ?? [] {
            switch step["action"]?.string {
            case "open": model.openNewDraft()
            case "type": model.newDraft.text = step["text"]!.string!
            case "problem": model.problem = step["text"]?.string
            case "change-failure": model.changeFailedDraftFolder(model.failedDrafts.first { $0.id == step["id"]?.string }!)
            case "reconcile": model.reconcileNewDraft()
            case "availability", "lose-ready":
                let capabilities = try input["availability"]!.decode(Capabilities.self)
                model.reviewSetAvailability(reviewAvailability(step["status"]?.string, capabilities: capabilities))
                if step["action"]?.string == "availability" { model.validateNewDraftWorkspace() }
            case "models":
                for provider in ["claude", "codex"] {
                    if let catalog = input["delayed_models"]?[provider] {
                        model.reviewApplyModels(try catalog.decode(ModelsListResult.self), provider: provider)
                    }
                }
                model.reconcileNewDraft()
            case "wait": try await Task.sleep(nanoseconds: UInt64(step["ms"]?.int ?? 1000) * 1_000_000)
            case "start-switch-retry":
                let checks = client.calls.filter { $0["op"] as? String == "workspace.check" }.count
                model.sendNewDraft(stayHere: true)
                for _ in 0..<100 where client.calls.filter({ $0["op"] as? String == "workspace.check" }).count == checks {
                    try await Task.sleep(nanoseconds: 5_000_000)
                }
                model.changeFailedDraftFolder(model.failedDrafts.first { $0.id == step["id"]?.string }!)
            case "folder":
                model.newDraft.workspace = step["path"]!.string!
                model.validateNewDraftWorkspace()
            case "discard": model.discardFailedDraft(step["id"]!.string!)
            case "start": model.sendNewDraft(stayHere: true)
            case "open-send", "send":
                if step["action"]?.string == "open-send" { model.openNewDraft() }
                try await Task.sleep(nanoseconds: 50_000_000)
                let started = Date()
                model.send(conversationID: step["id"]!.string!, text: "sent while the sheet checks", staged: [],
                           settings: try step["settings"]!.decode(ConversationSettings.self))
                for _ in 0..<1000 where !(model.engine?.outbox.entries.contains { $0.kind == .messageSubmit } ?? false) {
                    try await Task.sleep(nanoseconds: 5_000_000)
                }
                sendWaitMs = Date().timeIntervalSince(started) * 1000
            case "reload": model = UIModel(paths: .rooted(at: root), client: client, defaults: defaults, state: state)
            case "relaunch-pump":
                model.pump()
                // Wait for durable acknowledgements and the published refusals.
                // Failed creates deliberately retain their visibly unsent messages.
                for _ in 0..<300 {
                    try await Task.sleep(nanoseconds: 100_000_000)
                    let journalURL = root.appendingPathComponent("support/outbox.json")
                    let journal = try? JSONValue.parse(Data(contentsOf: journalURL))
                    let entries = journal?["entries"]?.array ?? []
                    let refused = Set(entries.filter { $0["kind"]?.string == "conversation.create"
                        && $0["state"]?.string == "failed" }.compactMap { $0["key"]?.string }.map { "draft:" + $0 })
                    let pending = entries.contains { entry in
                        ["queued", "sending"].contains(entry["state"]?.string ?? "")
                            && !refused.contains(entry["conversation"]?.string ?? "")
                    }
                    let publishedRefused = Set(model.failedDrafts.map { "draft:" + $0.id })
                    if !pending && publishedRefused == refused { break }
                    model.pump()
                }
            case "crash": _exit(73)
            default: break
            }
            for _ in 0..<300 {
                if !model.newDraft.isSubmitting && (!model.newDraft.isPresented || model.newDraft.workspaceCheck != nil) { break }
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            try await Task.sleep(nanoseconds: 100_000_000)
            if step["action"]?.string == "discard" {
                for _ in 0..<300 where model.failedDrafts.contains(where: { $0.id == step["id"]?.string }) {
                    try await Task.sleep(nanoseconds: 10_000_000)
                }
            }
            snapshots.append(reviewSnapshot(model, client, root: root))
        }
        let out: [String: Any] = ["snapshots": snapshots]
        print(String(data: try JSONSerialization.data(withJSONObject: out, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
