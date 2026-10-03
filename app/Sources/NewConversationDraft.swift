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
    }

    var startResolution: String {
        "\(provider == "codex" ? "Codex" : "Claude") · \(settings.model.isEmpty ? "Loading model…" : settings.model)"
    }

    mutating func invalidateWorkspaceCheck() {
        workspaceCheck = nil
        checkedWorkspace = nil
        checkedPermission = nil
        checkedProvider = nil
    }

    mutating func applyWorkspaceCheck(_ result: WorkspaceCheckResult, workspace: String, provider: String, permission: String) {
        guard workspace == resolvedWorkspace, provider == self.provider, permission == settings.permission else { return }
        workspaceCheck = result
        checkedWorkspace = workspace
        checkedPermission = permission
        checkedProvider = provider
    }

    /// Refresh choices as catalogs arrive or the provider/model changes.
    mutating func reconcile(models: [ModelEntry], capabilities: Capabilities?, defaultModel: String? = nil,
                            rememberedModel: String? = nil) {
        let options = makeComposerOptions(provider: provider, settings: settings, models: models, capabilities: capabilities)
        if !options.models.contains(where: { $0.value == settings.model }) {
            let preferred = [rememberedModel, defaultModel].compactMap { $0 }
                .compactMap { pick in options.models.first { $0.value == pick || $0.model.id == pick }?.value }.first
            settings.model = preferred ?? options.models.first?.value ?? settings.model
        }
        let selected = makeComposerOptions(provider: provider, settings: settings, models: models, capabilities: capabilities)
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
        isSubmitting = false
        focusRevision += 1
    }

    /// A restored draft is editable even if the app quit during a journal write.
    mutating func restore() {
        isSubmitting = false
        isPresented = false
        invalidateWorkspaceCheck()
    }
}
