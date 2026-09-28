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
    @State private var expanded = false

    var body: some View {
        let visible = queueTrayVisible(count: rows.count, expanded: expanded)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "tray.full").font(.caption).foregroundStyle(.secondary)
                Text(title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                if expanded && visible.shown > queueTrayVisible(count: rows.count, expanded: false).shown {
                    Button("Show fewer") { expanded = false }.buttonStyle(.link).font(.caption)
                }
            }
            if expanded {
                // A long queue scrolls inside the tray rather than pushing the timeline away.
                ViewThatFits(in: .vertical) {
                    list(rows)
                    ScrollView { list(rows) }.frame(height: 200)
                }
                .frame(maxHeight: 200)
            } else {
                list(Array(rows.prefix(visible.shown)))
            }
            if visible.hidden > 0 {
                Button("Show \(visible.hidden) more") { expanded = true }.buttonStyle(.link).font(.caption)
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
                             steer: steer.map { steer in { steer(row) } })
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

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: row.origin == "unblock-note" ? "note.text" : row.sending ? "paperplane" : "clock")
                .font(.caption).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(row.preview).font(.callout).lineLimit(2).truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help(row.text ?? row.preview)
                if let status = row.status {
                    Text(status).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            if busy {
                ProgressView().controlSize(.mini).help("Withdrawing")
            } else if row.canWithdraw {
                if row.canSteer, let steer {
                    Button("Steer", action: steer).buttonStyle(.link).font(.caption)
                        .help("Send it into the running turn now instead of after it")
                        .accessibilityLabel("Steer: " + row.preview)
                }
                Button("Withdraw", action: withdraw).buttonStyle(.link).font(.caption)
                    .help("Withdraw this message; it is not sent")
                    .accessibilityLabel("Withdraw: " + row.preview)
            }
        }
    }
}
#endif
