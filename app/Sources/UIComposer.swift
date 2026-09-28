// Subfleet: the composer. Return sends, Shift-Return (or Option-Return) inserts
// a newline, pasted or dropped images are staged for `attachment.add`, and the
// field stays editable while a turn runs: a message sent then queues behind it.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI
import UniformTypeIdentifiers

final class ComposerNSTextView: NSTextView {
    var onSubmit: () -> Void = {}
    var onImage: (Data) -> Void = { _ in }

    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if isReturn && !modifiers.contains(.shift) && !modifiers.contains(.option) && !hasMarkedText() {
            onSubmit()
            return
        }
        super.keyDown(with: event)
    }

    override func paste(_ sender: Any?) {
        if pasteImages(from: NSPasteboard.general) { return }
        super.paste(sender)
    }

    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        super.readablePasteboardTypes + [.png, .tiff, .fileURL]
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        if pasteImages(from: sender.draggingPasteboard) { return true }
        return super.performDragOperation(sender)
    }

    /// Images on the pasteboard, as PNG data or image files; true when any was taken.
    private func pasteImages(from board: NSPasteboard) -> Bool {
        var found = false
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] {
            for url in urls where AttachmentStager.mediaType(of: (try? Data(contentsOf: url)) ?? Data()) != nil {
                if let data = try? Data(contentsOf: url) {
                    onImage(data)
                    found = true
                }
            }
        }
        if !found, let png = board.data(forType: .png) {
            onImage(png)
            found = true
        }
        if !found, let tiff = board.data(forType: .tiff), let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            onImage(png)
            found = true
        }
        return found
    }
}

struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    var onSubmit: () -> Void
    var onImage: (Data) -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        let textView = ComposerNSTextView()
        textView.isRichText = false
        textView.allowsUndo = true
        textView.font = ReadingStyle.body.nsFont(scale: context.environment.textScale)
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.textContainerInset = NSSize(width: 4, height: 6)
        textView.delegate = context.coordinator
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.registerForDraggedTypes([.png, .tiff, .fileURL])
        scroll.documentView = textView
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        context.coordinator.textView = textView
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let textView = scroll.documentView as? ComposerNSTextView else { return }
        textView.onSubmit = onSubmit
        textView.onImage = onImage
        if textView.string != text { textView.string = text }
        // C-29.13: the body size at the window's text scale.
        let font = ReadingStyle.body.nsFont(scale: context.environment.textScale)
        if textView.font != font { textView.font = font }
    }

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        weak var textView: NSTextView?
        init(text: Binding<String>) { self.text = text }
        func textDidChange(_ notification: Notification) {
            if let view = notification.object as? NSTextView { text.wrappedValue = view.string }
        }
    }
}

/// The composer with its per-message settings (D-19: model, effort and Fast are
/// separate choices) and the stop control.
struct ComposerView: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    @State private var text = ""
    @State private var staged: [StagedAttachment] = []
    @State private var settings: ConversationSettings?
    @State private var widenTo: String?
    @State private var loadedDraftFor: String?
    @Environment(\.textScale) private var textScale

    var body: some View {
        let current = settings ?? conversation.settings
        let options = model.state.composerOptions(for: conversation.conversation_id, settings: current)
        let timeline = model.state.timelines[conversation.conversation_id]
        let live = timeline?.liveMessageID
        VStack(alignment: .leading, spacing: 6) {
            if !staged.isEmpty {
                ScrollView(.horizontal) {
                    HStack {
                        ForEach(staged, id: \.sha256) { attachment in
                            HStack(spacing: 4) {
                                if let image = NSImage(contentsOfFile: attachment.path) {
                                    Image(nsImage: image).resizable().scaledToFit().frame(height: 48)
                                }
                                Button { staged.removeAll { $0 == attachment } } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.borderless).help("Remove this image")
                            }
                        }
                    }
                }
            }
            ZStack(alignment: .topLeading) {
                ComposerTextView(text: $text, onSubmit: submit, onImage: addImage)
                    .frame(height: composerHeight)
                if text.isEmpty {
                    Text(live == nil ? "Message \(conversation.provider == "codex" ? "Codex" : "Claude")"
                                     : "Queue a follow-up while this turn runs")
                        .readingFont(.body).foregroundStyle(.tertiary).padding(.leading, 9).padding(.top, 6)
                        .allowsHitTesting(false)
                }
            }
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
            HStack(spacing: 10) {
                if let options {
                    Picker("Model", selection: Binding(get: { current.model }, set: { value in
                        var next = current
                        next.model = value
                        let entry = options.models.first { $0.value == value }?.model
                        if let effort = next.effort, let efforts = entry?.efforts, !efforts.contains(effort) { next.effort = nil }
                        if entry?.fast.supported == false { next.fast = false }   // the toggle is disabled there
                        settings = next
                    })) {
                        ForEach(options.models) { choice in Text(choice.label).tag(choice.value) }
                        if !options.models.contains(where: { $0.value == current.model }) {
                            Text(current.model).tag(current.model)
                        }
                    }.labelsHidden().frame(maxWidth: 190).help("Model")
                    Picker("Effort", selection: Binding(get: { current.effort ?? "" }, set: { value in
                        var next = current
                        next.effort = value.isEmpty ? nil : value
                        settings = next
                    })) {
                        Text(options.defaultEffort.map { "Default (\($0.capitalized))" } ?? "Default effort").tag("")
                        ForEach(options.efforts, id: \.self) { Text($0.capitalized).tag($0) }
                    }.labelsHidden().frame(maxWidth: 140).help("Reasoning effort")
                    Toggle(isOn: Binding(get: { current.fast }, set: { value in
                        var next = current
                        next.fast = value
                        settings = next
                    })) {
                        Label("Fast", systemImage: "hare")
                    }
                    .toggleStyle(.button)
                    .disabled(options.fastSupported == false)
                    .help(options.fastSupported == false ? "This model offers no Fast mode" : options.fastNote)
                    Picker("Permission", selection: Binding(get: { conversation.settings.permission }, set: { value in
                        if PermissionPolicy.widens(from: conversation.settings.permission, to: value) {
                            widenTo = value
                        } else {
                            applyPermission(value, confirmed: false)
                        }
                    })) {
                        ForEach(options.permissions) { choice in
                            Text(choice.policy.label).tag(choice.policy.rawValue)
                                .foregroundStyle(choice.enabled ? .primary : .tertiary)
                        }
                    }.labelsHidden().frame(maxWidth: 130).help("What the agent may do without asking")
                }
                Spacer()
                if let live, let timeline {
                    Button {
                        let entry: OutboxEntry? = nil
                        model.stop(stopAction(for: live, state: timeline.turn(live)?.state, outboxEntry: entry))
                    } label: { Label("Stop", systemImage: "stop.circle") }
                        .help("Stop the running turn")
                }
                Button(action: submit) { Label("Send", systemImage: "arrow.up.circle.fill") }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && staged.isEmpty)
            }
            .controlSize(.small)
        }
        .padding(10)
        .onAppear(perform: loadDraft)
        .onChange(of: conversation.conversation_id) { _, _ in loadDraft() }
        .onChange(of: text) { _, _ in saveDraft() }
        .alert("Give this conversation more permission?", isPresented: Binding(get: { widenTo != nil },
                                                                               set: { if !$0 { widenTo = nil } })) {
            Button("Allow \(PermissionPolicy(rawValue: widenTo ?? "")?.label ?? "")", role: .destructive) {
                if let value = widenTo { applyPermission(value, confirmed: true) }
                widenTo = nil
            }
            Button("Cancel", role: .cancel) { widenTo = nil }
        } message: {
            Text("From \(PermissionPolicy(rawValue: conversation.settings.permission)?.label ?? conversation.settings.permission) to \(PermissionPolicy(rawValue: widenTo ?? "")?.label ?? ""). The agent will do more without asking you.")
        }
    }

    /// Starts at two lines and grows with the text to at most about ten, in
    /// proportion to the text size (the constants were set at 13 pt).
    private var composerHeight: CGFloat {
        let factor = CGFloat(ReadingStyle.body.pointSize(scale: textScale) / 13)
        let perLine = Double(max(20, 110 / factor))
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
            .reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / perLine))) }
        return min(200 * factor, max(44 * factor, CGFloat(lines) * 18 * factor + 14))
    }

    private func submit() {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || !staged.isEmpty else { return }
        var outgoing = settings ?? conversation.settings
        outgoing.permission = conversation.settings.permission
        model.send(conversationID: conversation.conversation_id, text: trimmed, staged: staged, settings: outgoing)
        text = ""
        staged = []
        model.drafts.delete(conversation.conversation_id)
    }

    private func addImage(_ data: Data) {
        if let attachment = model.stage(data), !staged.contains(attachment) { staged.append(attachment) }
    }

    private func applyPermission(_ value: String, confirmed: Bool) {
        var next = conversation.settings
        next.permission = value
        let conversation = conversation
        Task { _ = await model.updateSettings(conversation, to: next, confirmedWiden: confirmed) }
    }

    private func loadDraft() {
        guard loadedDraftFor != conversation.conversation_id else { return }
        loadedDraftFor = conversation.conversation_id
        settings = nil
        if let draft = model.drafts.load(conversation.conversation_id) {
            text = draft.text
            staged = draft.attachments
            settings = draft.settings
        } else {
            text = ""
            staged = []
        }
    }

    private func saveDraft() {
        let key = conversation.conversation_id
        if text.isEmpty && staged.isEmpty {
            model.drafts.delete(key)
        } else {
            try? model.drafts.save(Draft(text: text, attachments: staged, settings: settings,
                                         updated_at: ISO8601DateFormatter().string(from: Date())), for: key)
        }
    }
}
#endif
