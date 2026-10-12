// A real UIModel's handling of a notification (C-29.9), apart from the views:
// what a click focuses, reveals and reasserts, what Allow once sends the daemon,
// and whether one arriving while the app is frontmost shows. It never starts
// UIModel's timers, watch or event loops, nor touches the notification center.
//
// `notification-model <input.json>`: `{"root", "focused", "baselined", "pending": {"<conversation>": n},
// "detail": <approval.get's answer>, "posted_detail": <the request as posted, default "detail">,
// "steps": [...]}`. A step is one of:
//   {"click": {"request_id", "user_info"}}            UIModel.show, as a delivered click reads back
//   {"respond": {"action", "request_id", <posted>}}    UIModel.handleNotification; action is "default",
//                                                      "allow-once" or "dismiss"
//   {"allow": <posted>}                                UIModel.allowOnce
//   {"frontmost": {"request_id", <posted>}}            UIModel.showsWhileFrontmost
//   {"deliver": {"action", "request_id", <posted>}}    the model's delegate, as the center hands it a
//                                                      response (ApprovalNotificationDelegate.respond)
//   {"present": {"request_id", <posted>}}              the model's delegate, as the center asks how to
//                                                      present one while frontmost (`present`)
//   {"new_draft": true}                                UIModel.openNewDraft
// where <posted> is {"user_info": {...}} given whole, or {"intent": {"kind", "subject", "conversation",
// "message"}, "allowing": true|false}, the userInfo `post()` builds for that intent (with the exact
// request of `posted_detail` when allowing). After each step: the focus, `focusReasserted`, the conversation
// whose card is to be revealed, whether a new conversation's draft is open, the daemon calls so far,
// the status line, for "frontmost" whether it shows, and for "present" the options. The output also
// names the Allow once category.
import AppKit
import UserNotifications

/// Preferences in memory only; nothing is written to ~/Library/Preferences.
final class NotificationProbeDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

/// Answers the approval ops from the input and records every call; anything else
/// fails as with a daemon that is down (an open a click starts, for one).
final class NotificationProbeClient: DaemonCalling, @unchecked Sendable {
    let detail: JSONValue?
    private let lock = NSLock()
    private var recorded: [[String: Any]] = []
    init(detail: JSONValue?) { self.detail = detail }

    var calls: [[String: Any]] { lock.withLock { recorded } }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(args))
        lock.withLock { recorded.append(["op": op.name, "args": encoded]) }
        switch op.name {
        case "approval.get":
            guard let detail else { break }
            return try detail.decode(R.self)
        case "approval.respond":
            guard let approval = detail?["approval"] else { break }
            return try JSONValue.object(["approval": approval]).decode(R.self)
        case "approval.list":
            return try JSONValue.object(["approvals": .array([])]).decode(R.self)
        default:
            break
        }
        throw DaemonClientError.unavailable("notification probe: \(op.name)")
    }
}

@MainActor func posted(_ step: JSONValue, detail: JSONValue?) throws -> [AnyHashable: Any] {
    if let given = step["user_info"]?.object {
        var userInfo: [AnyHashable: Any] = [:]
        for (key, value) in given { userInfo[key] = value.string }
        return userInfo
    }
    guard let intent = step["intent"], let kind = intent["kind"]?.string.flatMap(NotificationIntent.Kind.init(rawValue:))
    else { return [:] }
    let made = NotificationIntent(kind: kind, subject: intent["subject"]?.string ?? "1",
                                  conversationID: intent["conversation"]?.string ?? "",
                                  messageID: intent["message"]?.string, title: "Title", body: "Body")
    let target = step["allowing"]?.bool == true ? try detail.flatMap {
        ApprovalNotificationTarget(detail: try $0.decode(ApprovalDetail.self))
    } : nil
    return made.userInfo(allowing: target)
}

/// Waits until the daemon calls stop changing for a while (Allow once's work runs
/// on after the delegate tells the center it handled the response), at most 5 s.
@MainActor func settled(_ client: NotificationProbeClient) async {
    var quiet = 0
    var count = client.calls.count
    for _ in 0..<500 where quiet < 30 {
        try? await Task.sleep(nanoseconds: 10_000_000)
        let now = client.calls.count
        quiet = now == count ? quiet + 1 : 0
        count = now
    }
}

@main struct NotificationModelProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var state = ConversationStoreState()
        if let focused = input["focused"]?.string { state.focus(focused) }
        if input["baselined"]?.bool ?? true { state.markBaselined() }
        for (conversation, count) in input["pending"]?.object ?? [:] { state.pendingApprovals[conversation] = count.int ?? 0 }
        // A key given as null is absent.
        func given(_ key: String) -> JSONValue? { input[key].flatMap { $0 == .null ? nil : $0 } }
        let client = NotificationProbeClient(detail: given("detail"))
        let model = UIModel(paths: .rooted(at: URL(fileURLWithPath: input["root"]!.string!)),
                            client: client, defaults: NotificationProbeDefaults(), state: state)
        let detail = given("posted_detail") ?? given("detail")
        var out: [[String: Any]] = []
        for step in input["steps"]?.array ?? [] {
            var shows: Any = NSNull()
            if let click = step["click"] {
                if let target = NotificationTarget(requestID: click["request_id"]?.string ?? "",
                                                   userInfo: try posted(click, detail: detail)) {
                    model.show(target)
                }
            }
            if let respond = step["respond"] {
                let action = switch respond["action"]?.string {
                case "allow-once": ApprovalNotificationDelegate.allowOnceID
                case "dismiss": UNNotificationDismissActionIdentifier
                default: UNNotificationDefaultActionIdentifier
                }
                await model.handleNotification(action: action, requestID: respond["request_id"]?.string ?? "",
                                               userInfo: try posted(respond, detail: detail))?.value
            }
            if let allow = step["allow"] {
                await model.allowOnce(try posted(allow, detail: detail))?.value
            }
            if let frontmost = step["frontmost"] {
                shows = model.showsWhileFrontmost(requestID: frontmost["request_id"]?.string ?? "",
                                                  userInfo: try posted(frontmost, detail: detail))
            }
            if let deliver = step["deliver"] {
                let action = switch deliver["action"]?.string {
                case "allow-once": ApprovalNotificationDelegate.allowOnceID
                case "dismiss": UNNotificationDismissActionIdentifier
                default: UNNotificationDefaultActionIdentifier
                }
                let userInfo = try posted(deliver, detail: detail)
                await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
                    model.notificationDelegate.respond(action: action, requestID: deliver["request_id"]?.string ?? "",
                                                       userInfo: userInfo) { done.resume() }
                }
                await settled(client)
            }
            if let present = step["present"] {
                let userInfo = try posted(present, detail: detail)
                let options = await withCheckedContinuation { (done: CheckedContinuation<UNNotificationPresentationOptions, Never>) in
                    model.notificationDelegate.present(requestID: present["request_id"]?.string ?? "",
                                                       userInfo: userInfo) { done.resume(returning: $0) }
                }
                shows = [(UNNotificationPresentationOptions.banner, "banner"), (.list, "list"), (.sound, "sound"),
                         (.badge, "badge")].filter { options.contains($0.0) }.map { $0.1 }
            }
            if step["new_draft"]?.bool == true { model.openNewDraft() }
            out.append(["focused": model.state.focusedConversationID as Any? ?? NSNull(),
                        "focus_reasserted": model.focusReasserted,
                        "reveal": model.approvalReveal?.conversationID as Any? ?? NSNull(),
                        "new_draft": model.newDraft.isPresented,
                        "calls": client.calls.filter { ($0["op"] as? String)?.hasPrefix("approval.") == true },
                        "problem": model.problem as Any? ?? NSNull(),
                        "shows": shows])
        }
        let category = ApprovalNotificationDelegate.category
        let named: [String: Any] = ["id": category.identifier, "actions": category.actions.map {
            ["id": $0.identifier, "title": $0.title, "foreground": $0.options.contains(.foreground)] as [String: Any]
        }]
        print(String(data: try JSONSerialization.data(withJSONObject: ["steps": out, "category": named],
                                                      options: [.sortedKeys]),
                     encoding: .utf8)!)
    }
}
