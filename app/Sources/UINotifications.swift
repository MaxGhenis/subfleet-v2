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

extension NotificationIntent {
    /// What the posted notification carries: its conversation and kind, and for a
    /// tool approval that Allow once can answer, that one request, so the click
    /// still reads its conversation and kind (`NotificationTarget`).
    func userInfo(allowing target: ApprovalNotificationTarget?) -> [String: String] {
        guard let target else { return userInfo }
        return userInfo.merging(target.userInfo) { _, exact in exact }
    }
}

#if !SUBFLEET_MODEL_TEST || SUBFLEET_UI_MODEL_TEST
import UserNotifications

/// The notification center's delegate, for every notification the app posts
/// (C-29.9): its response (a click, Allow once on a tool approval) and whether
/// one arriving while the app is frontmost shows.
final class ApprovalNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let categoryID = "org.maxghenis.subfleet.tool-approval"
    static let allowOnceID = "org.maxghenis.subfleet.allow-once"
    /// The action, the request identifier and the userInfo.
    private let receive: @MainActor (String, String, [AnyHashable: Any]) -> Void
    /// Whether to show it while the app is frontmost, from its identifier and userInfo.
    private let shows: @MainActor (String, [AnyHashable: Any]) -> Bool

    init(receive: @escaping @MainActor (String, String, [AnyHashable: Any]) -> Void,
         present: @escaping @MainActor (String, [AnyHashable: Any]) -> Bool) {
        self.receive = receive
        self.shows = present
    }

    /// A tool approval's notifications carry this category: one action, Allow once,
    /// which brings the app forward.
    static var category: UNNotificationCategory {
        let action = UNNotificationAction(identifier: allowOnceID, title: "Allow once", options: [.foreground])
        return UNNotificationCategory(identifier: categoryID, actions: [action], intentIdentifiers: [], options: [])
    }

    func register() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let category = Self.category
        center.getNotificationCategories { existing in
            var categories = existing.filter { $0.identifier != Self.categoryID }
            categories.insert(category)
            center.setNotificationCategories(categories)
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let request = response.notification.request
        respond(action: response.actionIdentifier, requestID: request.identifier, userInfo: request.content.userInfo,
                completion: completionHandler)
    }

    /// Without this the system shows nothing while the app is frontmost.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        let request = notification.request
        present(requestID: request.identifier, userInfo: request.content.userInfo, completion: completionHandler)
    }

    /// A response, as the center hands it over (a probe calls this in its place):
    /// the model gets the action, the request and its userInfo, then the center
    /// is told it was handled.
    func respond(action: String, requestID: String, userInfo: [AnyHashable: Any], completion: @escaping () -> Void) {
        Task { @MainActor in
            receive(action, requestID, userInfo)
            completion()
        }
    }

    /// How to present one arriving while the app is frontmost: as a banner and in
    /// the list, or not at all.
    func present(requestID: String, userInfo: [AnyHashable: Any],
                 completion: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in
            completion(shows(requestID, userInfo) ? [.banner, .list] : [])
        }
    }
}
#endif
