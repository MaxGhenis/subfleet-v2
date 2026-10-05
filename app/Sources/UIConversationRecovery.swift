#if !SUBFLEET_MODEL_TEST
import SwiftUI

/// The same controls rendered at the top of an opened blocked conversation and
/// hosted by the frontend view probe to verify the person's choices.
struct BlockedConversationBanner: View {
    let banner: BlockedBanner
    let perform: (BlockedChoice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            StatusBanner(title: banner.title, detail: banner.detail, symbol: "exclamationmark.octagon")
            HStack {
                ForEach(Array(banner.choices.enumerated()), id: \.offset) { _, choice in
                    Button(choice.label) { perform(choice) }
                        .buttonStyle(.borderless)
                        .help(choice.detail)
                }
            }.padding(.horizontal, 10).padding(.bottom, 8)
        }
    }
}

/// Background commands come from Claude, not the person who opened the app.
struct TaskNotificationView: View {
    let notice: TaskNotification

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: notice.status == "completed" ? "checkmark.circle" : "info.circle")
            VStack(alignment: .leading, spacing: 2) {
                Text(notice.summary).lineLimit(2).textSelection(.enabled)
                Text(notice.detail).readingFont(.footnote)
            }
        }
        .readingFont(.caption)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}
#endif
