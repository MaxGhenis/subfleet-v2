// An unsent new conversation. Foundation only: opening and leaving the draft
// never creates a daemon conversation or discards the person's work.
import Foundation

struct NewConversationDraft: Codable, Equatable {
    var text = ""
    var attachments: [StagedAttachment] = []
    /// nil means the person explicitly chose No folder.
    var workspace: String?
    var provider = "claude"
    var settings = ConversationSettings(model: "")
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

    /// Refresh choices as catalogs arrive or the provider/model changes.
    mutating func reconcile(models: [ModelEntry], capabilities: Capabilities?) {
        let options = makeComposerOptions(provider: provider, settings: settings, models: models, capabilities: capabilities)
        if !options.models.contains(where: { $0.value == settings.model }), let first = options.models.first {
            settings.model = first.value
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
    }
}
