// Subfleet: the observable model the windows bind to.
//
// It owns one `ConversationEngine` and runs its blocking calls off the main
// thread: outbox work (send, pump, stop, withdraw) on one serial queue, as the
// engine requires, and the two long polls (the global `conversation.watch`
// feed and the focused conversation's `conversation.events`) on their own
// threads. Every answer is folded into `ConversationStoreState` on the main
// actor, so the views only ever read settled state (design D-24).

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI
import UserNotifications

@MainActor
final class UIModel: ObservableObject {
    @Published private(set) var state = ConversationStoreState()
    /// The last thing that went wrong, shown in the window's status line.
    @Published var problem: String?
    /// A listed session that cannot continue here, shown in place of a conversation.
    @Published var lockedEntry: SidebarEntry?
    /// Each conversation's dispatched runs (its sub-agents), newest first.
    @Published var runs: [String: [RunSummary]] = [:]
    @Published var busy = false
    @Published var newDraft = NewConversationDraft() {
        didSet { saveNewDraft() }
    }
    /// Sends that should open their conversation when the outbox receives it.
    private var draftDestinations: [String: Int] = [:]
    /// The Changes pane's subject while it is open (C-26.14).
    @Published var changesScope: ChangesScope?
    /// Each subject's last answer.
    @Published var changes: [ChangesScope: ChangesLoad] = [:]
    /// A finished turn's changed-file counts, for its status line.
    @Published var turnChanges: [String: DiffStats] = [:]
    private var turnChangesAsked: Set<String> = []

    let paths: AppPaths
    let drafts: DraftStore
    private(set) var engine: ConversationEngine?
    private let outboxQueue = DispatchQueue(label: "org.maxghenis.subfleet.outbox")
    /// Reads that run git on the daemon (the diffs) wait here, never behind a send.
    private let readQueue = DispatchQueue(label: "org.maxghenis.subfleet.reads", attributes: .concurrent)
    private var started = false
    private var eventsGeneration = 0
    private var pumpTimer: Timer?
    private var listTimer: Timer?
    /// Moves on with every availability check, so only the newest one's answer is
    /// kept: a slow check begun when the feed failed never overwrites a later one.
    private var availabilityChecks = 0
    /// What `lostDaemon` put in the status line, cleared when the daemon is back.
    private var lostProblem: String?

    init() {
        paths = AppPaths.standard()
        drafts = DraftStore(directory: paths.draftsDirectory)
        let draftURL = paths.support.appendingPathComponent("new-conversation-draft.json")
        if let data = try? Data(contentsOf: draftURL),
           var saved = try? JSONDecoder().decode(NewConversationDraft.self, from: data) {
            saved.restore()
            newDraft = saved
        } else {
            let defaults = UserDefaults.standard
            newDraft.workspace = defaults.string(forKey: "lastWorkspace")
            let provider = defaults.string(forKey: "providerChoice") ?? "claude"
            newDraft.provider = ["claude", "codex"].contains(provider) ? provider : "claude"
            newDraft.settings.model = defaults.string(forKey: "lastModel.\(newDraft.provider)") ?? ""
            newDraft.settings.permission = defaults.string(forKey: "lastPermission") ?? PermissionPolicy.ask.rawValue
        }
        for directory in [paths.support, paths.caches, paths.draftsDirectory, paths.attachmentsDirectory] {
            try? ensurePrivateDirectory(directory)
        }
        do {
            let client = try DaemonClient.forCurrentEndpoint()
            engine = ConversationEngine(client: client, outbox: try Outbox(url: paths.outboxURL))
        } catch DaemonClientError.endpointRefused(let reason) {
            state.availability = .refused(reason)
        } catch {
            state.availability = .down("The app could not open its outbox: \(error)")
        }
    }

    // MARK: Running

    func start() {
        guard !started, engine != nil else { return }
        started = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        Task { await connect() }
        pumpTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.pump() }
        }
        listTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refreshList() }
        }
    }

    /// `capabilities`, then models, the list and the watch baseline; then the feed.
    func connect() async {
        guard let engine else { return }
        // A check another began meanwhile answers for itself; this one asks again.
        let availability = await checkAvailability() ?? .unknown
        guard availability.isReady else {
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            if !Task.isCancelled { await connect() }
            return
        }
        for provider in ["claude", "codex"] {
            if let models = try? await onOutbox({ try engine.models(provider: provider) }) {
                state.apply(models: models, provider: provider)
            }
        }
        await refreshList()
        var baseline = state
        _ = try? await onOutbox { try engine.drainWatch(&baseline) }
        // A navigation while the baseline was loading keeps its focus.
        baseline.focus(state.focusedConversationID)
        state = baseline
        state.watchBaselined = true
        startWatchLoop()
        if let focused = state.focusedConversationID {
            startEventsLoop(focused)
        } else if !newDraft.isPresented,
                  let requested = ProcessInfo.processInfo.environment["SUBFLEET_OPEN_CONVERSATION"], !requested.isEmpty {
            // A launch that names a conversation opens it (notifications and links reuse this).
            focus(requested)
        }
    }

    func refreshList() async {
        guard let engine, state.availability.isReady else { return }
        do {
            let list = try await onOutbox { try engine.list(query: nil, provider: nil) }
            state.apply(list: list)
            updateBadge()
        } catch {
            report(error)
        }
    }

    private func startWatchLoop() {
        guard let engine else { return }
        Thread.detachNewThread { [weak self] in
            WatchLoop(engine: engine,
                      cursor: { DispatchQueue.main.sync(execute: { self?.state.watchCursor }) },
                      deliver: { page in DispatchQueue.main.async { self?.fold(watch: page) } },
                      lost: { error in DispatchQueue.main.async { self?.lostDaemon(error) } },
                      regained: { DispatchQueue.main.async { self?.regainedDaemon() } }).run()
        }
    }

    private func fold(watch page: WatchPage) {
        let known = Set(state.conversations.map(\.conversation_id))
        state.apply(watch: page)
        for intent in state.drainNotifications() { post(intent) }
        updateBadge()
        if page.changes.contains(where: { !known.contains($0.conversation_id) }) {
            Task { await refreshList() }
        }
    }

    private func lostDaemon(_ error: Error) {
        Task {
            guard let availability = await checkAvailability(), !availability.isReady else { return }
            problem = describe(error)
            lostProblem = problem
        }
    }

    /// The feed answers again after failing: check again, and once the daemon is
    /// ready resume what waits on it (C-29.2).
    private func regainedDaemon() {
        Task {
            let wasReady = state.availability.isReady
            guard let availability = await checkAvailability(), availability.isReady, !wasReady else { return }
            if problem != nil && problem == lostProblem { problem = nil }
            lostProblem = nil
            await refreshList()
            pump()
        }
    }

    /// `capabilities` now; the answer becomes `state.availability` unless a later
    /// check began meanwhile (then nil).
    private func checkAvailability() async -> DaemonAvailability? {
        guard let engine else { return nil }
        availabilityChecks += 1
        let check = availabilityChecks
        let availability = await onOutbox { engine.checkAvailability() }
        guard check == availabilityChecks else { return nil }
        state.availability = availability
        return availability
    }

    /// One events loop, for the focused conversation only (C-29.9); a new focus
    /// ends the old loop at its next answer.
    private func startEventsLoop(_ conversationID: String) {
        guard let engine else { return }
        eventsGeneration += 1
        let generation = eventsGeneration
        Thread.detachNewThread { [weak self] in
            var failures = 0
            var catchingUp = true
            while true {
                let cursor: Int? = DispatchQueue.main.sync {
                    guard let self, self.eventsGeneration == generation else { return nil }
                    return self.state.timelines[conversationID]?.cursor ?? 0
                }
                guard let cursor else { return }
                do {
                    let page = try engine.events(conversationID: conversationID, after: cursor, wait: catchingUp ? 0 : nil)
                    failures = 0
                    let result: Timeline.PageResult? = DispatchQueue.main.sync {
                        guard let self, self.eventsGeneration == generation else { return nil }
                        return self.state.apply(events: page, conversationID: conversationID)
                    }
                    switch result {
                    case nil, .superseded?: return
                    case .reset?: catchingUp = true
                    case .applied(let count)?: catchingUp = count > 0 && catchingUp
                    }
                } catch {
                    failures += 1
                    Thread.sleep(forTimeInterval: min(30, Double(failures) * 2))
                }
            }
        }
    }

    // MARK: Conversations

    /// Moves on with every navigation that opens or leaves something, so an open
    /// that answers after the person went elsewhere changes nothing.
    private var navigation = 0

    func select(_ entry: SidebarEntry?) {
        guard let entry else { return }
        newDraft.leave()
        if !entry.continuable {
            // Opening one only fails (not-continuable): say what it is instead.
            navigation += 1
            showLocked(entry)
            return
        }
        lockedEntry = nil
        switch entry.target {
        case .conversation(let id): focus(id)
        case .native:
            navigation += 1
            let token = navigation
            Task { await open(entry.target, token: token) }
        }
    }

    /// A locked page replaces the focused conversation, so that conversation's
    /// completions and approvals notify again, and its events loop ends.
    private func showLocked(_ entry: SidebarEntry) {
        lockedEntry = entry
        state.focus(nil)
        eventsGeneration += 1
    }

    func focus(_ conversationID: String) {
        newDraft.leave()
        lockedEntry = nil
        guard state.focusedConversationID != conversationID else { return }
        state.focus(conversationID)
        navigation += 1
        let token = navigation
        Task { await open(.conversation(conversationID), token: token) }
    }

    func open(_ target: SidebarEntry.Target, token: Int? = nil) async {
        guard let engine else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await onOutbox { try engine.open(target) }
            // The daemon's state is kept either way; the screen moves only if the
            // person has not gone elsewhere since asking.
            state.apply(open: result)
            if let token, token != navigation { return }
            lockedEntry = nil
            state.focus(result.conversation.conversation_id)
            startEventsLoop(result.conversation.conversation_id)
            await loadHistory(result.conversation.conversation_id)
        } catch {
            if let token, token != navigation { return }
            // The catalog said continuable and the daemon, looking now, refuses
            // (its directory is gone, it became a lane run): the same page.
            if case .native = target, let refusal = (error as? DaemonClientError)?.daemonError,
               ["not-continuable", "unknown-session"].contains(refusal.reason ?? ""),
               var entry = state.sidebarEntries().first(where: { $0.target == target }) {
                entry.continuable = false
                entry.continueBlocker = refusal.detail
                showLocked(entry)
                return
            }
            report(error)
        }
    }

    /// Lanes ready per provider, read from `status.json` now; empty when the
    /// snapshot is missing or old.
    func providerCapacity() -> [String: Int] {
        let environment = ProcessInfo.processInfo.environment
        guard let url = resolveDaemonEndpoint(environment: environment, home: FileManager.default.homeDirectoryForCurrentUser,
                                              flavor: .current).endpoint?.statusURL,
              let data = try? Data(contentsOf: url),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data),
              !snapshot.isStale() else { return [:] }
        return dispatchableLanes(snapshot)
    }

    /// Workspaces of recent conversations and sessions, newest first, each once.
    func recentWorkspaces(limit: Int = 12) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for entry in state.sidebarEntries() {
            guard let path = entry.workspace, !path.isEmpty, entry.continuable, seen.insert(path).inserted else { continue }
            out.append(path)
            if out.count == limit { break }
        }
        return out
    }

    /// Reads a conversation's runs; the conversation view calls it on a timer
    /// while it is on screen. An older daemon without the op shows none.
    func refreshRuns(_ conversationID: String) async {
        guard let engine else { return }
        if let found = try? await onOutbox({ try engine.runs(conversationID: conversationID) }), runs[conversationID] != found {
            runs[conversationID] = found
        }
    }

    // MARK: Changes

    /// Whether the daemon serves `turn.diff` and `conversation.diff` (C-25.1).
    var canShowChanges: Bool { state.availability.capabilities?.has(diffCapability) == true }

    /// Opens the Changes pane on a subject, or closes it when it already shows it.
    func showChanges(_ scope: ChangesScope) {
        if changesScope == scope {
            changesScope = nil
            return
        }
        changesScope = scope
        Task { await loadChanges(scope) }
    }

    func loadChanges(_ scope: ChangesScope) async {
        guard let engine else { return }
        if case .loaded = changes[scope] {} else { changes[scope] = .loading }
        do {
            let loaded = try await onReads { () throws -> ChangesLoad in
                let result: DiffResult
                switch scope {
                case .conversation(let id): result = try engine.conversationDiff(conversationID: id)
                case .turn(_, let messageID): result = try engine.turnDiff(messageID: messageID)
                }
                return .loaded(result, UnifiedDiff.parse(result.diff))
            }
            changes[scope] = loaded
            if case .turn(_, let messageID) = scope, case .loaded(let result, _) = loaded { noteTurn(messageID, result) }
        } catch {
            changes[scope] = .failed(describe(error))
        }
    }

    /// A finished turn's counts, asked once per turn. The end snapshot is
    /// recorded at finalization, a moment after the turn shows finished, so an
    /// answer still comparing with the working tree is asked again shortly.
    func loadTurnChanges(_ messageID: String) async {
        guard let engine, canShowChanges, turnChanges[messageID] == nil,
              turnChangesAsked.insert(messageID).inserted else { return }
        for attempt in 1...6 {
            guard let result = try? await onReads({ try engine.turnDiff(messageID: messageID) }) else { break }
            if !result.isLive {
                noteTurn(messageID, result)
                return
            }
            try? await Task.sleep(nanoseconds: UInt64(attempt) * 1_000_000_000)
        }
        turnChangesAsked.remove(messageID)
    }

    private func noteTurn(_ messageID: String, _ result: DiffResult) {
        if result.available && !result.isLive && turnChanges[messageID] != result.stats { turnChanges[messageID] = result.stats }
    }

    /// Pages that add nothing are followed at once, up to this many per "Load
    /// earlier", so it shows something or reaches the start. Opening a
    /// conversation reads one page and follows none (review of 5aa2718).
    static let historyPagesFollowed = 16

    func loadHistory(_ conversationID: String, follow: Bool = false) async {
        guard let engine else { return }
        await loadHistoryPages(
            pages: Self.historyPagesFollowed, follow: follow,
            timeline: { await self.timeline(conversationID) },
            fetch: { before in
                try await self.onOutbox { try engine.history(conversationID: conversationID, before: before) }
            },
            apply: { page in await self.applyHistory(page, conversationID) })
    }

    private func timeline(_ conversationID: String) -> Timeline? { state.timelines[conversationID] }

    private func applyHistory(_ page: HistoryPage, _ conversationID: String) {
        state.apply(history: page, conversationID: conversationID)
    }

    func openNewDraft() {
        navigation += 1
        lockedEntry = nil
        state.focus(nil)
        eventsGeneration += 1
        reconcileNewDraft()
        newDraft.open()
    }

    func reconcileNewDraft() {
        newDraft.reconcile(models: state.models[newDraft.provider] ?? [], capabilities: state.availability.capabilities)
    }

    private func saveNewDraft() {
        guard let data = try? JSONEncoder().encode(newDraft) else { return }
        try? atomicWrite(data, to: paths.support.appendingPathComponent("new-conversation-draft.json"))
    }

    func sendNewDraft(stayHere: Bool) {
        guard let engine, newDraft.canSend else { return }
        let draft = newDraft
        let token = navigation
        let messageID = Outbox.newMessageID()
        if !stayHere { draftDestinations[messageID] = token }
        newDraft.isSubmitting = true
        Task {
            do {
                try await onOutbox {
                    let key = try engine.createConversation(provider: draft.provider, workspace: draft.workspace ?? "",
                                                            settings: draft.settings, confirmWiden: draft.confirmWiden)
                    _ = try engine.send(conversation: key, text: draft.text, staged: draft.attachments,
                                        settings: draft.settings, messageID: messageID)
                }
                newDraft.journaled()
                let defaults = UserDefaults.standard
                defaults.set(draft.workspace, forKey: "lastWorkspace")
                defaults.set(draft.provider, forKey: "providerChoice")
                defaults.set(draft.settings.model, forKey: "lastModel.\(draft.provider)")
                defaults.set(draft.settings.permission, forKey: "lastPermission")
                pump()
            } catch {
                draftDestinations.removeValue(forKey: messageID)
                newDraft.isSubmitting = false
                report(error)
            }
        }
    }

    /// Journal a message and send it now; the optimistic row appears at once.
    func send(conversationID: String, text: String, staged: [StagedAttachment], settings: ConversationSettings) {
        guard let engine else { return }
        let messageID = Outbox.newMessageID()
        state.addLocalMessage(conversationID: conversationID, messageID: messageID, text: text,
                              attachments: staged.map(\.sha256), settings: settings)
        Task {
            do {
                _ = try await onOutbox {
                    try engine.send(conversation: conversationID, text: text, staged: staged, settings: settings,
                                    messageID: messageID)
                }
                pump()
            } catch {
                report(error)
            }
        }
    }

    func pump() {
        guard let engine, state.availability.isReady else { return }
        Task {
            let (report, texts) = await onOutbox { () -> (OutboxSender.Report, [String: String]) in
                let report = engine.pump()
                var texts: [String: String] = [:]
                for receipt in report.receipts {
                    if let text = engine.outbox.text(of: receipt.message_id) { texts[receipt.message_id] = text }
                }
                return (report, texts)
            }
            guard !report.receipts.isEmpty || !report.conversations.isEmpty || !report.failed.isEmpty else { return }
            for receipt in report.receipts {
                state.apply(receipt: receipt)
                if let cid = receipt.conversation_id, let text = texts[receipt.message_id] {
                    state.setPersonText(text, conversationID: cid, messageID: receipt.message_id)
                }
            }
            for conversation in report.conversations {
                state.upsert(conversation)
            }
            for receipt in report.receipts {
                if let token = draftDestinations.removeValue(forKey: receipt.message_id),
                   token == navigation, let id = receipt.conversation_id {
                    focus(id)
                }
            }
            if !report.failed.isEmpty { problem = "\(report.failed.count) message(s) could not be sent; see the conversation" }
        }
    }

    func stop(_ action: StopAction) {
        guard let engine else { return }
        Task {
            do {
                if let receipt = try await onOutbox({ try engine.stop(action) }) { state.apply(receipt: receipt) }
            } catch {
                report(error)
            }
        }
    }

    func perform(_ choice: BlockedChoice, conversationID: String) {
        guard let engine else { return }
        Task {
            do {
                let (conversation, receipt) = try await onOutbox { try engine.perform(choice.action, conversationID: conversationID) }
                if let conversation { state.upsert(conversation) }
                if let receipt { state.apply(receipt: receipt) }
            } catch {
                report(error)
            }
        }
    }

    func updateSettings(_ conversation: Conversation, to settings: ConversationSettings, confirmedWiden: Bool) async -> Bool {
        guard let engine else { return false }
        do {
            let updated = try await onOutbox { try engine.updateSettings(conversation, to: settings, confirmedWiden: confirmedWiden) }
            state.upsert(updated)
            return true
        } catch {
            report(error)
            return false
        }
    }

    func renameConversation(_ conversationID: String, title: String) {
        guard let engine else { return }
        Task {
            do {
                let updated = try await onOutbox { try engine.renameConversation(conversationID: conversationID, title: title) }
                state.upsert(updated)
            } catch { report(error) }
        }
    }

    // MARK: Approvals

    func approvalDetail(_ approvalID: String, reveal: Bool) async -> ApprovalDetail? {
        guard let engine else { return nil }
        do {
            return try await onOutbox { try engine.approvalDetail(approvalID, reveal: reveal) }
        } catch {
            report(error)
            return nil
        }
    }

    func respond(_ detail: ApprovalDetail, decision: String, answers: [String: String]?, message: String?,
                 reviewedMasked: Bool) async -> Bool {
        guard let engine else { return false }
        do {
            _ = try await onOutbox {
                try engine.respond(to: detail, decision: decision, answers: answers, message: message,
                                   reviewedMasked: reviewedMasked)
            }
            let approvals = try? await onOutbox { try engine.approvals(conversationID: detail.approval.conversation_id) }
            if let approvals { state.apply(approvals: approvals, conversationID: detail.approval.conversation_id) }
            updateBadge()
            return true
        } catch {
            report(error)
            return false
        }
    }

    /// The approval id for a card the events made before `approval.list` was read.
    func approvalID(for card: ApprovalCard, conversationID: String) async -> String? {
        if let id = card.approvalID { return id }
        guard let engine, let approvals = try? await onOutbox({ try engine.approvals(conversationID: conversationID) }) else {
            return nil
        }
        state.apply(approvals: approvals, conversationID: conversationID)
        return state.timelines[conversationID]?.turns.values.flatMap(\.pendingApprovals)
            .first { $0.requestID == card.requestID }?.approvalID
            ?? approvals.first { $0.state == "pending" && $0.kind == card.kind }?.approval_id
    }

    // MARK: Sidebar settings

    func setSearch(_ text: String) { state.searchQuery = text }
    func setProviderFilter(_ provider: String?) { state.providerFilter = provider }
    func setGrouping(_ grouping: SidebarGrouping) { state.grouping = grouping }

    // MARK: Attachments

    func stage(_ data: Data) -> StagedAttachment? {
        do {
            return try AttachmentStager.stage(data, in: paths.attachmentsDirectory)
        } catch {
            report(error)
            return nil
        }
    }

    // MARK: Helpers

    private func onOutbox<T>(_ work: @escaping () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            outboxQueue.async {
                do { continuation.resume(returning: try work()) } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func onReads<T>(_ work: @escaping () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { continuation in
            readQueue.async {
                do { continuation.resume(returning: try work()) } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func onOutbox<T>(_ work: @escaping () -> T) async -> T {
        await withCheckedContinuation { continuation in
            outboxQueue.async { continuation.resume(returning: work()) }
        }
    }

    func report(_ error: Error) {
        problem = describe(error)
    }

    private func describe(_ error: Error) -> String {
        if let error = error as? DaemonClientError {
            if case .daemon(let refusal) = error {
                return refusal.message + (refusal.fix.map { " (\($0))" } ?? "")
            }
            return error.summary
        }
        if let error = error as? ConversationEngineError {
            switch error {
            case .maskedValuesNeedReview: return "Reveal or confirm the masked values before allowing."
            case .widenNeedsConfirmation(let from, let to): return "Moving from \(from) to \(to) needs your confirmation."
            case .notOffered(let decision): return "\(decision) is not offered for this request."
            }
        }
        return "\(error)"
    }

    private func updateBadge() {
        let count = state.pendingApprovalCount
        NSApp?.dockTile.badgeLabel = count > 0 ? String(count) : nil
    }

    private func post(_ intent: NotificationIntent) {
        let content = UNMutableNotificationContent()
        content.title = intent.title
        content.body = intent.body
        content.userInfo = ["conversation_id": intent.conversationID]
        let request = UNNotificationRequest(identifier: intent.id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
#endif
