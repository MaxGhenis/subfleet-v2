// An unsent new conversation. Foundation only: opening and leaving the draft
// never creates a daemon conversation or discards the person's work.
import Foundation

struct NewConversationDraft: Codable, Equatable {
    var text = ""
    var attachments: [StagedAttachment] = []
    /// nil chooses a scratch workspace, whose directory is created only at Start.
    var workspace: String?
    var provider = "claude"
    /// Missing in older saved drafts: their provider remains an explicit pick.
    var providerChoice: String? = "auto"
    var settings = ConversationSettings(model: "")
    var scratchWorkspace: String?
    var workspaceCheck: WorkspaceCheckResult?
    var checkedWorkspace: String?
    var checkedPermission: String?
    var checkedProvider: String?
    /// The `workspace_kind` the folder check was for: a worktree skips the in-place
    /// checks (a checkout on main), so a check of one kind never starts the other.
    var checkedKind: String?
    /// The git repository holding `workspace` (`GitCheckout.key`) when it was last
    /// looked at, or nil when it is in none; only for `repositoryWorkspace`.
    var repository: String?
    var repositoryWorkspace: String?
    /// The Worktree toggle: the person's pick, or the repository's remembered one.
    /// Missing (nil) in older saved drafts and until a repository is found.
    var worktree: Bool?
    /// The repository `worktree` belongs to; nil for a pick carried over from a
    /// refused create, which the first repository found takes.
    var worktreeRepository: String?
    /// A transport failure is provisional; keep the folder and allow a recheck.
    var workspaceCheckTransient: Bool?
    /// Stable across a crash between journal writes and clearing the composer.
    var requestID: String?
    var messageID: String?
    var confirmWiden = false
    var isPresented = false
    var focusRevision = 0
    var isSubmitting = false

    init(workspace: String? = nil) { self.workspace = workspace }

    mutating func open() {
        isPresented = true
        focusRevision += 1
    }

    mutating func leave() { isPresented = false }

    var canSend: Bool {
        !isSubmitting && !settings.model.isEmpty
            && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty)
            && (!PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: settings.permission) || confirmWiden)
    }

    var resolvedWorkspace: String? { workspace ?? scratchWorkspace }

    var canStart: Bool {
        canSend && workspaceCheck?.ok == true && resolvedWorkspace == checkedWorkspace
            && settings.permission == checkedPermission && provider == checkedProvider
            && workspaceKind == (checkedKind ?? "in-place")
    }

    /// The chosen folder is in a git checkout: the Worktree toggle shows. A scratch
    /// folder never is (the daemon refuses a worktree there).
    var showsWorktreeToggle: Bool {
        workspace != nil && repository != nil && repositoryWorkspace == workspace
    }

    /// What Start asks `conversation.create` for: a worktree only of a git checkout,
    /// with the toggle on; otherwise the folder itself.
    var workspaceKind: String { showsWorktreeToggle && worktree == true ? "worktree" : "in-place" }

    /// The folder's repository was looked for (nil: it is in none). A pick made in
    /// another repository gives way to this one's remembered choice, off when there
    /// is none; one made in this repository (a subfolder changed) stays.
    mutating func noteRepository(_ repository: String?, for workspace: String?, remembered: Bool?) {
        guard workspace == self.workspace else { return }
        self.repository = repository
        repositoryWorkspace = workspace
        guard let repository else { return }
        if worktree == nil || (worktreeRepository != nil && worktreeRepository != repository) {
            worktree = remembered ?? false
        }
        worktreeRepository = repository
    }

    /// The person flips the toggle; the folder must be checked again for that kind.
    mutating func pickWorktree(_ on: Bool) {
        worktree = on
        worktreeRepository = repository
        invalidateWorkspaceCheck()
    }

    var startResolution: String {
        "\(provider == "codex" ? "Codex" : "Claude") · \(settings.model.isEmpty ? "Loading model…" : settings.model)"
    }

    mutating func invalidateWorkspaceCheck() {
        workspaceCheck = nil
        checkedWorkspace = nil
        checkedPermission = nil
        checkedProvider = nil
        checkedKind = nil
        workspaceCheckTransient = nil
    }

    mutating func applyWorkspaceCheck(_ result: WorkspaceCheckResult, workspace: String, provider: String, permission: String,
                                      kind: String = "in-place", transient: Bool = false) {
        guard workspace == resolvedWorkspace, provider == self.provider, permission == settings.permission,
              kind == workspaceKind else { return }
        workspaceCheck = result
        checkedWorkspace = workspace
        checkedPermission = permission
        checkedProvider = provider
        checkedKind = kind
        workspaceCheckTransient = transient
    }

    /// Refresh choices as catalogs arrive or the provider/model changes.
    mutating func reconcile(models: [ModelEntry], capabilities: Capabilities?, defaultModel: String? = nil,
                            rememberedModel: String? = nil) {
        let active = models.filter { $0.retired != true }
        let options = makeComposerOptions(provider: provider, settings: settings, models: active, capabilities: capabilities)
        if !options.models.contains(where: { $0.value == settings.model }) {
            // Codex starts with the daemon's live hard-tier choice. An existing
            // draft's explicit model pick above remains selected while offered.
            let picks = provider == "codex" ? [defaultModel] : [rememberedModel, defaultModel]
            let preferred = picks.compactMap { $0 }
                .compactMap { pick in options.models.first { $0.value == pick || $0.model.id == pick }?.value }.first
            settings.model = preferred ?? options.models.first?.value ?? (models.isEmpty ? settings.model : "")
        }
        let selected = makeComposerOptions(provider: provider, settings: settings, models: active, capabilities: capabilities)
        if let effort = settings.effort, selected.effortsObserved, !selected.efforts.contains(effort) {
            settings.effort = nil
        }
        if provider == "codex" && capabilities?.codex_writable != true {
            settings.permission = PermissionPolicy.readOnly.rawValue
        }
    }

    /// Clear only after both create and first message have been journaled.
    mutating func journaled() {
        text = ""
        attachments = []
        requestID = nil
        messageID = nil
        isSubmitting = false
        focusRevision += 1
    }

    /// A restored draft is editable even if the app quit during a journal write.
    /// Its folder is looked at again, so a checkout made or removed meanwhile counts.
    mutating func restore() {
        isSubmitting = false
        isPresented = false
        invalidateWorkspaceCheck()
        repository = nil
        repositoryWorkspace = nil
    }
}
