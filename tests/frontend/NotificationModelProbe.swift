// A real UIModel's handling of a notification's click (C-29.9), apart from the
// views: what a click focuses, reveals and reasserts. It never starts UIModel's
// timers, watch or event loops.
//
// `notification-model <input.json>`: `{"root", "focused", "baselined", "pending": {"<conversation>": n},
// "steps": [{"click": {"request_id", "user_info"}} | {"new_draft": true}]}`; after each step,
// the focus, `focusReasserted`, the conversation whose card is to be revealed, and whether a
// new conversation's draft is open.
import AppKit

/// Preferences in memory only; nothing is written to ~/Library/Preferences.
final class NotificationProbeDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

/// Answers nothing, as a daemon that is down: the open a click starts fails and
/// leaves the focus and the counters this probe reads.
final class NotificationProbeClient: DaemonCalling, @unchecked Sendable {
    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        throw DaemonClientError.unavailable("notification probe: \(op.name)")
    }
}

@main struct NotificationModelProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var state = ConversationStoreState()
        if let focused = input["focused"]?.string { state.focus(focused) }
        state.watchBaselined = input["baselined"]?.bool ?? true
        for (conversation, count) in input["pending"]?.object ?? [:] { state.pendingApprovals[conversation] = count.int ?? 0 }
        let model = UIModel(paths: .rooted(at: URL(fileURLWithPath: input["root"]!.string!)),
                            client: NotificationProbeClient(), defaults: NotificationProbeDefaults(), state: state)
        var out: [[String: Any]] = []
        for step in input["steps"]?.array ?? [] {
            if let click = step["click"] {
                var userInfo: [AnyHashable: Any] = [:]
                for (key, value) in click["user_info"]?.object ?? [:] { userInfo[key] = value.string }
                if let target = NotificationTarget(requestID: click["request_id"]?.string ?? "", userInfo: userInfo) {
                    model.show(target)
                }
            }
            if step["new_draft"]?.bool == true { model.openNewDraft() }
            out.append(["focused": model.state.focusedConversationID as Any? ?? NSNull(),
                        "focus_reasserted": model.focusReasserted,
                        "reveal": model.approvalReveal?.conversationID as Any? ?? NSNull(),
                        "new_draft": model.newDraft.isPresented])
        }
        print(String(data: try JSONSerialization.data(withJSONObject: ["steps": out], options: [.sortedKeys]),
                     encoding: .utf8)!)
    }
}
