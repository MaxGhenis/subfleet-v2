#if !SUBFLEET_MODEL_TEST
import SwiftUI

struct EmptyConversationView: View {
    let start: () -> Void
    var body: some View {
        VStack(spacing: Theme.space.inset * 1.5) {
            Image(systemName: "bubble.left.and.bubble.right").font(.largeTitle)
               .foregroundStyle(Theme.text.secondary.color)
            Text("Choose a conversation or start a new one").readingFont(.body)
            Text("Press ⌘K to search conversations and messages").readingFont(.caption)
               .foregroundStyle(Theme.text.tertiary.color)
            Button("New conversation", action: start).keyboardShortcut("n")
        }
        .foregroundStyle(Theme.text.primary.color)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.surface.conversation.color)
    }
}
#endif
