// Subfleet: the queue tray pinned above the composer (design §12, C-29.7).

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

/// The messages waiting their turn, in the order the daemon sends them, each
/// with Withdraw. Values and closures only, so a probe can host it.
struct QueueTray: View {
    /// `queueTrayTitle`: how many wait, and when they go.
    let title: String
    let rows: [QueuedMessage]
    /// Rows whose Withdraw the daemon has not answered yet: no second click.
    var withdrawing: Set<String> = []
    let withdraw: (QueuedMessage) -> Void
    /// Steer a row into the running turn; nil until the daemon offers it (`steer.v1`).
    var steer: ((QueuedMessage) -> Void)? = nil
    /// An optional seam for a future send notice, attached to its owning message.
    /// Keep its model/actions owned by that stack; attach its existing view by id.
    var notice: ((String) -> AnyView?)? = nil
    @State private var expanded = false

    var body: some View {
        let visible = queueTrayVisible(count: rows.count, expanded: expanded)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "tray.full").readingFont(.caption).foregroundStyle(.secondary)
                Text(title).readingFont(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                if expanded && visible.shown > queueTrayVisible(count: rows.count, expanded: false).shown {
                    Button("Show fewer") { expanded = false }.buttonStyle(.link).readingFont(.caption)
                }
            }
            if expanded {
                // A long queue scrolls inside the tray rather than pushing the timeline away.
                ViewThatFits(in: .vertical) {
                    list(rows)
                    ScrollView { list(rows) }.frame(height: 200)
                }
                .frame(maxHeight: 200)
                .fixedSize(horizontal: false, vertical: true)
            } else {
                list(Array(rows.prefix(visible.shown)))
            }
            if visible.hidden > 0 {
                Button("Show \(visible.hidden) more") { expanded = true }.buttonStyle(.link).readingFont(.caption)
                    .help("Show every queued message")
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
    }

    private func list(_ shown: [QueuedMessage]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(shown) { row in
                QueueTrayRow(row: row, busy: withdrawing.contains(row.id), withdraw: { withdraw(row) },
                             steer: steer.map { steer in { steer(row) } }, notice: notice?(row.id))
            }
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
                Button("Withdraw", action: withdraw).buttonStyle(.link).readingFont(.caption)
                    .help("Withdraw this message; it is not sent")
                    .accessibilityLabel("Withdraw: " + row.preview)
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
