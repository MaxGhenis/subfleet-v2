// A display projection only: never mutates event order, receipts or routing.
import Foundation

extension ToolActivity {
    /// Claude descriptions are retained as `description:` in the public summary.
    /// Older recordings without one get a useful verb, never a shell command.
    var label: String {
        if hidden { return "Credential access (hidden)" }
        let lines = summary.components(separatedBy: "\n")
        if let description = lines.first(where: { $0.hasPrefix("description: ") }) {
            return String(description.dropFirst("description: ".count))
        }
        if let json = summary.data(using: .utf8).flatMap({ try? JSONValue.parse($0) }),
           let description = json["description"]?.string, !description.isEmpty { return description }
        let file = lines.first(where: { $0.hasPrefix("file_path: ") || $0.hasPrefix("path: ") || $0.hasPrefix("notebook_path: ") })
            .map { line in String(line[line.range(of: ": ")!.upperBound...]) }
        let filename = file.map { URL(fileURLWithPath: $0).lastPathComponent }
        switch name.lowercased() {
        case "bash", "shell", "exec_command", "commandexecution": return "Run a command"
        case "read", "read_file": return filename.map { "Read \($0)" } ?? "Read a file"
        case "edit", "write", "apply_patch", "filechange": return filename.map { "Edit \($0)" } ?? "Edit files"
        case "glob", "grep", "search": return "Search files"
        case "websearch", "webfetch": return "Search the web"
        default: return name
        }
    }
}

struct WorkGroup: Identifiable, Equatable {
    let id: String
    let items: [TimelineItem]
    let completed: Bool
    let duration: String?
    var tools: [ToolActivity] { items.compactMap { if case .tool(let t) = $0.content { return t }; return nil } }
    var failed: Int { tools.filter { $0.state == .failed }.count }
    var running: ToolActivity? { tools.last { $0.state == .running } }
    var count: Int { tools.count }
    var label: String {
        if completed {
            return "Worked" + (duration.map { " for \($0)" } ?? "") + " · \(count) step\(count == 1 ? "" : "s")"
        }
        if tools.isEmpty { return "Work details" }
        let names = Set(tools.map { $0.name.lowercased() })
        if names.isSubset(of: ["bash", "shell", "exec_command", "commandexecution"]) {
            return "Ran \(count) command\(count == 1 ? "" : "s")"
        }
        if names.isSubset(of: ["read", "read_file"]) { return "Read \(count) file\(count == 1 ? "" : "s")" }
        if names.isSubset(of: ["edit", "write", "apply_patch", "filechange"]) { return "Edited \(count) file\(count == 1 ? "" : "s")" }
        return "Used \(count) tool\(count == 1 ? "" : "s")"
    }
}

enum ProgressRow: Identifiable, Equatable {
    case item(TimelineItem)
    case work(WorkGroup)
    var id: String { switch self { case .item(let item): return item.id; case .work(let group): return group.id } }
}

enum WorkPresentation {
    static func isWork(_ item: TimelineItem) -> Bool {
        switch item.content {
        case .tool: return true
        case .thinking(let text, _): return !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        default: return false
        }
    }
    static func isEmptyThinking(_ item: TimelineItem) -> Bool {
        if case .thinking(let text, _) = item.content { return text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        return false
    }
    static func duration(_ turn: TurnTimeline) -> String? {
        let stamps = (turn.phases.compactMap(\.ts) + turn.items.compactMap(\.ts) + [turn.completedTS].compactMap { $0 }).compactMap(parseTimestamp)
        guard let start = stamps.min(), let end = stamps.max() else { return nil }
        let seconds = max(0, Int(end.timeIntervalSince(start)))
        return seconds < 60 ? "\(seconds)s" : String(format: "%dm %02ds", seconds / 60, seconds % 60)
    }
    static func rows(in timeline: Timeline) -> [ProgressRow] {
        let items = timeline.items.filter { !isEmptyThinking($0) }
        let finished = timeline.turns.filter { $0.value.messageState.map(MessageState.terminal.contains) ?? false }
        var emitted: Set<String> = []
        var rows: [ProgressRow] = []
        var pending: [TimelineItem] = []
        func flush() {
            guard let first = pending.first else { return }
            rows.append(.work(WorkGroup(id: "work:" + first.id, items: pending, completed: false, duration: nil)))
            pending = []
        }
        for item in items {
            if isWork(item), let id = item.messageID, let turn = finished[id] {
                flush()
                if emitted.insert(id).inserted {
                    rows.append(.work(WorkGroup(id: "work:\(id)", items: items.filter { $0.messageID == id && isWork($0) },
                                               completed: true, duration: duration(turn))))
                }
            } else if isWork(item) {
                if pending.last?.messageID != item.messageID { flush() }
                pending.append(item)
            } else {
                flush()
                rows.append(.item(item))
            }
        }
        flush()
        return rows
    }
}
