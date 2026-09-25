// Scenario subcommands of the core probe: the outbox against a socket, the
// store's state, staged images, drafts, and connecting through the endpoint.
import Darwin
import Foundation

final class ProbeClock {
    var now = Date(timeIntervalSince1970: 1_790_000_000)
}

func fileMode(_ path: String) -> String {
    var info = stat()
    guard stat(path, &info) == 0 else { return "missing" }
    return String(info.st_mode & 0o777, radix: 8)
}

func project(_ entry: OutboxEntry) -> [String: Any] {
    [
        "key": entry.key, "kind": entry.kind.rawValue, "order": entry.order, "conversation": entry.conversation,
        "state": entry.state.rawValue, "attempts": entry.attempts,
        "last_after": entry.lastAfterMessageID as Any? ?? NSNull(),
        "waiting_for_predecessor": entry.waitingForPredecessor,
        "next_attempt_at": entry.nextAttemptAt as Any? ?? NSNull(),
        "failure": entry.failure.map { ["code": $0.code as Any? ?? NSNull(), "reason": $0.reason as Any? ?? NSNull(),
                                        "message": $0.message, "retryable": $0.retryable] } as Any? ?? NSNull(),
        "receipt": entry.receipt.map(jsonObject) as Any? ?? NSNull(),
        "conversation_id": entry.conversationID as Any? ?? NSNull(),
        "text": entry.message?.text as Any? ?? NSNull(),
    ]
}

func project(_ report: OutboxSender.Report) -> [String: Any] {
    ["sent": report.sent, "acknowledged": report.acknowledged, "failed": report.failed, "retrying": report.retrying,
     "resynced": report.resynced, "receipts": report.receipts.map(jsonObject),
     "conversations": report.conversations.map { $0.conversation_id }]
}

func project(_ outcome: OutboxSender.WithdrawOutcome) -> [String: Any] {
    switch outcome {
    case .withdrawn(let receipt): return ["outcome": "withdrawn", "receipt": receipt.map(jsonObject) as Any? ?? NSNull()]
    case .inDaemon(let receipt): return ["outcome": "in-daemon", "receipt": jsonObject(receipt)]
    case .inFlight: return ["outcome": "in-flight"]
    }
}

/// `outbox <socket> <journal> <steps.json>`: run outbox steps against a daemon socket.
/// `@conv:<request id>` names the conversation that create made; `@draft:<request id>` its draft key.
func runOutbox(socket: String, journal: String, stepsData: Data) throws -> [String: Any] {
    let client = DaemonClient(transport: UnixSocketTransport(path: socket))
    client.baseTimeout = 3
    var calls: [String] = []
    client.onExchange = { op, request, response in
        let args = (try? JSONValue.parse(request))?["args"]
        let key = args?["message_id"]?.string ?? args?["request_id"]?.string ?? ""
        let after = args?["after_message_id"].map { $0.isNull ? "null" : ($0.string ?? "") }
        calls.append([op, key, after.map { "after=" + $0 } ?? "", response == nil ? "no-answer" : "answered"]
            .filter { !$0.isEmpty }.joined(separator: " "))
    }
    let clock = ProbeClock()
    let url = URL(fileURLWithPath: journal)
    var outbox = try Outbox(url: url, now: { clock.now })
    var sender = OutboxSender(outbox: outbox, client: client)
    var results: [Any] = []

    func resolve(_ value: String?) -> String {
        guard let value else { return "" }
        if value.hasPrefix("@conv:") { return outbox.entry(String(value.dropFirst(6)))?.conversationID ?? value }
        if value.hasPrefix("@draft:") { return Outbox.draftKey(String(value.dropFirst(7))) }
        return value
    }

    for step in try JSONValue.parse(stepsData).array ?? [] {
        let action = step["do"]?.string ?? ""
        do {
            switch action {
            case "create":
                let settings = try step["settings"]?.decode(ConversationSettings.self) ?? ConversationSettings(model: "opus[1m]")
                let entry = try outbox.enqueueCreate(ConversationCreateArgs(
                    request_id: step["request_id"]?.string ?? UUID().uuidString, provider: step["provider"]?.string ?? "claude",
                    workspace: step["workspace"]?.string ?? "", settings: settings))
                results.append(["do": action, "key": entry.key, "conversation": entry.conversation])
            case "submit":
                let settings = try step["settings"]?.decode(ConversationSettings.self) ?? ConversationSettings(model: "opus[1m]")
                let entry = try outbox.enqueueSubmit(conversation: resolve(step["conversation"]?.string),
                                                     messageID: step["message_id"]?.string ?? Outbox.newMessageID(),
                                                     text: step["text"]?.string ?? "", settings: settings)
                results.append(["do": action, "key": entry.key, "conversation": entry.conversation])
            case "pump":
                results.append(["do": action, "report": project(sender.pump())])
            case "reload":
                outbox = try Outbox(url: url, now: { clock.now })
                sender = OutboxSender(outbox: outbox, client: client)
                results.append(["do": action, "states": outbox.entries.map { $0.state.rawValue }])
            case "begin":
                // Journal a send as under way without sending it: the app stopped here.
                _ = try outbox.begin(resolve(step["key"]?.string))
                results.append(["do": action])
            case "send-then-crash":
                // Send it, and lose the answer: the daemon has it, the journal says `sending`.
                let key = resolve(step["key"]?.string)
                if case .submit(let args) = try outbox.begin(key) {
                    let receipt = try client.call(Ops.messageSubmit, args)
                    results.append(["do": action, "receipt": jsonObject(receipt)])
                }
            case "fail":
                let key = resolve(step["key"]?.string)
                let entry = try outbox.finish(key, .failed(.timedOut(op: "message.submit", seconds: 15)))
                results.append(["do": action, "entry": project(entry)])
            case "withdraw":
                results.append(["do": action, "result": project(try sender.withdraw(resolve(step["key"]?.string)))])
            case "know_chain":
                try outbox.knowChain(resolve(step["conversation"]?.string), lastPersonMessageID: step["last"]?.string)
                results.append(["do": action])
            case "retry":
                try outbox.retry(resolve(step["key"]?.string))
                results.append(["do": action])
            case "advance":
                clock.now = clock.now.addingTimeInterval(step["seconds"]?.double ?? 0)
                results.append(["do": action])
            case "sendable":
                results.append(["do": action, "keys": outbox.sendable().map(\.key)])
            case "engine-pump":
                // The app's pump: send, then keep only the newest `keep` closed entries.
                let engine = ConversationEngine(client: client, outbox: outbox)
                engine.keptClosedEntries = step["keep"]?.int ?? engine.keptClosedEntries
                results.append(["do": action, "report": project(engine.pump())])
            case "stop":
                // Stop as the app does it, from the state the app last saw.
                let key = resolve(step["key"]?.string)
                let engine = ConversationEngine(client: client, outbox: outbox)
                let stop = stopAction(for: key, state: step["state"]?.string, outboxEntry: outbox.entry(key))
                let receipt = try engine.stop(stop)
                results.append(["do": action, "action": project(stop), "receipt": receipt.map(jsonObject) as Any? ?? NSNull()])
            case "raw-submit":
                // A late copy of a journaled submit reaching the daemon (for tombstones).
                let key = resolve(step["key"]?.string)
                guard let entry = outbox.entry(key), let message = entry.message else { throw OutboxError.unknownEntry(key) }
                let receipt = try client.call(Ops.messageSubmit, MessageSubmitArgs(
                    conversation_id: entry.conversation, message_id: key, after_message_id: step["after"]?.string,
                    text: message.text, attachments: message.attachments, settings: message.settings))
                results.append(["do": action, "receipt": jsonObject(receipt)])
            default:
                results.append(["do": action, "error": "unknown step"])
            }
        } catch {
            results.append(["do": action, "error": describe(error)])
        }
    }
    return [
        "results": results, "calls": calls, "entries": outbox.entries.map(project),
        "chains": outbox.journal.chains.mapValues { $0.lastPersonMessageID as Any? ?? NSNull() },
        "journal_mode": fileMode(journal), "directory_mode": fileMode(url.deletingLastPathComponent().path),
    ]
}

// MARK: - Store state

func project(_ entry: SidebarEntry) -> [String: Any] {
    var target: Any
    switch entry.target {
    case .conversation(let id): target = ["conversation": id]
    case .native(let native): target = ["native": jsonObject(native)]
    }
    return ["id": entry.id, "target": target, "provider": entry.provider, "title": entry.title, "subtitle": entry.subtitle,
            "pending": entry.pendingApprovals, "active": entry.active, "blocked_by": entry.blockedBy as Any? ?? NSNull(),
            "live_elsewhere": entry.liveElsewhere, "continuable": entry.continuable,
            "continue_blocker": entry.continueBlocker as Any? ?? NSNull()]
}

func project(_ options: ComposerOptions) -> [String: Any] {
    ["models": options.models.map { ["label": $0.label, "value": $0.value] },
     "selected": options.selectedModel?.short as Any? ?? NSNull(), "efforts": options.efforts,
     "efforts_observed": options.effortsObserved, "fast_supported": options.fastSupported as Any? ?? NSNull(),
     "fast_note": options.fastNote,
     "permissions": options.permissions.map { ["policy": $0.policy.rawValue, "enabled": $0.enabled, "widens": $0.widens,
                                                "reason": $0.disabledReason as Any? ?? NSNull()] as [String: Any] }]
}

func project(_ banner: BlockedBanner) -> [String: Any] {
    ["title": banner.title, "detail": banner.detail, "choices": banner.choices.map { choice -> [String: Any] in
        var action: [String: Any]
        switch choice.action {
        case .unblock(let value): action = ["unblock": value.rawValue]
        case .resolve(let messageID, let resolution): action = ["resolve": resolution.rawValue, "message_id": messageID]
        }
        return ["label": choice.label, "action": action]
    }]
}

func project(_ action: StopAction) -> [String: Any] {
    switch action {
    case .none: return ["action": "none"]
    case .withdraw(let id): return ["action": "withdraw", "message_id": id]
    case .cancel(let id): return ["action": "cancel", "message_id": id]
    case .interrupt(let id): return ["action": "interrupt", "message_id": id]
    }
}

/// `store <input.json>`: fold a sequence of daemon answers into the store state.
func runStore(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var state = ConversationStoreState()
    var log: [Any] = []
    for step in input["steps"]?.array ?? [] {
        if let list = step["list"] { state.apply(list: try list.decode(ConversationListResult.self)); log.append("list") }
        if let models = step["models"], let provider = step["provider"]?.string {
            state.apply(models: try models.decode(ModelsListResult.self), provider: provider)
            log.append("models")
        }
        if let capabilities = step["capabilities"] {
            state.availability = DaemonAvailability.judge(try capabilities.decode(Capabilities.self))
            log.append("capabilities")
        }
        if let open = step["open"] { state.apply(open: try open.decode(ConversationOpenResult.self)); log.append("open") }
        if let page = step["events"], let cid = step["conversation_id"]?.string {
            log.append(pageResult(state.apply(events: try page.decode(EventsPage.self), conversationID: cid)))
        }
        if let page = step["watch"] {
            switch state.apply(watch: try page.decode(WatchPage.self)) {
            case .applied(let count): log.append("watch:\(count)")
            case .superseded: log.append("watch:superseded")
            }
        }
        if let status = step["status"] {
            state.laneLabels = laneLabels(from: try JSONDecoder().decode(Snapshot.self, from: JSONEncoder().encode(status)))
            log.append("status")
        }
        if step["focus"] != nil { state.focus(step["focus"]?.string); log.append("focus") }
        if let query = step["search"]?.string { state.searchQuery = query }
        if step["provider_filter"] != nil { state.providerFilter = step["provider_filter"]?.string }
        if let grouping = step["grouping"]?.string { state.grouping = SidebarGrouping(rawValue: grouping) ?? .recency }
    }
    let now = Date(timeIntervalSince1970: input["now"]?.double ?? Date().timeIntervalSince1970)
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "UTC")!
    var out: [String: Any] = [
        "log": log,
        "sidebar": state.sidebar(now: now, calendar: calendar).map { ["title": $0.title, "entries": $0.entries.map(project)] },
        "badge": state.pendingApprovalCount,
        "notifications": state.notifications.map { ["id": $0.id, "kind": $0.kind.rawValue, "conversation": $0.conversationID,
                                                     "title": $0.title, "body": $0.body] },
        "watch_cursor": state.watchCursor, "watch_baselined": state.watchBaselined,
        "focused": state.focusedConversationID as Any? ?? NSNull(),
        "conversations": state.conversations.map { ["id": $0.conversation_id, "active": $0.active,
                                                     "pending": $0.pending_approvals,
                                                     "last_state": $0.last_message?.state as Any? ?? NSNull()] as [String: Any] },
    ]
    var composer: [String: Any] = [:]
    var banners: [String: Any] = [:]
    var chips: [String: Any] = [:]
    var stops: [String: Any] = [:]
    var statuses: [String: Any] = [:]
    for conversation in state.conversations {
        let id = conversation.conversation_id
        composer[id] = state.composerOptions(for: id).map(project) ?? NSNull()
        banners[id] = state.blockedBanner(for: id).map(project) ?? NSNull()
        if let timeline = state.timelines[id] {
            for messageID in timeline.order {
                if let chip = state.servedChip(conversationID: id, messageID: messageID) {
                    chips[messageID] = ["account": chip.account as Any? ?? NSNull(), "model": chip.model as Any? ?? NSNull(),
                                        "effort": chip.effort as Any? ?? NSNull(), "fast": chip.fast as Any? ?? NSNull(),
                                        "warnings": chip.warnings]
                }
                stops[messageID] = project(stopAction(for: messageID, state: timeline.turn(messageID)?.state, outboxEntry: nil))
                statuses[messageID] = timeline.turn(messageID)?.statusText ?? NSNull()
            }
        }
    }
    out["composer"] = composer
    out["banners"] = banners
    out["chips"] = chips
    out["stops"] = stops
    out["statuses"] = statuses
    return out
}

// MARK: - Dispatch

func extraCommand(_ arguments: [String]) throws -> Any? {
    switch arguments[1] {
    case "outbox":
        return try runOutbox(socket: arguments[2], journal: arguments[3], stepsData: readFile(arguments[4]))
    case "store":
        return try runStore(readFile(arguments[2]))
    case "stage":
        // stage <image> <directory>
        do {
            let staged = try AttachmentStager.stage(readFile(arguments[2]), in: URL(fileURLWithPath: arguments[3]))
            return ["staged": jsonObject(staged), "mode": fileMode(staged.path), "directory_mode": fileMode(arguments[3])]
        } catch {
            return ["error": "\(error)"]
        }
    case "drafts":
        // drafts <directory> <key>: save, load, delete
        let store = DraftStore(directory: URL(fileURLWithPath: arguments[2]))
        let draft = Draft(text: "half a thought\nwith two lines", attachments: [], settings: ConversationSettings(model: "opus[1m]"),
                          updated_at: "2026-09-24T12:00:00.000Z")
        try store.save(draft, for: arguments[3])
        let path = store.fileURL(for: arguments[3]).path
        let loaded = store.load(arguments[3])
        let mode = fileMode(path)
        store.delete(arguments[3])
        return ["path": path, "mode": mode, "round_trip": loaded == draft, "deleted": store.load(arguments[3]) == nil]
    case "connect-current":
        // connect-current <home> <flavor> [SUBFLEET_HOME]: what the app does at launch
        let environment = arguments.count > 4 ? ["SUBFLEET_HOME": arguments[4]] : [:]
        let flavor = BuildFlavor(rawValue: arguments[3]) ?? .release
        let availability: DaemonAvailability
        do {
            let client = try DaemonClient.forCurrentEndpoint(environment: environment, home: URL(fileURLWithPath: arguments[2]),
                                                             flavor: flavor)
            client.baseTimeout = 3
            availability = DaemonAvailability.check(client)
        } catch DaemonClientError.endpointRefused(let reason) {
            availability = .refused(reason)
        }
        switch availability {
        case .ready(let capabilities): return ["ready": capabilities.capabilities]
        case .down(let detail): return ["down": detail, "banner": availability.banner?.title ?? ""]
        case .incompatible(let detail): return ["incompatible": detail]
        case .refused(let detail): return ["refused": detail, "banner": availability.banner?.title ?? ""]
        case .unknown: return ["unknown": true]
        }
    case "live":
        return try runLive(arguments)
    default:
        return nil
    }
}
