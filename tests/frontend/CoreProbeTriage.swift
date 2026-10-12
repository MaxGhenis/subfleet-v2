// The sidebar's triage (unread, pinned, archived, Unread first) and the new
// conversation's Worktree toggle, in the app's core: the store's fold of the list
// and the watch feed, the marks saved and read back, the draft's kind and the
// requests it builds. Foundation only.
import Foundation

func triageProbeCommand(_ arguments: [String]) throws -> Any? {
    switch arguments[1] {
    case "triage":
        // triage <input.json> <marks.json>: steps over the store; snapshots of the sidebar
        return try runTriage(readFile(arguments[2]), marksPath: arguments[3])
    case "worktree-draft":
        // worktree-draft <input.json> <scratch dir>: steps over a new draft; snapshots and requests
        return try runWorktreeDraft(readFile(arguments[2]), scratch: URL(fileURLWithPath: arguments[3]))
    case "git-checkout":
        // git-checkout <folder>...: the checkout each is in, as the toggle finds it
        return arguments.dropFirst(2).map { folder -> Any in
            gitCheckout(containing: folder).map { ["top": $0.top, "key": $0.key] } ?? NSNull()
        }
    default:
        return nil
    }
}

private let utc: Calendar = {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "UTC")!
    return calendar
}()

func triageSnapshot(_ state: ConversationStoreState, now: Date) -> [String: Any] {
    ["sections": state.sidebar(now: now, calendar: utc).map { section -> [String: Any] in
        ["id": section.id, "title": section.title,
         "entries": section.entries.map { ["id": $0.id, "unread": $0.unread, "pinned": $0.pinned, "archived": $0.archived] }]
    },
     "unread": state.conversations.map(\.conversation_id).filter(state.isUnread).sorted(),
     "focused": state.focusedConversationID as Any? ?? NSNull(),
     "focused_conversation": state.focusedConversation.map { jsonObject($0) } as Any? ?? NSNull(),
     "conversations": state.conversations.map(\.conversation_id),
     "marks": jsonObject(state.marks), "message_seqs": state.messageSeqs]
}

/// Steps: `list`, `watch`, `upsert` (a Conversation), `focus` (an id or null), `pin`,
/// `unpin`, `mark_read`, `unread_first`, `show_archived`, `grouping`, `reload` (save the
/// marks to the file and read them into a fresh store, as a relaunch does), and
/// `snapshot` (a label).
func runTriage(_ data: Data, marksPath: String) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let now = input["now"]?.string.flatMap(parseTimestamp) ?? Date(timeIntervalSince1970: 1_791_000_000)
    var state = ConversationStoreState()
    var snapshots: [String: Any] = [:]
    var log: [Any] = []
    let url = URL(fileURLWithPath: marksPath)
    for step in input["steps"]?.array ?? [] {
        if let list = step["list"] { state.apply(list: try list.decode(ConversationListResult.self)) }
        if let page = step["watch"] {
            if case .applied(let count) = state.apply(watch: try page.decode(WatchPage.self)) { log.append(count) }
        }
        if let one = step["upsert"] { state.upsert(try one.decode(Conversation.self)) }
        if let focus = step["focus"] { state.focus(focus.string) }
        if let id = step["pin"]?.string { state.marks.setPinned(id, true) }
        if let id = step["unpin"]?.string { state.marks.setPinned(id, false) }
        if let id = step["mark_read"]?.string { state.marks.markRead(id, through: state.messageSeqs[id]) }
        if let on = step["unread_first"]?.bool { state.marks.unreadFirst = on }
        if let on = step["show_archived"]?.bool { state.showArchived = on }
        if let grouping = step["grouping"]?.string { state.grouping = SidebarGrouping(rawValue: grouping) ?? .recency }
        if step["reload"]?.bool == true {
            try state.marks.save(to: url)
            var fresh = ConversationStoreState()
            fresh.marks = SidebarMarks.load(from: url)
            log.append(["reloaded_equal": fresh.marks == state.marks, "mode": fileMode(marksPath)])
            fresh.conversations = state.conversations
            fresh.messageSeqs = state.messageSeqs
            fresh.watchCursor = state.watchCursor
            fresh.showArchived = state.showArchived
            state = fresh
        }
        if let label = step["snapshot"]?.string { snapshots[label] = triageSnapshot(state, now: now) }
    }
    return ["snapshots": snapshots, "log": log]
}

/// What the outbox sends: the first call's op and arguments, then it stops (unavailable).
final class RecordingClient: DaemonCalling, @unchecked Sendable {
    var calls: [[String: Any]] = []
    func call<Args: Encodable, Result: Decodable>(_ op: DaemonOperation<Args, Result>, _ args: Args) throws -> Result {
        calls.append(["op": op.name, "args": jsonObject(args)])
        throw DaemonClientError.unavailable("recorded")
    }
}

func worktreeDraftSnapshot(_ draft: NewConversationDraft) -> [String: Any] {
    ["shows_toggle": draft.showsWorktreeToggle, "kind": draft.workspaceKind,
     "worktree": draft.worktree as Any? ?? NSNull(), "repository": draft.repository as Any? ?? NSNull(),
     "worktree_repository": draft.worktreeRepository as Any? ?? NSNull(),
     "checked_kind": draft.checkedKind as Any? ?? NSNull(), "can_start": draft.canStart,
     "workspace": draft.workspace as Any? ?? NSNull()]
}

/// Steps: `workspace` (a folder or null), `detect` (find its checkout and take the
/// remembered choice, as `UIModel.noteNewDraftRepository`), `pick` (the toggle),
/// `check` ({ok, kind?}: a folder check's answer for that kind, the draft's by
/// default), `start` (remember the choice in UserDefaults, as Start does), `memory`
/// (read the choices back from UserDefaults), `relaunch` (encode the draft, decode
/// it as the app does at launch, `restore()`), `decode_old` (a draft saved before the
/// toggle existed), `requests` (what Start sends: the folder check and the journaled
/// create, through a recording client) and `snapshot` (a label).
func runWorktreeDraft(_ data: Data, scratch: URL) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let suite = input["defaults_suite"]?.string ?? "org.maxghenis.subfleet.probe.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defaults.removePersistentDomain(forName: suite)
    var memory = WorktreeMemory(defaults: defaults)
    var draft = NewConversationDraft()
    draft.text = "Fix the build"
    draft.settings = try input["settings"]?.decode(ConversationSettings.self) ?? ConversationSettings(model: "opus")
    var snapshots: [String: Any] = [:]
    var log: [Any] = []
    for step in input["steps"]?.array ?? [] {
        if let folder = step["workspace"] {
            draft.workspace = folder.string
            draft.invalidateWorkspaceCheck()
        }
        if step["detect"]?.bool == true {
            let repository = draft.workspace.flatMap { gitCheckout(containing: $0)?.key }
            draft.noteRepository(repository, for: draft.workspace, remembered: memory.choice(for: repository))
        }
        if let on = step["pick"]?.bool { draft.pickWorktree(on) }
        if let check = step["check"], let folder = draft.resolvedWorkspace {
            draft.applyWorkspaceCheck(WorkspaceCheckResult(ok: check["ok"]?.bool ?? true), workspace: folder,
                                      provider: draft.provider, permission: draft.settings.permission,
                                      kind: check["kind"]?.string ?? draft.workspaceKind)
        }
        if step["start"]?.bool == true {
            memory.remember(draft)
            memory.save(to: defaults)
        }
        if step["memory"]?.bool == true {
            memory = WorktreeMemory(defaults: defaults)
            log.append(["memory": memory.choices])
        }
        if step["relaunch"]?.bool == true {
            var saved = try JSONDecoder().decode(NewConversationDraft.self, from: JSONEncoder().encode(draft))
            saved.restore()
            draft = saved
        }
        if let old = step["decode_old"] {
            var saved = try old.decode(NewConversationDraft.self)
            saved.restore()
            draft = saved
            log.append(["decoded_old": worktreeDraftSnapshot(draft)])
        }
        if step["requests"]?.bool == true, let folder = draft.resolvedWorkspace {
            let check = try JSONSerialization.jsonObject(with: DaemonClient.requestLine(
                op: Ops.workspaceCheck.name, id: "probe-check",
                args: WorkspaceCheckArgs(workspace: folder, provider: draft.provider, permission: draft.settings.permission,
                                         workspace_kind: draft.workspaceKind)))
            let client = RecordingClient()
            let journal = scratch.appendingPathComponent("outbox-\(UUID().uuidString).json")
            let engine = ConversationEngine(client: client, outbox: try Outbox(url: journal))
            let key = try engine.createConversation(provider: draft.provider, workspace: folder, settings: draft.settings,
                                                    workspaceKind: draft.workspaceKind, requestID: "app-probe-1")
            _ = try engine.send(conversation: key, text: draft.text, settings: draft.settings)
            _ = engine.pump()
            log.append(["check": check, "journaled": jsonObject(engine.outbox.entry("app-probe-1")?.create),
                        "sent": client.calls.first ?? [:]])
        }
        if let label = step["snapshot"]?.string { snapshots[label] = worktreeDraftSnapshot(draft) }
    }
    defaults.removePersistentDomain(forName: suite)
    return ["snapshots": snapshots, "log": log]
}
