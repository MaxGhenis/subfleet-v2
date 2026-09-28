// The core probe's queue-tray commands (design §12, C-29.7): the conversation
// view's split into timeline and tray, how it keeps the end in view, and the
// tray's words. Foundation only; part of the core probe (conftest.CORE_PROBE).
import Foundation

func project(_ row: QueuedMessage) -> [String: Any] {
    ["id": row.id, "origin": row.origin as Any? ?? NSNull(), "preview": row.preview,
     "text": row.text as Any? ?? NSNull(), "attachments": row.attachments, "sending": row.sending,
     "status": row.status as Any? ?? NSNull(), "withdraw": project(row.withdraw), "can_withdraw": row.canWithdraw,
     "can_steer": row.canSteer]
}

func project(_ layout: ConversationLayout) -> [String: Any] {
    ["items": layout.items.map(project), "tray": layout.tray.map(project),
     "followed": layout.followed?.id as Any? ?? NSNull(), "held": layout.held, "live": layout.live,
     "settling": layout.settling, "has_history": layout.hasHistory,
     "title": layout.trayTitle as Any? ?? NSNull()]
}

func project(_ key: FollowKey) -> [String: Any] {
    ["last": key.last as Any? ?? NSNull(), "last_is_own_send": key.lastIsOwnSend,
     "followed": key.followed as Any? ?? NSNull(), "followed_length": key.followedLength, "tray": key.tray,
     "settling": key.settling, "has_history": key.hasHistory]
}

func followKey(_ value: JSONValue) -> FollowKey {
    FollowKey(last: value["last"]?.string, lastIsOwnSend: value["last_is_own_send"]?.bool ?? false,
              followed: value["followed"]?.string, followedLength: Int(value["followed_length"]?.double ?? 0),
              tray: value["tray"]?.array?.compactMap(\.string) ?? [], settling: value["settling"]?.bool ?? false,
              hasHistory: value["has_history"]?.bool ?? false)
}

func moveWord(_ move: FollowKey.Move) -> String {
    switch move {
    case .stay: return "stay"
    case .jump: return "jump"
    case .glide: return "glide"
    }
}

/// `queue <input.json>`: `{"conversation_id", "steps": [...]}` with the fold's
/// steps (`page`, `receipts`, `approvals`, `history`, `local`) and these:
/// `{"withdraw_local": <message id>}` (the app withdrew a message the daemon
/// never had), `{"focus": true}` (the conversation is focused again and reads
/// its log from the cursor). Every step may carry `held` (a block holds the
/// conversation), `steer` (the daemon steers its provider) and `at_bottom` (the
/// end of the timeline is on screen; default true). After each step the view's
/// layout and follow key are computed as the conversation view computes them,
/// and the move from the previous key; a step with `"snapshot": true` also
/// records the timeline and its layout.
func runQueue(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var timeline = Timeline(conversationID: input["conversation_id"]?.string ?? "cv")
    var key = FollowKey.empty
    var moves: [String] = []
    var snapshots: [[String: Any]] = []
    for step in input["steps"]?.array ?? [] {
        if let page = step["page"] {
            timeline.apply(page: try page.decode(EventsPage.self))
        } else if let receipts = step["receipts"] {
            timeline.apply(receipts: try receipts.decode([Receipt].self))
        } else if let approvals = step["approvals"] {
            timeline.attach(approvals: try approvals.decode([ApprovalView].self))
        } else if let history = step["history"] {
            timeline.apply(history: try history.decode(HistoryPage.self))
        } else if let local = step["local"] {
            timeline.addLocal(messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "",
                              attachments: local["attachments"]?.array?.compactMap(\.string) ?? [])
        } else if let id = step["withdraw_local"]?.string {
            timeline.withdrawLocal(messageID: id)
        } else if step["focus"]?.bool == true {
            timeline.beginReading()
        }
        let layout = timeline.layout(held: step["held"]?.bool == true, steerOffered: step["steer"]?.bool == true)
        let next = layout.followKey(length: queueProbeLength)
        moves.append(moveWord(FollowKey.move(from: key, to: next, atBottom: step["at_bottom"]?.bool ?? true)))
        key = next
        if step["snapshot"]?.bool == true {
            snapshots.append(["timeline": project(timeline), "layout": project(layout), "key": project(next)])
        }
    }
    let layout = timeline.layout(held: false)
    return ["timeline": project(timeline), "layout": project(layout), "moves": moves, "snapshots": snapshots]
}

/// The length the view follows: how much text a text or thinking row holds
/// (`streamedLength` in the view, which the core probe does not compile).
func queueProbeLength(_ item: TimelineItem) -> Int {
    switch item.content {
    case .text(let text, _), .thinking(let text, _): return text.count
    default: return 0
    }
}

/// `queue-words <input.json>`: the tray's pure helpers, each over a list of
/// inputs: `previews` [{text, attachments}], `statuses` [reason|null],
/// `titles` [{rows: [{origin}], live, held}], `visible` [{count, expanded, limit?}],
/// `moves` [{old, new, at_bottom}] (follow keys as `project(FollowKey)` prints
/// them), `stops` [{message_id, state}] (the control under a message) and
/// `steer` [[{id, origin, sending}]].
func runQueueWords(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    let previews = (input["previews"]?.array ?? []).map { value -> String in
        queuePreview(value["text"]?.string, attachments: Int(value["attachments"]?.double ?? 0))
    }
    let statuses = (input["statuses"]?.array ?? []).map { value -> Any in
        queueStatusWords(value.string) as Any? ?? NSNull()
    }
    let titles = (input["titles"]?.array ?? []).map { value -> Any in
        let rows = (value["rows"]?.array ?? []).enumerated().map { index, row in
            QueuedMessage(id: "m\(index)", origin: row["origin"]?.string, preview: "", text: nil, attachments: 0,
                          sending: false, status: nil, withdraw: .none)
        }
        return queueTrayTitle(rows, live: value["live"]?.bool == true, held: value["held"]?.bool == true)
            as Any? ?? NSNull()
    }
    let visible = (input["visible"]?.array ?? []).map { value -> [Int] in
        let shown = queueTrayVisible(count: Int(value["count"]?.double ?? 0), expanded: value["expanded"]?.bool == true,
                                     limit: Int(value["limit"]?.double ?? 4))
        return [shown.shown, shown.hidden]
    }
    let moves = (input["moves"]?.array ?? []).map { value -> String in
        moveWord(FollowKey.move(from: followKey(value["old"] ?? .null), to: followKey(value["new"] ?? .null),
                                atBottom: value["at_bottom"]?.bool ?? true))
    }
    let stops = (input["stops"]?.array ?? []).map { value -> [String: Any] in
        let action = stopAction(for: value["message_id"]?.string ?? "", state: value["state"]?.string, outboxEntry: nil)
        return ["action": project(action), "label": stopLabel(action) as Any? ?? NSNull()]
    }
    let steer = (input["steer"]?.array ?? []).map { value -> Any in
        let rows = (value.array ?? []).map { row in
            QueuedMessage(id: row["id"]?.string ?? "", origin: row["origin"]?.string, preview: "", text: nil,
                          attachments: 0, sending: row["sending"]?.bool == true, status: nil, withdraw: .none)
        }
        return steerCandidate(rows) as Any? ?? NSNull()
    }
    return ["previews": previews, "statuses": statuses, "titles": titles, "visible": visible, "moves": moves,
            "stops": stops, "steer": steer, "preview_limit": queuePreviewLimit]
}
