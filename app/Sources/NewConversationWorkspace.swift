import Foundation

/// Folder admission stays in the daemon, shared with `conversation.create`.
struct WorkspaceCheckArgs: Codable, Equatable {
    var workspace: String
    var provider: String
    var permission: String
    var workspace_kind: String? = nil
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
    func checkWorkspace(_ workspace: String, provider: String, permission: String,
                        kind: String = "in-place", allowMain: Bool? = nil) throws -> WorkspaceCheckResult {
        try client.call(Ops.workspaceCheck, WorkspaceCheckArgs(workspace: workspace, provider: provider,
                                                               permission: permission, workspace_kind: kind, allow_main: allowMain))
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

// MARK: - The Worktree toggle (a new conversation in its own worktree)

/// The git checkout holding a folder, as git's own discovery finds it, read from the
/// filesystem only (no git, no daemon call): the nearest folder at or above it with
/// a `.git` directory holding `HEAD`, or a `.git` file naming its git directory (a
/// linked worktree). `key` is the repository's common git directory, the same for
/// every linked worktree of it, so a choice remembered per repository covers them all.
/// The daemon decides for itself when the conversation is made (`not-a-repository`).
struct GitCheckout: Equatable {
    /// The working tree's top folder.
    var top: String
    /// The common git directory, its real path (`realpath(3)`, as git and the daemon
    /// spell folders; Foundation's symlink resolution drops `/private`).
    var key: String
}

private func realPath(_ url: URL) -> String {
    guard let resolved = realpath(url.path, nil) else { return url.standardizedFileURL.path }
    defer { free(resolved) }
    return String(cString: resolved)
}

func gitCheckout(containing folder: String, fileManager: FileManager = .default) -> GitCheckout? {
    var url = URL(fileURLWithPath: folder, isDirectory: true).standardizedFileURL
    while true {
        let dotGit = url.appendingPathComponent(".git")
        var isDirectory: ObjCBool = false
        if fileManager.fileExists(atPath: dotGit.path, isDirectory: &isDirectory) {
            if isDirectory.boolValue {
                if fileManager.fileExists(atPath: dotGit.appendingPathComponent("HEAD").path) {
                    return GitCheckout(top: url.path, key: realPath(dotGit))
                }
            } else if let text = try? String(contentsOf: dotGit, encoding: .utf8),
                      let line = text.split(whereSeparator: \.isNewline).first, line.hasPrefix("gitdir:") {
                let named = line.dropFirst("gitdir:".count).trimmingCharacters(in: .whitespaces)
                let gitdir = named.hasPrefix("/") ? URL(fileURLWithPath: named, isDirectory: true)
                    : url.appendingPathComponent(named, isDirectory: true)
                // A linked worktree's git directory names the shared one in `commondir`.
                var common = gitdir
                if let shared = try? String(contentsOf: gitdir.appendingPathComponent("commondir"), encoding: .utf8) {
                    let path = shared.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !path.isEmpty {
                        common = path.hasPrefix("/") ? URL(fileURLWithPath: path, isDirectory: true)
                            : gitdir.appendingPathComponent(path, isDirectory: true)
                    }
                }
                return GitCheckout(top: url.path, key: realPath(common))
            }
        }
        let parent = url.deletingLastPathComponent()
        if parent.path == url.path { return nil }
        url = parent
    }
}

/// The Worktree toggle's last choice per repository (`GitCheckout.key`), kept when a
/// conversation starts: a new draft in that repository starts from it.
struct WorktreeMemory: Equatable {
    static let defaultsKey = "worktreeByRepository"
    var choices: [String: Bool] = [:]

    init(choices: [String: Bool] = [:]) { self.choices = choices }

    init(defaults: UserDefaults) {
        choices = (defaults.dictionary(forKey: Self.defaultsKey) ?? [:]).compactMapValues { $0 as? Bool }
    }

    func choice(for repository: String?) -> Bool? { repository.flatMap { choices[$0] } }

    /// What a started draft chose, for its repository; nothing for a folder in none.
    mutating func remember(_ draft: NewConversationDraft) {
        guard draft.showsWorktreeToggle, let repository = draft.repository else { return }
        choices[repository] = draft.worktree == true
    }

    func save(to defaults: UserDefaults) { defaults.set(choices, forKey: Self.defaultsKey) }
}
