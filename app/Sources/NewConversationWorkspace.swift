import Foundation

/// Folder admission stays in the daemon, shared with `conversation.create`.
struct WorkspaceCheckArgs: Codable, Equatable {
    var workspace: String
    var provider: String
    var permission: String
    var workspace_kind = "in-place"
    var allow_main: Bool? = nil
}

struct WorkspaceCheckResult: Codable, Equatable {
    var ok: Bool
    var reason: String?
    var fix: String?
    var workspace: String?
}

extension Ops {
    static let workspaceCheck = DaemonOperation<WorkspaceCheckArgs, WorkspaceCheckResult>(name: "workspace.check")
}

extension ConversationEngine {
    func checkWorkspace(_ workspace: String, provider: String, permission: String) throws -> WorkspaceCheckResult {
        try client.call(Ops.workspaceCheck, WorkspaceCheckArgs(workspace: workspace, provider: provider,
                                                               permission: permission))
    }
}

/// Reserve a name without touching the filesystem; only Start creates it.
func scratchWorkspace(support: URL, now: Date = Date(), suffix: String = String(UUID().uuidString.prefix(6)).lowercased()) -> URL {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd"
    return support.appendingPathComponent("scratch", isDirectory: true)
        .appendingPathComponent("\(formatter.string(from: now))-\(suffix)", isDirectory: true)
}
