// Durable task suggestions and sidebar ancestry (DESIGN-chips.md).
import Foundation

struct TaskChip: Codable, Equatable, Identifiable {
    var chip_id: String
    var parent_conversation_id: String
    var message_id: String
    var title: String
    var tldr: String
    /// Events omit the prompt; open/list and mutation responses include it.
    var prompt: String?
    var cwd: String
    var state: String
    var child_conversation_id: String?
    var created_at: String
    var updated_at: String
    var dismissal_reason: String?

    var id: String { chip_id }
    var isPending: Bool { state == "pending" }

    /// An old event replay cannot undo the person's terminal decision.
    func merging(_ incoming: TaskChip) -> TaskChip {
        guard isPending || !incoming.isPending else { return self }
        var result = incoming
        result.prompt = incoming.prompt ?? prompt
        return result
    }
}

struct ChipSpawnArgs: Codable, Equatable {
    var conversation_id: String
    var message_id: String
    var host_token: String
    var request_id: String
    var title: String
    var tldr: String
    var prompt: String
    var cwd: String?
}
struct ChipListArgs: Codable, Equatable { var conversation_id: String }
struct ChipListResult: Codable, Equatable { var chips: [TaskChip] }
struct ChipStartArgs: Codable, Equatable { var chip_id: String }
struct ChipDismissArgs: Codable, Equatable {
    var chip_id: String
    var reason: String?
}
struct ChipResult: Codable, Equatable { var chip: TaskChip }
struct ChipStartResult: Codable, Equatable {
    var chip: TaskChip
    var conversation: Conversation
    var message: Receipt
}

extension Ops {
    static let chipSpawn = DaemonOperation<ChipSpawnArgs, ChipResult>(name: "chip.spawn")
    static let chipList = DaemonOperation<ChipListArgs, ChipListResult>(name: "chip.list")
    static let chipDismiss = DaemonOperation<ChipDismissArgs, ChipResult>(name: "chip.dismiss")
    static let chipStart = DaemonOperation<ChipStartArgs, ChipStartResult>(name: "chip.start")
}

extension ConversationEngine {
    func chips(conversationID: String) throws -> [TaskChip] {
        try client.call(Ops.chipList, ChipListArgs(conversation_id: conversationID)).chips
    }

    /// Both choices have the chip's stable idempotency key and survive restart.
    func chooseChip(_ chip: TaskChip, start: Bool) throws -> OutboxEntry {
        let entry = try outbox.enqueueChip(chip, start: start)
        if entry.state == .failed { try outbox.retry(entry.key) }
        return outbox.entry(entry.key) ?? entry
    }
}

extension ConversationStoreState {
    mutating func apply(chips: [TaskChip]) {
        for group in Dictionary(grouping: chips, by: \.parent_conversation_id) {
            var timeline = timelines[group.key] ?? Timeline(conversationID: group.key)
            timeline.attach(chips: group.value)
            timelines[group.key] = timeline
        }
    }
}

/// Roots retain recency order; descendants follow their visible parent even
/// when their cwd or date differs. Missing/filtered parents and cycles are roots.
func nestedSidebarEntries(_ entries: [SidebarEntry]) -> [SidebarEntry] {
    let byID = Dictionary(uniqueKeysWithValues: entries.map { ($0.id, $0) })
    var children: [String: [SidebarEntry]] = [:]
    var roots: [SidebarEntry] = []
    for entry in entries {
        if let parent = entry.parentID, parent != entry.id, byID[parent] != nil {
            children[parent, default: []].append(entry)
        } else {
            roots.append(entry)
        }
    }
    var seen: Set<String> = []
    var result: [SidebarEntry] = []
    func append(_ entry: SidebarEntry, depth: Int, root: SidebarEntry) {
        guard seen.insert(entry.id).inserted else { return }
        var row = entry
        row.depth = depth
        row.groupDate = root.date
        row.groupWorkspace = root.workspace
        result.append(row)
        for child in children[entry.id] ?? [] { append(child, depth: depth + 1, root: root) }
    }
    for root in roots { append(root, depth: 0, root: root) }
    // Defensive cycle recovery: show every row once, without recursive loops.
    for entry in entries where !seen.contains(entry.id) { append(entry, depth: 0, root: entry) }
    return result
}
