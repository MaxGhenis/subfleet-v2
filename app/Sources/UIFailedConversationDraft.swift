#if !SUBFLEET_MODEL_TEST
import SwiftUI

struct FailedConversationDraftView: View {
    let draft: FailedConversationDraft
    let changeFolder: () -> Void
    let copy: () -> Void
    let discard: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Conversation could not start", systemImage: "exclamationmark.triangle").font(.title2)
            Text(draft.failure.message).foregroundStyle(.orange).textSelection(.enabled)
            Text(abbreviatedPath(draft.create.workspace)).foregroundStyle(.secondary)
            if !draft.text.isEmpty { Text(draft.text).textSelection(.enabled) }
            HStack {
                Button("Change folder and retry", action: changeFolder).buttonStyle(.borderedProminent)
                Button("Copy message", action: copy)
                Button("Discard", action: discard)
            }
        }.padding(28).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
#endif
