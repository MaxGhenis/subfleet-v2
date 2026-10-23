// Subfleet: the queue tray pinned above the composer (design §12, C-29.7).

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

/// One layout of the rendered tray, reported outside ViewThatFits' alternatives.
struct QueueTrayRowLayout: Equatable {
    var conversationID: String?
    var reveal: SearchReveal?
    var rowIDs: [String]
}

struct QueueTrayRowLayoutKey: PreferenceKey {
    static let defaultValue: [QueueTrayRowLayout] = []
    static func reduce(value: inout [QueueTrayRowLayout], nextValue: () -> [QueueTrayRowLayout]) {
        value += nextValue()
    }
}

/// Values and actions owned by the conversation; rendering state lives in Contents.
struct QueueTray: View {
    let title: String
    let rows: [QueuedMessage]
    var withdrawing: Set<String> = []
    let withdraw: (QueuedMessage) -> Void
    var steer: ((QueuedMessage) -> Void)? = nil
    var notice: ((String) -> AnyView?)? = nil
    var conversationID: String? = nil
    @Environment(\.searchPalette) private var palette

    var body: some View {
        if let palette { Following(palette: palette, tray: self) }
        else { Contents(tray: self, reveal: nil) }
    }

    private struct Following: View {
        @ObservedObject var palette: SearchPaletteModel
        let tray: QueueTray
        var body: some View { Contents(tray: tray, reveal: palette.reveal) }
    }

    private struct Contents: View {
        let tray: QueueTray
        let reveal: SearchReveal?
        @State private var expanded = false
        @State private var laidOut = SearchRevealLayout(reveal: nil, itemIDs: [])

        var body: some View {
            let presentation = QueueTrayPresentation(rows: tray.rows, expanded: expanded,
                conversationID: tray.conversationID, reveal: reveal)
            ScrollViewReader { proxy in
                content(presentation)
                    .onPreferenceChange(QueueTrayRowLayoutKey.self) { layouts in
                        guard let layout = layouts.last(where: { $0.conversationID == tray.conversationID }) else { return }
                        laidOut = SearchRevealLayout(reveal: layout.reveal, itemIDs: Set(layout.rowIDs))
                    }
                    .background {
                        if let cid = tray.conversationID {
                            SearchRevealer(conversationID: cid, proxy: proxy, layout: laidOut)
                        }
                    }
                    .onChange(of: reveal, initial: true) { _, _ in
                        if presentation.revealTargetsTray { expanded = true }
                    }
            }
        }

        private func content(_ presentation: QueueTrayPresentation) -> some View {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "tray.full").readingFont(.caption).foregroundStyle(.secondary)
                    Text(tray.title).readingFont(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Spacer(minLength: 4)
                    if presentation.expanded && presentation.rows.count > queueTrayVisible(count: tray.rows.count, expanded: false).shown {
                        Button("Show fewer") { expanded = false }.buttonStyle(.link).readingFont(.caption)
                    }
                }
                if presentation.expanded {
                    ViewThatFits(in: .vertical) {
                        list(presentation.rows)
                        ScrollView { list(presentation.rows) }.frame(height: 200)
                    }
                    .frame(maxHeight: 200)
                    .fixedSize(horizontal: false, vertical: true)
                } else {
                    list(presentation.rows)
                }
                if presentation.hidden > 0 {
                    Button("Show \(presentation.hidden) more") { expanded = true }.buttonStyle(.link).readingFont(.caption)
                        .help("Show every queued message")
                }
            }
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
            .background(GeometryReader { _ in
                Theme.clear.preference(key: QueueTrayRowLayoutKey.self, value: [QueueTrayRowLayout(
                    conversationID: tray.conversationID, reveal: reveal,
                    rowIDs: presentation.rows.map { "person:" + $0.id })])
            })
        }

        private func list(_ shown: [QueuedMessage]) -> some View {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(shown) { row in
                    QueueTrayRow(row: row, busy: tray.withdrawing.contains(row.id), withdraw: { tray.withdraw(row) },
                        steer: tray.steer.map { steer in { steer(row) } }, notice: tray.notice?(row.id))
                        .id("person:" + row.id)
                }
            }
        }
    }
}

/// Keep the original SwiftUI link's appearance and action. Owning its AppKit
/// host lets us attach identity to the real button even without an AX client.
private struct QueueTrayWithdrawButton: NSViewRepresentable {
    let id: String
    let preview: String
    let action: () -> Void
    @Environment(\.textScale) private var scale
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.isEnabled) private var enabled

    private var button: some View {
        Button("Withdraw", action: action).buttonStyle(.link).readingFont(.caption)
            .help("Withdraw this message; it is not sent")
            .accessibilityLabel("Withdraw: " + preview)
            .accessibilityIdentifier("queue-tray-withdraw-" + id)
            .environment(\.textScale, scale)
            .environment(\.colorScheme, colorScheme)
            .disabled(!enabled)
    }

    func makeNSView(context: Context) -> QueueTrayWithdrawHost {
        let view = QueueTrayWithdrawHost(rootView: AnyView(button))
        view.safeAreaRegions = []
        updateIdentity(view)
        return view
    }

    func updateNSView(_ view: QueueTrayWithdrawHost, context: Context) {
        view.rootView = AnyView(button)
        updateIdentity(view)
        view.needsLayout = true
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: QueueTrayWithdrawHost, context: Context) -> CGSize? {
        nsView.fittingSize
    }

    private func updateIdentity(_ view: QueueTrayWithdrawHost) {
        view.controlID = "queue-tray-withdraw-" + id
        view.words = "Withdraw: " + preview
        view.bindIdentity()
    }
}

private final class QueueTrayWithdrawHost: NSHostingView<AnyView> {
    var controlID = ""
    var words = ""

    override func layout() {
        super.layout()
        bindIdentity()
    }

    func bindIdentity() {
        func children(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(children) }
        for button in children(self).compactMap({ $0 as? NSButton }) {
            button.identifier = NSUserInterfaceItemIdentifier(controlID)
            button.setAccessibilityIdentifier(controlID)
            button.setAccessibilityLabel(words)
            button.toolTip = "Withdraw this message; it is not sent"
        }
    }
}

/// One queued message: up to two lines of it, why it waits when that is not
/// simply its turn, and Withdraw.
struct QueueTrayRow: View {
    let row: QueuedMessage
    /// Its Withdraw is under way.
    var busy = false
    let withdraw: () -> Void
    var steer: (() -> Void)? = nil
    var notice: AnyView? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: row.origin == "unblock-note" ? "note.text" : row.sending ? "paperplane" : "clock")
                .readingFont(.caption).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(row.preview).readingFont(.secondary).lineLimit(2).truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help(row.text ?? row.preview)
                if let status = row.status {
                    Text(status).readingFont(.footnote).foregroundStyle(.secondary).lineLimit(1)
                }
                if let notice { notice.disabled(busy) }
            }
            if busy {
                ProgressView().controlSize(.mini).help("Withdrawing")
            } else if row.canWithdraw {
                if row.canSteer, let steer {
                    Button("Send now", action: steer).buttonStyle(.link).readingFont(.caption)
                        .help("Send it into the running turn now instead of after it")
                        .accessibilityLabel("Send now: " + row.preview)
                }
                QueueTrayWithdrawButton(id: row.id, preview: row.preview, action: withdraw)
            }
        }
    }
}
/// A pre-start withdrawal is one compact line; other message notices retain
/// their existing status and served facts. Values only, for offscreen hosting.
struct ConversationNoticeRow: View {
    let words: String
    var withdrawn = false
    var status: AnyView? = nil

    var body: some View {
        if withdrawn {
            Text(words).readingFont(.caption).foregroundStyle(Theme.text.secondary.color)
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading).help(words)
        } else {
            VStack(alignment: .leading, spacing: Theme.space.step) {
                NoticeRow(symbol: "info.circle") { Text(words) }
                if let status { status }
            }
        }
    }
}
#endif
