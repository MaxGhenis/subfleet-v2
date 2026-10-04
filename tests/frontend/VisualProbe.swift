import Foundation

@main struct VisualProbe {
    static func main() throws {
        if CommandLine.arguments[1] == "contrast" {
            var values: [[String: Any]] = []
            for (name, foreground, target) in [("primary", Theme.text.primary, 4.5),
                                              ("secondary", Theme.text.secondary, 4.5),
                                              ("tertiary", Theme.text.tertiary, 3.0)] {
                for (i, surface) in Theme.surface.all.enumerated() {
                    values.append(["text": name, "surface": i, "mode": "dark", "target": target,
                                   "contrast": Theme.contrast(foreground.dark, surface.dark)])
                    values.append(["text": name, "surface": i, "mode": "light", "target": target,
                                   "contrast": Theme.contrast(foreground.light, surface.light)])
                }
            }
            print(String(data: try JSONSerialization.data(withJSONObject: values), encoding: .utf8)!)
            return
        }
        if CommandLine.arguments[1] == "account" {
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])))
            let served = Served(fields: ["lane_id": .string("claude-2"), "account": .string("max@example.com")])
            let fresh = AccountUsage.make(provider: "claude", served: served, laneID: nil, snapshot: snapshot,
                                          now: snapshotDate(snapshot.generated_at)!)!
            let stale = AccountUsage.make(provider: "claude", served: served, laneID: nil, snapshot: snapshot,
                                          now: snapshotDate(snapshot.generated_at)!.addingTimeInterval(601))!
            let other = AccountUsage.make(provider: "claude", served: Served(fields: ["lane_id": .string("other")]),
                                          laneID: nil, snapshot: snapshot)!
            print(String(data: try JSONSerialization.data(withJSONObject: ["fresh": fresh.words, "stale": stale.words,
                  "other": other.words]), encoding: .utf8)!)
            return
        }
        let events = try JSONDecoder().decode([ConversationEvent].self,
            from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var timeline = Timeline(conversationID: "c0")
        timeline.apply(receipt: Receipt(message_id: "m1", state: "running", text: "Please review"))
        _ = timeline.apply(events: events)
        func describe(_ timeline: Timeline) -> [String: Any] {
            let rows = WorkPresentation.rows(in: timeline)
            let groups = rows.compactMap { if case .work(let group) = $0 { return group }; return nil }
            return ["groups": groups.map { ["label": $0.label, "count": $0.count, "failed": $0.failed,
                  "running": $0.running?.label as Any? ?? NSNull(), "items": $0.items.count] },
                    "text": rows.compactMap { if case .item(let item) = $0, case .text(let text, _) = item.content { return text }; return nil },
                    "tools": groups.flatMap(\.tools).map(\.label)]
        }
        let live = describe(timeline)
        _ = timeline.apply(events: [ConversationEvent(seq: events.count + 1, message_id: "m1", kind: "turn.completed",
            ts: "2026-10-04T10:03:12Z", data: .object(["state": .string("succeeded")]))])
        timeline.apply(receipt: Receipt(message_id: "m1", state: "complete"))
        let finished = describe(timeline)
        let read = ToolActivity(name: "Read", summary: "file_path: /repo/app/Sources/UIWindow.swift", hidden: false,
                                state: .succeeded).label
        let hidden = ToolActivity(name: "Bash", summary: "secret", hidden: true, state: .succeeded).label
        // Adjacent mixed tools, a visible summary without tools, and a pending card boundary.
        let edited = ToolActivity(name: "Edit", summary: "file_path: /repo/a.swift", hidden: false, state: .succeeded)
        let mixed = WorkGroup(id: "mixed", items: [TimelineItem(id: "edit", content: .tool(edited))], completed: false, duration: nil)
        print(String(data: try JSONSerialization.data(withJSONObject: ["live": live, "finished": finished,
              "read": read, "hidden": hidden, "edit": mixed.label]), encoding: .utf8)!)
    }
}
