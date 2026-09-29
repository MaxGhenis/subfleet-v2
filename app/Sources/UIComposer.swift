// Subfleet: the composer. Return sends, Shift-Return (or Option-Return) inserts
// a newline, pasted or dropped images are staged for `attachment.add`, and the
// field stays editable while a turn runs: a message sent then queues behind it.
// While the running turn takes steers (C-24.9, `steer.v1` for this provider),
// Return steers the text into it and ⌘Return queues it for later, as Claude Code
// does (DESIGN.md sections 8 and 9); `/` commands and `!` shell input still wait
// for the turn to end. Esc while a turn runs takes back the latest unread steer,
// or else stops the turn.

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI
import UniformTypeIdentifiers

final class ComposerNSTextView: NSTextView {
    var onSubmit: () -> Void = {}
    /// Set only while the composer steers: ⌘Return queues the text for later.
    var onQueue: (() -> Void)?
    /// Set only while a turn runs: Esc takes back an unread steer, or stops the turn.
    var onEscape: (() -> Void)?
    var onImage: (Data) -> Void = { _ in }

    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if isReturn && !modifiers.contains(.shift) && !modifiers.contains(.option) && !hasMarkedText() {
            if modifiers.contains(.command), let onQueue {
                onQueue()
            } else {
                onSubmit()
            }
            return
        }
        // Esc belongs to IME composition first (it cancels it), then to the running turn.
        if event.keyCode == 53, modifiers.isDisjoint(with: [.shift, .option, .command, .control]), !hasMarkedText(),
           let onEscape {
            onEscape()
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
    var onQueue: (() -> Void)? = nil
    var onEscape: (() -> Void)? = nil
    var onImage: (Data) -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        let textView = ComposerNSTextView()
        textView.isRichText = false
        textView.allowsUndo = true
        textView.font = NSFont.preferredFont(forTextStyle: .body)
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
        textView.onQueue = onQueue
        textView.onEscape = onEscape
        textView.onImage = onImage
        if textView.string != text { textView.string = text }
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

    var body: some View {
        let current = settings ?? conversation.settings
        let options = model.state.composerOptions(for: conversation.conversation_id, settings: current)
        let timeline = model.state.timelines[conversation.conversation_id]
        let live = timeline?.liveMessageID
        // C-24.9: while this is set, Return steers into that turn and ⌘Return queues for later.
        let steerHost = model.state.steerHost(forComposerOf: conversation.conversation_id)
        let empty = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && staged.isEmpty
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
                ComposerTextView(text: $text, onSubmit: { submit() },
                                 onQueue: steerHost == nil ? nil : { submit(queue: true) },
                                 onEscape: live == nil ? nil : {
                                     model.escape(conversationID: conversation.conversation_id, assistant: assistant)
                                 },
                                 onImage: addImage)
                    .frame(height: composerHeight)
                if text.isEmpty {
                    Text(steerHost != nil ? "Steer the running turn · ⌘⏎ queues for later"
                         : live == nil ? "Message \(assistant)"
                         : "Queue a follow-up while this turn runs")
                        .foregroundStyle(.tertiary).padding(.leading, 9).padding(.top, 6).allowsHitTesting(false)
                }
            }
            .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.3)))
            if let steerHost, let hint = steerSettingsHint(picked: outgoing(current), host: steerHost) {
                Label(hint, systemImage: "info.circle").font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
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
                    if steerHost != nil {
                        // Claude Code's send button while a turn runs: Steer, with Queue for later and Stop.
                        Menu {
                            Button("Queue for later") { submit(queue: true) }
                                .keyboardShortcut(.return, modifiers: .command)
                                .disabled(empty)
                                .help("Queue for later ⌘⏎")
                                .accessibilityLabel("Queue for later")
                            Button("Stop") {
                                model.stop(stopAction(for: live, state: timeline.turn(live)?.state, outboxEntry: nil))
                            }
                            .accessibilityLabel("Stop the running turn")
                        } label: {
                            Label("Steer", systemImage: "arrow.up.circle.fill")
                        } primaryAction: {
                            submit()
                        }
                        .fixedSize()
                        .help("Send ⏎")
                        .accessibilityLabel("Steer the running turn")
                        .accessibilityHint("Its menu queues the message for later or stops the turn")
                    } else {
                        Button {
                            model.stop(stopAction(for: live, state: timeline.turn(live)?.state, outboxEntry: nil))
                        } label: { Label("Stop", systemImage: "stop.circle") }
                            .help("Stop the running turn")
                    }
                }
                if steerHost == nil {
                    Button { submit() } label: { Label("Send", systemImage: "arrow.up.circle.fill") }
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(empty)
                }
            }
            .controlSize(.small)
        }
        .padding(10)
        .onAppear { takeRecall() }
        .onChange(of: conversation.conversation_id) { _, _ in takeRecall() }
        .onChange(of: text) { _, _ in saveDraft() }
        .onChange(of: model.composerRecall[conversation.conversation_id]?.id) { _, _ in takeRecall() }
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

    /// Starts at two lines and grows with the text to at most about ten.
    private var composerHeight: CGFloat {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
            .reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / 110))) }
        return min(200, max(44, CGFloat(lines) * 18 + 14))
    }

    /// The message's own settings: the composer's picks, under the conversation's permission.
    private func outgoing(_ picked: ConversationSettings) -> ConversationSettings {
        var outgoing = picked
        outgoing.permission = conversation.settings.permission
        return outgoing
    }

    private var assistant: String { conversation.provider == "codex" ? "Codex" : "Claude" }

    /// Send the text: steered into the running turn when the composer steers and
    /// `queue` is false, else queued for later, as the next turn (C-24.9). A slash
    /// command or `!` shell input is never steered. A steer the turn can no longer
    /// take is refused, and the message stays queued.
    private func submit(queue: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || !staged.isEmpty else { return }
        let steer = !queue && steerable(text: trimmed)
            && model.state.steerHost(forComposerOf: conversation.conversation_id) != nil
        model.send(conversationID: conversation.conversation_id, text: trimmed, staged: staged,
                   settings: outgoing(settings ?? conversation.settings), steer: steer)
        text = ""
        staged = []
        model.drafts.delete(conversation.conversation_id)
    }

    /// Load this conversation's draft if the composer has not yet, then take in
    /// what Esc took back: its words go into the composer ahead of anything typed
    /// since, and its images are staged again. `UIModel.escape` put them in the
    /// draft too (`inDraft`), so a composer that has just loaded the draft has
    /// them; one on screen since merges them in and saves. The draft is loaded
    /// first whichever change SwiftUI hands over first (the conversation's or the
    /// recall's), so the words never land in the text of the conversation shown
    /// before.
    private func takeRecall() {
        let loaded = loadDraft()
        let key = conversation.conversation_id
        guard let recall = model.composerRecall[key] else { return }
        model.composerRecall[key] = nil
        guard !(loaded && recall.inDraft) else { return }
        let merged = recalledDraft(Draft(text: text, attachments: staged, settings: settings, updated_at: ""),
                                   text: recall.text, staged: recall.staged, now: "")
        text = merged.text
        staged = merged.attachments
        saveDraft()                         // images alone change no text, so nothing else saves them
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

    /// Whether it read the draft from disk now (not already loaded for this conversation).
    @discardableResult
    private func loadDraft() -> Bool {
        guard loadedDraftFor != conversation.conversation_id else { return false }
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
        return true
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
