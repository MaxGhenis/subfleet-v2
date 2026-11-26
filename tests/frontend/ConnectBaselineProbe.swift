// Runs a real UIModel.connect() against a scripted daemon.
//
// `probe <input.json>` takes an object with these keys:
//   root: directory for the app's paths
//   focused: initially focused conversation ID, or null
//   on_screen: initial value for UIModel.onScreenOverride
//   answers: fixed answers keyed by operation name (capabilities, models.list,
//     conversation.list, conversation.status, message.status and conversation.open)
//   drain: lists of conversation.watch answers keyed by the requested `after`,
//     for requests with `wait_s` 0. The last answer for each cursor repeats.
//     An answer of {"error": "<text>"} makes that read throw.
//   during_first_drain: optional {"search": "<text>", "grouping": "recency" | "workspace"}
//     for the sidebar edits to make on the main actor before the first drain read returns
//   feed: optional page for the feed loop's first long poll, held until connect returns
//   flip_on_screen: value for UIModel.onScreenOverride just before releasing that page
//   after_connect: optional watch page to replay into a copy of the connected state
//
// All other long-poll watch and conversation.events calls block until the process exits.
// The output records the sidebar search and grouping, watch baseline and cursor,
// drain cursors, number of feed reads, problem, and operations in call order.
// `backlog` lists [kind, conversation_id] for the replay's notifications, or
// is null if no replay was requested. `posted` lists the same pairs for intents
// handed to the notification sender by the real model.
import AppKit

/// Keeps the model's preferences in memory.
private final class ConnectBaselineDefaults: UserDefaults, @unchecked Sendable {
    private var values: [String: Any] = [:]
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values.removeValue(forKey: key) }
}

private final class ConnectBaselineClient: DaemonCalling, @unchecked Sendable {
    private let answers: [String: JSONValue]
    private let duringFirstDrain: JSONValue?
    private let feed: JSONValue?
    private let lock = NSLock()
    private let feedReady = DispatchSemaphore(value: 0)
    private let stalled = DispatchSemaphore(value: 0)
    private var drain: [String: [JSONValue]]
    private var ops: [String] = []
    private var drainReads: [Int] = []
    private var feedReads = 0
    @MainActor weak var model: UIModel?

    init(_ input: JSONValue) {
        answers = input["answers"]?.object ?? [:]
        duringFirstDrain = input["during_first_drain"]
        drain = input["drain"]?.object?.compactMapValues(\.array) ?? [:]
        feed = input["feed"].flatMap { $0.isNull ? nil : $0 }
    }

    var reads: (ops: [String], drain: [Int], feed: Int) {
        lock.withLock { (ops, drainReads, feedReads) }
    }

    var hasFeed: Bool { feed != nil }

    func releaseFeed() { feedReady.signal() }

    func call<A: Encodable, R: Decodable>(_ op: DaemonOperation<A, R>, _ args: A) throws -> R {
        lock.withLock { ops.append(op.name) }
        switch op.name {
        case "conversation.watch":
            let request = try JSONValue.from(args)
            guard let after = request["after"]?.int, let wait = request["wait_s"]?.double else {
                throw DaemonClientError.unavailable("connect baseline probe: malformed watch request")
            }
            if wait > 0 {
                let first = lock.withLock {
                    feedReads += 1
                    return feedReads == 1
                }
                if first, let feed {
                    feedReady.wait()
                    return try feed.decode(R.self)
                }
                stall()
            }
            if wait == 0 { return try drainAnswer(after: after).decode(R.self) }
        case "conversation.events":
            stall()
        case "capabilities", "models.list", "conversation.list", "conversation.status", "message.status", "conversation.open":
            if let answer = answers[op.name] { return try answer.decode(R.self) }
        default:
            break
        }
        throw DaemonClientError.unavailable("connect baseline probe: \(op.name)")
    }

    private func drainAnswer(after: Int) throws -> JSONValue {
        let (first, answer) = lock.withLock { () -> (Bool, JSONValue?) in
            let first = drainReads.isEmpty
            drainReads.append(after)
            let key = String(after)
            guard let answer = drain[key]?.first else { return (first, nil) }
            if (drain[key]?.count ?? 0) > 1 { drain[key]?.removeFirst() }
            return (first, answer)
        }
        if first, let search = duringFirstDrain?["search"]?.string,
           let grouping = duringFirstDrain?["grouping"]?.string.flatMap(SidebarGrouping.init(rawValue:)) {
            DispatchQueue.main.sync {
                model?.setSearch(search)
                model?.setGrouping(grouping)
            }
        }
        guard let answer else {
            throw DaemonClientError.unavailable("connect baseline probe: no drain page after \(after)")
        }
        if let error = answer["error"]?.string { throw DaemonClientError.unavailable(error) }
        return answer
    }

    private func stall() -> Never {
        stalled.wait()
        fatalError("connect baseline probe: the stall semaphore was signalled")
    }
}

@main struct ConnectBaselineProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let input = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
        var initial = ConversationStoreState()
        initial.focus(input["focused"]?.string)
        let client = ConnectBaselineClient(input)
        let paths = AppPaths.rooted(at: URL(fileURLWithPath: input["root"]!.string!))
        let model = UIModel(paths: paths, client: client, defaults: ConnectBaselineDefaults(), state: initial)
        client.model = model
        model.onScreenOverride = input["on_screen"]?.bool
        await model.connect()

        if client.hasFeed {
            model.onScreenOverride = input["flip_on_screen"]?.bool
            client.releaseFeed()
            // The second long poll starts after the first page has folded on the main actor.
            let clock = ContinuousClock()
            let deadline = clock.now + .seconds(10)
            while client.reads.feed < 2 && clock.now < deadline {
                try await Task.sleep(until: min(clock.now + .milliseconds(10), deadline), clock: clock)
            }
        }

        let state = model.state
        var backlog: Any = NSNull()
        if let replay = input["after_connect"], !replay.isNull {
            var copy = state
            let page = try replay.decode(WatchPage.self)
            copy.apply(update: WatchUpdate(page: page, conversation: nil, receipts: []))
            backlog = copy.drainNotifications().map { [$0.kind.rawValue, $0.conversationID] }
        }
        let reads = client.reads
        let result: [String: Any] = [
            "search": state.searchQuery,
            "grouping": state.grouping.rawValue,
            "watch_baselined": state.watchBaselined,
            "watch_cursor": state.watchCursor,
            "drain_reads": reads.drain,
            "feed_reads": reads.feed,
            "backlog": backlog,
            "posted": model.probePosted.map { [$0.kind.rawValue, $0.conversationID] },
            "problem": model.problem as Any? ?? NSNull(),
            "ops": reads.ops,
        ]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
