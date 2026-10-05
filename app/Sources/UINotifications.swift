import Foundation

/// A notification can allow only the exact, unmasked request it advertised.
/// Fresh detail is checked again when the action arrives, including its hash.
struct ApprovalNotificationTarget: Codable, Equatable {
    var conversationID: String
    var messageID: String
    var approvalID: String
    var requestSHA256: String

    static func candidate(from approvals: [ApprovalView], conversationID: String,
                          messageID: String?) -> ApprovalView? {
        let pending = approvals.filter {
            $0.state == "pending" && $0.conversation_id == conversationID
                && (messageID == nil || $0.message_id == messageID)
        }
        guard pending.count == 1, pending[0].kind != "question",
              pending[0].options.contains("allow") else { return nil }
        return pending[0]
    }

    init?(detail: ApprovalDetail) {
        guard Self.isAllowable(detail) else { return nil }
        conversationID = detail.approval.conversation_id
        messageID = detail.approval.message_id
        approvalID = detail.approval.approval_id
        requestSHA256 = detail.request_sha256
    }

    func canAllow(_ detail: ApprovalDetail) -> Bool {
        Self.isAllowable(detail)
            && detail.approval.conversation_id == conversationID
            && detail.approval.message_id == messageID
            && detail.approval.approval_id == approvalID
            && detail.request_sha256 == requestSHA256
    }

    private static func isAllowable(_ detail: ApprovalDetail) -> Bool {
        detail.approval.state == "pending" && detail.approval.kind != "question"
            && detail.approval.options.contains("allow") && detail.masked.isEmpty
            && !detail.request_sha256.isEmpty
    }

    var userInfo: [String: String] {
        ["conversation_id": conversationID, "message_id": messageID,
         "approval_id": approvalID, "request_sha256": requestSHA256]
    }

    init?(userInfo: [AnyHashable: Any]) {
        guard let conversation = userInfo["conversation_id"] as? String, !conversation.isEmpty,
              let message = userInfo["message_id"] as? String, !message.isEmpty,
              let approval = userInfo["approval_id"] as? String, !approval.isEmpty,
              let hash = userInfo["request_sha256"] as? String, !hash.isEmpty else { return nil }
        conversationID = conversation
        messageID = message
        approvalID = approval
        requestSHA256 = hash
    }
}

#if !SUBFLEET_MODEL_TEST || SUBFLEET_UI_MODEL_TEST
import UserNotifications

final class ApprovalNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let categoryID = "org.maxghenis.subfleet.tool-approval"
    static let allowOnceID = "org.maxghenis.subfleet.allow-once"
    private let receive: @MainActor (String, [AnyHashable: Any]) -> Void

    init(receive: @escaping @MainActor (String, [AnyHashable: Any]) -> Void) {
        self.receive = receive
    }

    func register() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let action = UNNotificationAction(identifier: Self.allowOnceID, title: "Allow once", options: [.foreground])
        let category = UNNotificationCategory(identifier: Self.categoryID, actions: [action],
                                               intentIdentifiers: [], options: [])
        center.getNotificationCategories { existing in
            var categories = existing.filter { $0.identifier != Self.categoryID }
            categories.insert(category)
            center.setNotificationCategories(categories)
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let action = response.actionIdentifier
        let userInfo = response.notification.request.content.userInfo
        Task { @MainActor in
            receive(action, userInfo)
            completionHandler()
        }
    }
}
#endif
