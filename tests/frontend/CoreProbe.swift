// Foundation-only probe of the app's non-UI core (app/Sources, SUBFLEET_MODEL_TEST).
// Each subcommand reads JSON the tests wrote and prints JSON the tests assert on.
// `live` drives a real development daemon through the same engine the app uses.
import Foundation

// MARK: - Output helpers

let sortedEncoder: JSONEncoder = {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return encoder
}()

func emit(_ value: Any) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
    print(String(data: data, encoding: .utf8)!)
}

func jsonObject<T: Encodable>(_ value: T) -> Any {
    let data = try! sortedEncoder.encode(value)
    return try! JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
}

func readFile(_ path: String) -> Data {
    guard let data = FileManager.default.contents(atPath: path) else {
        FileHandle.standardError.write("cannot read \(path)\n".data(using: .utf8)!)
        exit(2)
    }
    return data
}

func describe(_ error: Error) -> [String: Any] {
    switch error {
    case let error as DaemonClientError:
        switch error {
        case .daemon(let refusal):
            return ["kind": "daemon", "code": refusal.code, "message": refusal.message,
                    "reason": refusal.reason as Any? ?? NSNull(), "detail": refusal.detail, "fix": refusal.fix as Any? ?? NSNull()]
        case .timedOut(let op, let seconds): return ["kind": "timedOut", "op": op, "seconds": seconds]
        case .unavailable(let reason): return ["kind": "unavailable", "message": reason]
        case .transport(let reason): return ["kind": "transport", "message": reason]
        case .malformed(let reason): return ["kind": "malformed", "message": reason]
        case .requestTooLarge(let bytes): return ["kind": "requestTooLarge", "bytes": bytes]
        case .endpointRefused(let reason): return ["kind": "endpointRefused", "message": reason]
        }
    case let error as OutboxError: return ["kind": "outbox", "message": "\(error)"]
    case let error as ConversationEngineError: return ["kind": "engine", "message": "\(error)"]
    default: return ["kind": "other", "message": "\(error)"]
    }
}

// MARK: - Op codecs

struct OpCodec {
    let name: String
    let requestLine: (Data, String) throws -> Data
    let roundTrip: (Data) throws -> Data
    let timeout: (Data) throws -> TimeInterval
    let decodeResponse: (Data, String) throws -> Data
    let call: (DaemonCalling, Data) throws -> Data
}

func codec<A: Codable, R: Codable>(_ op: DaemonOperation<A, R>) -> OpCodec {
    OpCodec(
        name: op.name,
        requestLine: { args, id in try DaemonClient.requestLine(op: op.name, id: id, args: JSONDecoder().decode(A.self, from: args)) },
        roundTrip: { data in try sortedEncoder.encode(JSONDecoder().decode(R.self, from: data)) },
        timeout: { args in op.timeout(for: try JSONDecoder().decode(A.self, from: args)) },
        decodeResponse: { line, id in
            let result: R = try DaemonClient.decodeResponse(line, id: id, op: op.name)
            return try sortedEncoder.encode(result)
        },
        call: { client, args in try sortedEncoder.encode(client.call(op, JSONDecoder().decode(A.self, from: args))) })
}

let codecs: [String: OpCodec] = {
    let all = [
        codec(Ops.capabilities), codec(Ops.conversationList), codec(Ops.conversationOpen), codec(Ops.conversationCreate),
        codec(Ops.conversationSettings), codec(Ops.conversationUnblock), codec(Ops.conversationHistory),
        codec(Ops.conversationEvents), codec(Ops.conversationWatch), codec(Ops.messageSubmit), codec(Ops.messageStatus),
        codec(Ops.messageCancel), codec(Ops.turnInterrupt), codec(Ops.messageResolve), codec(Ops.approvalList),
        codec(Ops.approvalGet), codec(Ops.approvalRespond), codec(Ops.attachmentAdd), codec(Ops.catalogRefresh),
        codec(Ops.modelsList), codec(Ops.conversationRuns),
    ]
    return Dictionary(uniqueKeysWithValues: all.map { ($0.name, $0) })
}()

func opCodec(_ name: String) -> OpCodec {
    guard let codec = codecs[name] else {
        FileHandle.standardError.write("no codec for \(name)\n".data(using: .utf8)!)
        exit(2)
    }
    return codec
}

// MARK: - Timeline and Markdown projections

func project(_ inlines: [MarkdownInline]) -> [Any] {
    inlines.map { inline -> Any in
        switch inline {
        case .text(let text): return ["text": text]
        case .code(let text): return ["code": text]
        case .emphasis(let inner): return ["emphasis": project(inner)]
        case .strong(let inner): return ["strong": project(inner)]
        case .strikethrough(let inner): return ["strike": project(inner)]
        case .link(let label, let destination): return ["link": destination, "label": project(label)]
        case .image(let alt, let source): return ["image": source, "alt": alt]
        case .softBreak: return ["break": "soft"]
        case .hardBreak: return ["break": "hard"]
        }
    }
}

func project(_ blocks: [MarkdownBlock]) -> [Any] {
    blocks.map { block -> Any in
        switch block {
        case .heading(let level, let content): return ["type": "heading", "level": level, "content": project(content)]
        case .paragraph(let content): return ["type": "paragraph", "content": project(content)]
        case .list(let ordered, let start, let tight, let items):
            return ["type": "list", "ordered": ordered, "start": start, "tight": tight,
                    "items": items.map { ["blocks": project($0.blocks), "checked": $0.checked as Any? ?? NSNull()] as [String: Any] }]
        case .code(let language, let text, let closed):
            return ["type": "code", "language": language as Any? ?? NSNull(), "text": text, "closed": closed]
        case .quote(let inner): return ["type": "quote", "blocks": project(inner)]
        case .table(let header, let alignments, let rows):
            return ["type": "table", "header": header.map(project), "alignments": alignments.map(\.rawValue),
                    "rows": rows.map { $0.map(project) }]
        case .rule: return ["type": "rule"]
        }
    }
}

func project(_ card: ApprovalCard) -> [String: Any] {
    let state: String
    switch card.state {
    case .pending: state = "pending"
    case .answered(let decision): state = "answered:" + (decision ?? "")
    case .withdrawn: state = "withdrawn"
    }
    return ["request_id": card.requestID as Any? ?? NSNull(), "approval_id": card.approvalID as Any? ?? NSNull(),
            "kind": card.kind, "options": card.options, "state": state, "display": jsonObject(card.display),
            "shown": card.display.shownFields.map { [$0.key, $0.value] },
            "questions": card.questions.map { $0.question }]
}

func project(_ item: TimelineItem) -> [String: Any] {
    var out: [String: Any] = ["id": item.id, "message_id": item.messageID as Any? ?? NSNull()]
    switch item.content {
    case .history(let role, let text, let tool):
        out["type"] = "history"; out["role"] = role; out["text"] = text; out["tool"] = tool as Any? ?? NSNull()
    case .person(let text, let attachments, let state):
        out["type"] = "person"; out["text"] = text as Any? ?? NSNull(); out["attachments"] = attachments; out["state"] = state
    case .text(let text, let final):
        out["type"] = "text"; out["text"] = text; out["final"] = final
    case .thinking(let text, let final):
        out["type"] = "thinking"; out["text"] = text; out["final"] = final
    case .tool(let tool):
        out["type"] = "tool"; out["name"] = tool.name; out["summary"] = tool.summary; out["hidden"] = tool.hidden
        out["state"] = tool.state.rawValue; out["preview"] = tool.preview as Any? ?? NSNull()
    case .approval(let card):
        out["type"] = "approval"; out["card"] = project(card)
    case .error(let message, let kind, let willRetry):
        out["type"] = "error"; out["message"] = message; out["kind"] = kind as Any? ?? NSNull(); out["will_retry"] = willRetry
    case .notice(let text):
        out["type"] = "notice"; out["text"] = text
    }
    return out
}

func project(_ turn: TurnTimeline) -> [String: Any] {
    [
        "message_id": turn.messageID, "seq": turn.seq as Any? ?? NSNull(), "state": turn.state,
        "state_reason": turn.stateReason as Any? ?? NSNull(), "origin": turn.origin as Any? ?? NSNull(),
        "person_text": turn.personText as Any? ?? NSNull(), "phases": turn.phases.map(\.phase), "accepted": turn.accepted,
        "served": jsonObject(turn.served), "outcome": turn.outcome.map { ["state": $0.state, "reason": $0.reason as Any? ?? NSNull(),
                                                                           "served_model": $0.servedModel as Any? ?? NSNull()] } as Any? ?? NSNull(),
        "limits": turn.limits.map(jsonObject) as Any? ?? NSNull(), "diff": turn.diff as Any? ?? NSNull(),
        "status_text": turn.statusText, "streaming": turn.isStreaming, "pending_approvals": turn.pendingApprovals.count,
    ]
}

func project(_ timeline: Timeline) -> [String: Any] {
    [
        "cursor": timeline.cursor, "resets": timeline.resets, "order": timeline.order,
        "items": timeline.items.map(project), "turns": Dictionary(uniqueKeysWithValues: timeline.order.compactMap { id in
            timeline.turn(id).map { (id, project($0)) } }),
        "history_before": timeline.historyBefore as Any? ?? NSNull(), "history_complete": timeline.historyComplete,
        "unknown_kinds": timeline.unknownKinds, "pending_cards": timeline.pendingApprovalCards.map(project),
        "live_message": timeline.liveMessageID as Any? ?? NSNull(),
    ]
}

func pageResult(_ result: Timeline.PageResult) -> String {
    switch result {
    case .applied(let count): return "applied:\(count)"
    case .reset: return "reset"
    case .superseded: return "superseded"
    }
}

/// `{"conversation_id", "steps": [{"page"}|{"receipts"}|{"approvals"}|{"history"}|{"local"}]}`
func runFold(_ data: Data) throws -> [String: Any] {
    let input = try JSONValue.parse(data)
    var timeline = Timeline(conversationID: input["conversation_id"]?.string ?? "cv")
    var results: [String] = []
    var snapshots: [[String: Any]] = []
    for step in input["steps"]?.array ?? [] {
        if let page = step["page"] {
            results.append(pageResult(timeline.apply(page: try page.decode(EventsPage.self))))
        } else if let receipts = step["receipts"] {
            timeline.apply(receipts: try receipts.decode([Receipt].self))
            results.append("receipts")
        } else if let approvals = step["approvals"] {
            timeline.attach(approvals: try approvals.decode([ApprovalView].self))
            results.append("approvals")
        } else if let history = step["history"] {
            timeline.apply(history: try history.decode(HistoryPage.self))
            results.append("history")
        } else if let local = step["local"] {
            timeline.addLocal(messageID: local["message_id"]?.string ?? "", text: local["text"]?.string ?? "")
            results.append("local")
        }
        if step["snapshot"]?.bool == true { snapshots.append(project(timeline)) }
    }
    var out = project(timeline)
    out["results"] = results
    out["snapshots"] = snapshots
    return out
}

// MARK: - Main

@main
struct CoreProbe {
    static func main() throws {
        let arguments = CommandLine.arguments
        guard arguments.count >= 2 else { exit(2) }
        switch arguments[1] {
        case "ops":
            emit(Ops.names)
        case "request":
            // request <op> <args.json> [id]
            let line = try opCodec(arguments[2]).requestLine(readFile(arguments[3]), arguments.count > 4 ? arguments[4] : "probe-1")
            FileHandle.standardOutput.write(line)
        case "roundtrip":
            // roundtrip <op> <result.json>: decode the result as the op's model, encode it again
            FileHandle.standardOutput.write(try opCodec(arguments[2]).roundTrip(readFile(arguments[3])))
        case "timeout":
            emit(try opCodec(arguments[2]).timeout(readFile(arguments[3])))
        case "response":
            // response <op> <line> <id>
            do {
                let data = try opCodec(arguments[2]).decodeResponse(readFile(arguments[3]), arguments[4])
                emit(["ok": try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])])
            } catch {
                emit(["error": describe(error)])
            }
        case "call":
            // call <socket> <op> <args.json>
            let client = DaemonClient(transport: UnixSocketTransport(path: arguments[2]))
            let started = Date()
            do {
                let data = try opCodec(arguments[3]).call(client, readFile(arguments[4]))
                emit(["ok": try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]),
                      "elapsed": Date().timeIntervalSince(started)])
            } catch {
                emit(["error": describe(error), "elapsed": Date().timeIntervalSince(started)])
            }
        case "endpoint":
            // endpoint <home> <release|development> [SUBFLEET_HOME]
            let environment = arguments.count > 4 ? ["SUBFLEET_HOME": arguments[4]] : [:]
            let flavor = BuildFlavor(rawValue: arguments[3]) ?? .release
            switch resolveDaemonEndpoint(environment: environment, home: URL(fileURLWithPath: arguments[2]), flavor: flavor) {
            case .ready(let endpoint):
                emit(["ready": endpoint.root.path, "socket": endpoint.socketURL.path, "status": endpoint.statusURL.path])
            case .refused(let root, let reason):
                emit(["refused": reason, "root": root.path])
            }
        case "availability":
            // availability <capabilities.json>: how the app judges a daemon
            let capabilities = try JSONDecoder().decode(Capabilities.self, from: readFile(arguments[2]))
            switch DaemonAvailability.judge(capabilities) {
            case .ready: emit(["ready": true])
            case .incompatible(let reason): emit(["incompatible": reason])
            default: emit(["other": true])
            }
        case "markdown":
            emit(project(Markdown.parse(String(decoding: readFile(arguments[2]), as: UTF8.self))))
        case "attributed":
            let text = String(decoding: readFile(arguments[2]), as: UTF8.self)
            let attributed = Markdown.attributed(Markdown.parseInlines(text))
            var runs: [[String: Any]] = []
            for run in attributed.runs {
                var intents: [String] = []
                if let intent = run.inlinePresentationIntent {
                    if intent.contains(.emphasized) { intents.append("emphasized") }
                    if intent.contains(.stronglyEmphasized) { intents.append("strong") }
                    if intent.contains(.code) { intents.append("code") }
                    if intent.contains(.strikethrough) { intents.append("strikethrough") }
                }
                runs.append(["text": String(attributed[run.range].characters), "intents": intents,
                             "link": run.link?.absoluteString as Any? ?? NSNull()])
            }
            emit(runs)
        case "bounds":
            let text = String(decoding: readFile(arguments[2]), as: UTF8.self)
            let code = MarkdownBounds.code(text, maxLines: Int(arguments[3]) ?? 40)
            emit(["shown_lines": code.shown.split(separator: "\n", omittingEmptySubsequences: false).count,
                  "hidden_lines": code.hiddenLines])
        case "fold":
            emit(try runFold(readFile(arguments[2])))
        default:
            if let handled = try extraCommand(arguments) {
                emit(handled)
            } else {
                FileHandle.standardError.write("unknown command \(arguments[1])\n".data(using: .utf8)!)
                exit(2)
            }
        }
    }
}
