// The core probe's send-failure placement command (C-29.12 with the queue tray,
// C-29.7): where a send that is not going through shows its notice, as the
// store lays out a conversation. Foundation only; part of the core probe
// (conftest.CORE_PROBE).
import Foundation

/// A tray row as `project(QueuedMessage)` prints it, and its notice's parts.
func projectNoticeRow(_ row: QueuedMessage) -> [String: Any] {
    var out = project(row)
    out["notice"] = row.notice.map(project) as Any? ?? NSNull()
    out["notice_actions"] = row.noticeActions.map(\.rawValue)
    out["notice_line"] = row.noticeLine as Any? ?? NSNull()
    out["notice_help"] = row.noticeHelp as Any? ?? NSNull()
    return out
}

/// A layout as `project(ConversationLayout)` prints it, its tray rows with their
/// notices, and each person bubble with the notice the view shows under it
/// (`sendNotice`, as UIWindow's person row asks for it). The plain timeline's
/// layout has no store to ask: its bubbles carry no notice.
func projectSendLayout(_ layout: ConversationLayout, state: ConversationStoreState?,
                       conversationID: String) -> [String: Any] {
    var out = project(layout)
    out["tray"] = layout.tray.map(projectNoticeRow)
    out["items"] = layout.items.map { item -> [String: Any] in
        var row = project(item)
        if let state, case .person = item.content, let id = item.messageID {
            row["notice"] = state.sendNotice(conversationID: conversationID, messageID: id).map(project) as Any? ?? NSNull()
        }
        return row
    }
    return out
}

/// A timeline as `project(Timeline)` prints it, each turn with what a restored
/// row takes from its outbox entry besides the text: images and settings.
func projectRestoredTimeline(_ timeline: Timeline) -> [String: Any] {
    var out = project(timeline)
    out["turns"] = Dictionary(uniqueKeysWithValues: timeline.order.compactMap { id -> (String, Any)? in
        guard let turn = timeline.turn(id) else { return nil }
        var row = project(turn)
        row["attachments"] = turn.attachments
        row["settings"] = turn.settings.map(jsonObject) as Any? ?? NSNull()
        row["items"] = turn.items.count
        return (id, row)
    })
    return out
}

/// `send-layout <input.json>`: `{"conversation_id", "conversations": [ids], "steps": [...]}`.
/// Each step folds one thing into a `ConversationStoreState` as the app does:
/// `{"upsert": [Conversation]}`, `{"receipts": [Receipt]}`,
/// `{"page": <EventsPage>, "page_conversation"?}` (default the main conversation),
/// `{"local": {conversation_id?, message_id, text, attachments, settings?}}` (the
/// composer's row), `{"sends": [OutboxEntry]}` (the outbox's entries, set only when
/// they differ from what the state shows, as UIModel.applySends sets them),
/// `{"withdraw_local": <message id>}`, `{"focus": <id>|null}`. `"held": {id: true}`
/// says a block holds a conversation. After each step, for every listed
/// conversation: the store's layout (`layout(conversationID:held:)`) and the
/// timeline's own (`Timeline.layout`), every message's notice (`sendNotice`), the
/// stuck send, the send problem and the sidebar's, the timeline; and the create
/// notices, every notice the outbox gives (`sendNotices`), and for a `sends` step
/// whether it differed, the entries as Swift encodes them, and whether applying
/// them twice is applying them once (for `focus`, focusing twice).
func runSendLayout(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let main = input["conversation_id"]?.string ?? "cv"
    let watched = input["conversations"]?.array?.compactMap(\.string) ?? [main]
    var state = ConversationStoreState()
    var snapshots: [[String: Any]] = []
    for step in input["steps"]?.array ?? [] {
        var did: [String: Any] = [:]
        if let conversations = step["upsert"] {
            for conversation in try conversations.decode([Conversation].self) { state.upsert(conversation) }
        }
        if let receipts = step["receipts"] {
            for receipt in try receipts.decode([Receipt].self) { state.apply(receipt: receipt) }
        }
        if let page = step["page"] {
            did["page"] = pageResult(state.apply(events: try page.decode(EventsPage.self),
                                                 conversationID: step["page_conversation"]?.string ?? main))
        }
        if let local = step["local"] {
            state.addLocalMessage(conversationID: local["conversation_id"]?.string ?? main,
                                  messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "",
                                  attachments: local["attachments"]?.array?.compactMap(\.string) ?? [],
                                  settings: try local["settings"].flatMap { $0.isNull ? nil : $0 }
                                      .map { try $0.decode(ConversationSettings.self) })
        }
        if let sends = step["sends"] {
            let entries = try sends.decode([OutboxEntry].self)
            let differs = state.differs(sends: entries)
            if differs { state.apply(sends: entries) }
            var once = state
            once.apply(sends: entries)
            var twice = once
            twice.apply(sends: entries)
            did["differs"] = differs
            did["idempotent"] = twice == once
            did["encoded"] = entries.map(jsonObject)
        }
        if let withdrawn = step["withdraw_local"]?.string {
            state.withdrawLocal(messageID: withdrawn)
        }
        if step["focus"] != nil {
            state.focus(step["focus"]?.string)
            var again = state
            again.focus(step["focus"]?.string)
            did["idempotent"] = again == state
        }
        let sidebar = Dictionary(state.sidebarEntries().compactMap { entry -> (String, Any)? in
            guard case .conversation(let id) = entry.target else { return nil }
            return (id, entry.sendProblem?.rawValue as Any? ?? NSNull())
        }, uniquingKeysWith: { first, _ in first })
        var conversations: [String: Any] = [:]
        for id in watched {
            let held = step["held"]?[id]?.bool == true
            var out: [String: Any] = [
                "stuck": state.stuckSend(in: id).map(project) as Any? ?? NSNull(),
                "send_problem": state.sendProblem(in: id)?.rawValue as Any? ?? NSNull(),
                "sidebar": sidebar[id] ?? NSNull(),
                "held": held,
            ]
            var messages = Set(state.sends.values.filter { $0.conversation == id }.map(\.key))
            let timeline = state.timelines[id]
            messages.formUnion(timeline?.order ?? [])
            out["timeline"] = timeline.map(projectRestoredTimeline) as Any? ?? NSNull()
            out["plain"] = timeline.map { projectSendLayout($0.layout(held: held), state: nil, conversationID: id) }
                as Any? ?? NSNull()
            out["layout"] = state.layout(conversationID: id, held: held)
                .map { projectSendLayout($0, state: state, conversationID: id) } as Any? ?? NSNull()
            out["notices"] = Dictionary(uniqueKeysWithValues: messages.map { messageID in
                (messageID, state.sendNotice(conversationID: id, messageID: messageID).map(project) as Any? ?? NSNull())
            })
            conversations[id] = out
        }
        snapshots.append([
            "did": did, "focused": state.focusedConversationID as Any? ?? NSNull(), "conversations": conversations,
            "create_notices": state.createNotices.map(project),
            "send_notices": state.sendNotices.mapValues(project),
            "sends": state.sends.keys.sorted(),
        ])
    }
    return ["snapshots": snapshots]
}
