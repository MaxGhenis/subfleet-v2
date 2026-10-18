// A display projection only: never mutates event order, receipts or routing.
import Foundation

extension ToolActivity {
    /// Claude descriptions are retained as `description:` in the public summary.
    /// Older recordings without one retain a useful verb and public input detail.
    var label: String {
        if hidden { return "Credential access (hidden)" }
        let lines = summary.components(separatedBy: "\n")
        let json = summary.data(using: .utf8).flatMap({ try? JSONValue.parse($0) })
        if name.lowercased() == "command" {
            let command = json?["command"]?.string ?? (summary.hasPrefix("command: ") ? String(summary.dropFirst(9)) : summary)
            return ShellCommandPresentation.label(command)
        }
        if let json,
           let description = json["description"]?.string, !description.isEmpty { return description }
        // Summary metadata follows the command. A description inside a heredoc
        // is command input, even when truncation leaves it as the last line.
        var heredoc: String?
        let heredocPattern = try? NSRegularExpression(pattern: "<<-?\\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
        for (index, line) in lines.enumerated() {
            if let delimiter = heredoc {
                if line.trimmingCharacters(in: .whitespaces) == delimiter { heredoc = nil }
                continue
            }
            if index == lines.count - 1, line.hasPrefix("description: ") {
                return String(line.dropFirst("description: ".count))
            }
            let range = NSRange(line.startIndex..., in: line)
            if let match = heredocPattern?.firstMatch(in: line, range: range),
               let token = Range(match.range(at: 1), in: line) { heredoc = String(line[token]) }
        }
        func field(_ key: String) -> String? {
            json?[key]?.string ?? lines.first { $0.hasPrefix(key + ": ") }.map { String($0.dropFirst(key.count + 2)) }
        }
        let file = lines.first(where: { $0.hasPrefix("file_path: ") || $0.hasPrefix("path: ") || $0.hasPrefix("notebook_path: ") })
            .map { line in String(line[line.range(of: ": ")!.upperBound...]) }
        let filename = file.map { URL(fileURLWithPath: $0).lastPathComponent }
        switch name.lowercased() {
        case "bash", "shell", "exec_command", "commandexecution": return "Run a command"
        case "read", "read_file": return filename.map { "Read \($0)" } ?? "Read a file"
        case "edit", "write", "apply_patch", "filechange":
            let files = file?.components(separatedBy: ", ").map { URL(fileURLWithPath: $0).lastPathComponent } ?? []
            if files.count > 1 { return "Edit \(files.count) files (\(files.joined(separator: ", ")))" }
            return filename.map { "Edit \($0)" } ?? "Edit files"
        case "glob": return field("pattern").map { "Find files matching \($0)" } ?? "Find files"
        case "grep", "search":
            return "Search " + (filename ?? "files") + (field("pattern").map { " for \($0)" } ?? "")
        case "webfetch": return field("url").map { "Fetch \($0)" } ?? "Fetch a web page"
        case "websearch", "web search": return "Search the web" + (field("query").map { " for \($0)" } ?? "")
        default: return name
        }
    }
}

struct WorkGroup: Identifiable, Equatable {
    let id: String
    let items: [TimelineItem]
    let completed: Bool
    let duration: String?
    var outcome: String? = nil
    var tools: [ToolActivity] { items.compactMap { if case .tool(let t) = $0.content { return t }; return nil } }
    var failed: Int { tools.filter { $0.state == .failed }.count }
    var running: ToolActivity? { tools.last { $0.state == .running } }
    var count: Int { tools.count }
    var label: String {
        if completed {
            let verb = outcome == "failed" ? "Failed" : outcome == "interrupted" ? "Stopped" : outcome == "cancelled" ? "Withdrawn" : "Worked"
            return verb + (duration.map { (verb == "Worked" ? " for " : " after ") + $0 } ?? "") + " · \(count) step\(count == 1 ? "" : "s")"
        }
        if tools.isEmpty { return "Work details" }
        let names = Set(tools.map { $0.name.lowercased() })
        if names.isSubset(of: ["bash", "shell", "exec_command", "commandexecution", "command"]) {
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
                                               completed: true, duration: duration(turn), outcome: turn.state)))
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


extension TurnTimeline {
    /// Live status belongs to the bottom strip; settled outcomes stay by the message.
    /// Queued and steered messages still need their own delivery acknowledgment.
    var showsMessageAcknowledgment: Bool {
        (messageState.map(MessageState.terminal.contains) ?? false) || isReadSteer || isUnreadSteer || messageState == .queued || messageState == .steering
            || messageState == .deliveryUnknown || messageState == .unknown || (messageState == nil && state == "sending")
    }
}

extension WorkGroup {
    func tooltip(served: ServedChip?, expanded: Bool) -> String {
        let action = expanded ? "Hide work details" : "Show work details"
        guard completed, let served else { return action }
        let facts = [served.account, served.model, served.effort, served.fast]
            .compactMap { $0 }.filter { !$0.isEmpty } + served.warnings
        return ([action] + facts).joined(separator: " · ")
    }
}
