import Foundation

/// A refused create still owns its unsent messages. Derived from the journal,
/// including version-1 journals, so no second copy can get out of sync.
struct FailedConversationDraft: Identifiable, Codable, Equatable {
    var id: String
    var create: ConversationCreateArgs
    var failure: OutboxFailure
    var messages: [OutboxEntry]
    var createdAt: String

    var text: String { messages.compactMap { $0.message?.text }.joined(separator: "\n\n") }
    var firstMessage: OutboxMessage? { messages.first?.message }
    var title: String { text.split(separator: "\n").first.map(String.init) ?? "Conversation could not start" }
}
