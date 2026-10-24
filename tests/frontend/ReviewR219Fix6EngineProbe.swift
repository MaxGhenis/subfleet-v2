import Foundation

/// Real service fixtures, with an older daemon's unsupported status operation.
final class Fix6Daemon: DaemonCalling {
    let input: JSONValue
    var calls: [[String: Any]] = []
    init(_ input: JSONValue) { self.input = input }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        let encoded = try JSONEncoder().encode(args)
        let value = try JSONValue.parse(encoded)
        calls.append(["op": op.name, "args": try JSONSerialization.jsonObject(with: encoded)])
        let result: JSONValue
        switch op.name {
        case "capabilities": result = input["capabilities"]!
        case "conversation.open": result = input["open"]!
        case "conversation.status":
            guard let status = input["status"] else {
                throw DaemonClientError.unavailable("unknown operation: conversation.status")
            }
            result = status
        case "message.status":
            let ids = value["message_ids"]?.array?.compactMap(\.string) ?? []
            let receipts = ids.compactMap { input["receipts"]?[$0] }
            return try JSONValue.from(["messages": receipts]).decode(R.self)
        case "conversation.watch": result = input["watch"]!
        default: throw DaemonClientError.unavailable("unexpected operation: \(op.name)")
        }
        return try result.decode(R.self)
    }
}

@main struct ReviewR219Fix6EngineProbe {
    static func main() throws {
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        let client = Fix6Daemon(input)
        let engine = ConversationEngine(client: client, outbox: try Outbox(url: nil))
        var state = ConversationStoreState()
        state.availability = engine.checkAvailability()
        let cid = input["open"]!["conversation"]!["conversation_id"]!.string!
        // The launch baseline precedes focusing an existing conversation.
        if let baseline = input["baseline"] { state.apply(watch: try baseline.decode(WatchPage.self)) }
        state.apply(open: try engine.open(.conversation(cid)))
        state.focus(cid)
        let openingIDs = state.layout(conversationID: cid)!.tray.map(\.id)
        var attempts = 0, delivered = 0
        var errors: [String] = []
        let loop = WatchLoop(engine: engine,
            cursor: { attempts < 2 ? state.watchCursor : nil },
            deliver: { update in
                attempts += 1
                delivered += 1
                state.apply(update: update)
            },
            lost: { error in attempts += 1; errors.append(String(describing: error)) },
            regained: {}, pause: { _ in },
            focusedConversationID: { cid }, knownMessageIDs: { state.knownMessageIDs(in: $0) })
        loop.run()
        let output: [String: Any] = ["ready": state.availability.isReady, "opening_tray": openingIDs,
            "tray": state.layout(conversationID: cid)!.tray.map(\.id), "delivered": delivered,
            "cursor": state.watchCursor, "errors": errors, "calls": client.calls]
        print(String(decoding: try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), as: UTF8.self))
    }
}
