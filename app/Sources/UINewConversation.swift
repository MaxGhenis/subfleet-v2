#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

/// Lives in the window detail. The model owns the draft so navigation cannot
/// discard its text, images or picks.
struct NewConversationDraftView: View {
    @ObservedObject var model: UIModel

    private var options: ComposerOptions {
        makeComposerOptions(provider: model.newDraft.provider, settings: model.newDraft.settings,
                            models: model.state.models[model.newDraft.provider] ?? [],
                            capabilities: model.state.availability.capabilities)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Spacer(minLength: 20)
            Text("New conversation").readingFont(.title, weight: .regular)
            Text("What would you like to work on?").foregroundStyle(Theme.text.secondary.color)
            HStack {
                Menu {
                    Button("Use a new scratch folder", action: model.useNewScratchFolder)
                    ForEach(model.recentWorkspaces(), id: \.self) { path in
                        Button(abbreviatedPath(path)) { model.newDraft.workspace = path }
                    }
                    Divider()
                    Button("Choose folder…", action: chooseFolder)
                } label: {
                    Label(model.newDraft.workspace.map { abbreviatedPath($0) } ?? "New scratch folder", systemImage: "folder")
                        .lineLimit(1)
                }.help("Folder for the new conversation")
                Spacer()
                Picker("Provider", selection: Binding(get: { model.newDraft.providerChoice ?? model.newDraft.provider },
                                                       set: model.selectNewDraftProvider)) {
                    Text("Auto").tag("auto")
                    Text("Claude").tag("claude")
                    Text("Codex").tag("codex")
                }.labelsHidden().pickerStyle(.segmented).frame(width: 225)
            }
            if let check = model.newDraft.workspaceCheck, !check.ok {
                NoticeRow(symbol: "exclamationmark.triangle") {
                    VStack(alignment: .leading, spacing: Theme.space.step) {
                        Text(check.reason ?? "This folder cannot be used.")
                        if let fix = check.fix { Text(fix).foregroundStyle(Theme.text.secondary.color) }
                        Button("Use a new scratch folder", action: model.useNewScratchFolder)
                    }
                }
            } else if model.newDraft.workspaceCheck == nil {
                Text("Checking folder…").font(.caption).foregroundStyle(Theme.text.secondary.color)
            } else if model.newDraft.workspace == nil, let scratch = model.newDraft.scratchWorkspace {
                Text("Created when you start: \(abbreviatedPath(scratch))")
                    .font(.caption).foregroundStyle(Theme.text.secondary.color)
            }
            if !model.newDraft.attachments.isEmpty {
                HStack {
                    ForEach(model.newDraft.attachments, id: \.sha256) { attachment in
                        HStack(spacing: 4) {
                            if let image = NSImage(contentsOfFile: attachment.path) {
                                Image(nsImage: image).resizable().scaledToFit().frame(height: 48)
                            }
                            Button { model.newDraft.attachments.removeAll { $0 == attachment } } label: {
                                Image(systemName: "xmark.circle.fill")
                            }.buttonStyle(.borderless).help("Remove this image").accessibilityLabel("Remove this image")
                        }
                    }
                }
            }
            VStack(alignment: .leading, spacing: Theme.space.inset) {
            ZStack(alignment: .topLeading) {
                DraftComposerTextView(text: $model.newDraft.text, focusRevision: model.newDraft.focusRevision,
                                      isSubmitting: model.newDraft.isSubmitting,
                                      submit: { model.sendNewDraft(stayHere: $0) }, onImage: addImage)
                    .frame(minHeight: 130, maxHeight: 230)
                if model.newDraft.text.isEmpty {
                    Text("Message \(model.newDraft.provider == "codex" ? "Codex" : "Claude")")
                        .foregroundStyle(Theme.text.tertiary.color).padding(10).allowsHitTesting(false)
                }
            }
            HStack(spacing: 10) {
                ModelEffortControl(provider: model.newDraft.provider, settings: $model.newDraft.settings, options: options)
                PermissionControl(value: model.newDraft.settings.permission, options: options) { model.newDraft.settings.permission = $0 }
                Spacer()
            }.windowFont(.control).controlSize(.small)
            }
            .padding(Theme.space.inset * 1.5)
            .background(RoundedRectangle(cornerRadius: Theme.radius.container).fill(Theme.surface.raised.color))
            .overlay(RoundedRectangle(cornerRadius: Theme.radius.container).stroke(Theme.line.hairline))
            if PermissionPolicy.widens(from: PermissionPolicy.ask.rawValue, to: model.newDraft.settings.permission) {
                Toggle("I understand the agent will act without asking", isOn: $model.newDraft.confirmWiden)
                    .font(.callout)
            }
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Return to start · Shift-Return for a new line")
                }.font(.caption).foregroundStyle(Theme.text.secondary.color)
                Spacer()
                Button("Start and stay here") { model.sendNewDraft(stayHere: true) }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(!model.newDraft.canStart)
                Button { model.sendNewDraft(stayHere: false) } label: {
                    Label("Start", systemImage: "arrow.up.circle.fill")
                }.buttonStyle(.borderedProminent).disabled(!model.newDraft.canStart)
            }
            Spacer(minLength: 20)
        }
        .readingFont(.body)
        .foregroundStyle(Theme.text.primary.color)
        .padding(Theme.space.column)
        .frame(maxHeight: .infinity)
        .readingColumn()
        .frame(maxWidth: .infinity)
        .background(Theme.surface.conversation.color)
        .onAppear { model.reconcileNewDraft(); model.validateNewDraftWorkspace(selectDefault: true) }
        .onChange(of: model.newDraft.provider) { _, _ in
            model.validateNewDraftWorkspace()
        }
        .onChange(of: model.newDraft.workspace) { _, _ in model.validateNewDraftWorkspace() }
        .onChange(of: model.newDraft.settings.model) { _, _ in model.reconcileNewDraft() }
        .onChange(of: model.newDraft.settings.permission) { _, _ in
            model.newDraft.confirmWiden = false
            model.validateNewDraftWorkspace()
        }
        .onChange(of: model.state.models) { _, _ in model.reconcileNewDraft() }
        .onChange(of: model.state.availability) { _, _ in model.reconcileNewDraft() }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        if let folder = model.newDraft.workspace { panel.directoryURL = URL(fileURLWithPath: folder) }
        panel.begin { response in
            if response == .OK, let folder = panel.url { model.newDraft.workspace = folder.path }
        }
    }

    private func addImage(_ data: Data) {
        guard !model.newDraft.isSubmitting else { return }
        do {
            let attachment = try AttachmentStager.stage(data, in: model.paths.attachmentsDirectory)
            if !model.newDraft.attachments.contains(attachment) { model.newDraft.attachments.append(attachment) }
        } catch { model.problem = "Could not attach image: \(error)" }
    }
}

/// The existing composer supplies paste/drop and newline behavior. This draft
/// adds its distinct Cmd-Return action and explicit focus when + New is used.
private final class DraftNSTextView: ComposerNSTextView {
    var onStay: () -> Void = {}
    var needsDraftFocus = true

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if needsDraftFocus, let window {
            window.makeFirstResponder(self)
            needsDraftFocus = false
        }
    }

    override func keyDown(with event: NSEvent) {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if (event.keyCode == 36 || event.keyCode == 76), modifiers.contains(.command),
           !modifiers.contains(.shift), !modifiers.contains(.option), !hasMarkedText() {
            onStay()
        } else { super.keyDown(with: event) }
    }
}

private struct DraftComposerTextView: NSViewRepresentable {
    @Binding var text: String
    var focusRevision: Int
    var isSubmitting: Bool
    var submit: (Bool) -> Void
    var onImage: (Data) -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        let editor = DraftNSTextView()
        editor.isRichText = false
        editor.allowsUndo = true
        editor.font = ReadingStyle.body.nsFont(scale: context.environment.textScale)
        editor.drawsBackground = false
        editor.textColor = Theme.text.primary.nsColor
        editor.updateFocusRing()
        editor.insertionPointColor = Theme.accentNS
        editor.setAccessibilityLabel("New conversation message")
        editor.isAutomaticQuoteSubstitutionEnabled = false
        editor.isAutomaticDashSubstitutionEnabled = false
        editor.textContainerInset = NSSize(width: 6, height: 8)
        editor.isVerticallyResizable = true
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.delegate = context.coordinator
        editor.registerForDraggedTypes([.png, .tiff, .fileURL])
        scroll.documentView = editor
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let editor = scroll.documentView as? DraftNSTextView else { return }
        editor.font = ReadingStyle.body.nsFont(scale: context.environment.textScale)
        editor.textColor = Theme.text.primary.nsColor
        editor.updateFocusRing()
        editor.onSubmit = { submit(false) }
        editor.onStay = { submit(true) }
        editor.onImage = onImage
        editor.isEditable = !isSubmitting
        if editor.string != text { editor.string = text }
        if context.coordinator.focusRevision != focusRevision {
            context.coordinator.focusRevision = focusRevision
            editor.needsDraftFocus = true
            DispatchQueue.main.async {
                if let window = editor.window, editor.needsDraftFocus {
                    window.makeFirstResponder(editor)
                    editor.needsDraftFocus = false
                }
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        var focusRevision = -1
        init(text: Binding<String>) { self.text = text }
        func textDidChange(_ notification: Notification) {
            if let editor = notification.object as? NSTextView { text.wrappedValue = editor.string }
        }
    }
}
#endif
