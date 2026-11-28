#if !SUBFLEET_MODEL_TEST
import SwiftUI

/// The same controls rendered at the top of an opened blocked conversation and
/// hosted by the frontend view probe to verify the person's choices.
struct BlockedConversationBanner: View {
    let banner: BlockedBanner
    let perform: (BlockedChoice) -> Void

    var body: some View {
        NoticeRow(symbol: "exclamationmark.octagon") {
            VStack(alignment: .leading, spacing: Theme.space.inset) {
                Text(banner.title).readingFont(.body)
                Text(banner.detail).readingFont(.secondary).foregroundStyle(Theme.text.secondary.color)
                HStack(spacing: Theme.space.inset * 2) {
                    ForEach(Array(banner.choices.enumerated()), id: \.offset) { _, choice in
                        Button(choice.label) { perform(choice) }
                            .buttonStyle(.borderless).help(choice.detail)
                    }
                }
            }
        }.padding(Theme.space.inset)

    }
}

/// Background commands come from Claude, not the person who opened the app.
struct TaskNotificationView: View {
    let notice: TaskNotification

    var body: some View {
        NoticeRow(symbol: notice.status == "completed" ? "checkmark.circle" : "info.circle") {
            VStack(alignment: .leading, spacing: Theme.space.step) {
                Text(notice.summary).lineLimit(2).textSelection(.enabled)
                Text(notice.detail).readingFont(.footnote).foregroundStyle(Theme.text.secondary.color)
            }
        }
        .accessibilityElement(children: .combine)

    }
}
#endif
