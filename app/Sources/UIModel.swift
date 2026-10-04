// Subfleet: the observable model the windows bind to.
//
// It owns one `ConversationEngine` and runs its blocking calls off the main
// thread: outbox work (send, pump, stop, withdraw) on one serial queue, as the
// engine requires, and the two long polls (the global `conversation.watch`
// feed and the focused conversation's `conversation.events`) on their own
// threads. Every answer is folded into `ConversationStoreState` on the main
// actor, so the views only ever read settled state (design D-24).

#if !SUBFLEET_MODEL_TEST || SUBFLEET_UI_MODEL_TEST
import AppKit
import SwiftUI
import UserNotifications

/// One request to bring a conversation's oldest waiting card into view.
struct ApprovalReveal: Equatable {
    let conversationID: String
    let token: Int
}

@MainActor
final class UIModel: ObservableObject {
    @Published private(set) var state = ConversationStoreState()
    /// The last thing that went wrong, shown in the window's status line.
    @Published var problem: String?
    @Published private(set) var accountSnapshot: Snapshot?
    /// A listed session that cannot continue here, shown in place of a conversation.
    @Published var lockedEntry: SidebarEntry?
    @Published private(set) var failedDrafts: [FailedConversationDraft] = []
    @Published var selectedFailedDraftID: String?
    /// The refused create being edited; retry preserves its queued message ids.
    @Published var failedDraftKey: String?
    /// Each conversation's dispatched runs (its sub-agents), newest first.
    @Published var runs: [String: [RunSummary]] = [:]
    @Published var busy = false
    @Published var newDraft = NewConversationDraft() {
        didSet { saveNewDraft() }
    }
    private var draftNeedsWorkspaceDefault = true
    private var draftWorkspaceChecks = 0
    private var draftCheckInFlight = false
    /// A retry edits its own persisted draft; the ordinary composer stays intact.
    private var composerBeforeRetry: NewConversationDraft?
    private var workspaceDefaultBeforeRetry = false
    private var restoringComposer = false
    private let refusedDraftNotice = "A conversation could not start. Review its saved draft."
    /// Sends that should open their conversation when the outbox receives it.
    private var draftDestinations: [String: Int] = [:]
    /// The Changes pane's subject while it is open (C-26.14).
    @Published var changesScope: ChangesScope?
    /// Each subject's last answer.
    @Published var changes: [ChangesScope: ChangesLoad] = [:]
    /// A finished turn's changed-file counts, for its status line.
    @Published var turnChanges: [String: DiffStats] = [:]
    /// Words Esc took back from the running turn, per conversation, for its composer.
    /// They are in the conversation's draft too (`recalledDraft`), so they outlive this.
    @Published var composerRecall: [String: ComposerRecall] = [:]
    /// Steers the daemon answered `too-late` for, each with what the app saw of it then:
    /// Esc passes over one only while it is still there (`stillTooLate`).
    private var tooLateSteers = TooLateSteers()
    private var turnChangesAsked: Set<String> = []
    /// The person asked to see a conversation's oldest waiting card (the sidebar's
    /// hand badge, the strip's Review); cleared once it is in view.
    @Published private(set) var approvalReveal: ApprovalReveal?
    private var reveals = 0
    /// What the stale-approvals check last read, per conversation (the daemon's
    /// count and the cards shown), so an unchanged mismatch is read once.
    private var staleRead: [String: [Int]] = [:]

    let paths: AppPaths
    let drafts: DraftStore
    private let defaults: UserDefaults
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
    private lazy var notificationDelegate = ApprovalNotificationDelegate { [weak self] action, userInfo in
        self?.handleNotification(action: action, userInfo: userInfo)
    }

    init(paths: AppPaths = .standard(), client: DaemonCalling? = nil, defaults: UserDefaults = .standard,
         state initialState: ConversationStoreState = ConversationStoreState()) {
        self.paths = paths
        self.defaults = defaults
        drafts = DraftStore(directory: paths.draftsDirectory)
        self.state = initialState
        defaults.removeObject(forKey: "lastWorkspace")
        let draftURL = paths.support.appendingPathComponent("new-conversation-draft.json")
        if let data = try? Data(contentsOf: draftURL),
           var saved = try? JSONDecoder().decode(NewConversationDraft.self, from: data) {
            saved.restore()
            newDraft = saved
            draftNeedsWorkspaceDefault = false
        } else {
            // Folder history is only considered after daemon admission; older
            // versions remembered home even when create had refused it.
            let provider = defaults.string(forKey: "providerChoice") ?? "auto"
            newDraft.providerChoice = ["auto", "claude", "codex"].contains(provider) ? provider : "auto"
            newDraft.settings.permission = defaults.string(forKey: "lastPermission") ?? PermissionPolicy.ask.rawValue
        }
        for directory in [paths.support, paths.caches, paths.draftsDirectory, paths.attachmentsDirectory] {
            try? ensurePrivateDirectory(directory)
        }
        do {
            let client = try client ?? DaemonClient.forCurrentEndpoint()
            engine = ConversationEngine(client: client, outbox: try Outbox(url: paths.outboxURL))
            if let id = newDraft.messageID, engine?.outbox.entry(id) != nil {
                // The journal write won the crash race. Do not offer the same
                // words as a new message when the app relaunches.
                newDraft.journaled()
            }
            failedDrafts = engine?.outbox.failedDrafts ?? []
            if !failedDrafts.isEmpty { problem = refusedDraftNotice }
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
        notificationDelegate.register()
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
        refreshAccountUsage()
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
        refreshApprovalsIfStale()
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
        if newDraft.isPresented { validateNewDraftWorkspace() }
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
        selectedFailedDraftID = nil
        endFailedDraftEditing()
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
        selectedFailedDraftID = nil
        endFailedDraftEditing()
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
            let (result, steers) = try await onOutbox { () throws -> (ConversationOpenResult, [OutboxSteer]) in
                let open = try engine.open(target)
                return (open, engine.outbox.steers(in: open.conversation.conversation_id))
            }
            // The daemon's state is kept either way; the screen moves only if the
            // person has not gone elsewhere since asking.
            state.apply(open: result)
            // Steers this app journaled: still on their way, or refused (C-24.9).
            state.apply(steers: steers)
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

    /// A read of the same status projection used by the quota panel; no RPC additions.
    func refreshAccountUsage() {
        let environment = ProcessInfo.processInfo.environment
        guard let url = resolveDaemonEndpoint(environment: environment, home: FileManager.default.homeDirectoryForCurrentUser,
                                              flavor: .current).endpoint?.statusURL,
              let data = try? Data(contentsOf: url),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            accountSnapshot = nil
            return
        }
        accountSnapshot = snapshot
        state.laneLabels = laneLabels(from: snapshot)
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
        selectedFailedDraftID = nil
        endFailedDraftEditing()
        navigation += 1
        lockedEntry = nil
        state.focus(nil)
        eventsGeneration += 1
        reconcileNewDraft()
        newDraft.open()
        validateNewDraftWorkspace(selectDefault: true)
    }

    func reconcileNewDraft() {
        let provider = (newDraft.providerChoice ?? newDraft.provider) == "auto"
            ? autoProvider(providerCapacity()) : (newDraft.providerChoice ?? newDraft.provider)
        if provider != newDraft.provider {
            newDraft.provider = provider
            newDraft.settings.model = ""
            newDraft.settings.effort = nil
            newDraft.confirmWiden = false
            newDraft.invalidateWorkspaceCheck()
        }
        newDraft.reconcile(models: state.models[provider] ?? [], capabilities: state.availability.capabilities,
                           defaultModel: state.modelDefaults[provider],
                           rememberedModel: defaults.string(forKey: "lastModel.\(provider)"))
        if newDraft.isPresented, newDraft.workspaceCheckTransient == true, !draftCheckInFlight {
            validateNewDraftWorkspace()
        }
    }

    func selectNewDraftProvider(_ choice: String) {
        newDraft.providerChoice = choice
        reconcileNewDraft()
        validateNewDraftWorkspace()
    }

    func useNewScratchFolder() {
        draftNeedsWorkspaceDefault = false
        newDraft.workspace = nil
        newDraft.scratchWorkspace = scratchWorkspace(support: paths.support).path
        validateNewDraftWorkspace()
    }

    /// The check's generation and exact picks prevent a slow previous answer
    /// from enabling Start after the person changes folder or permission.
    func validateNewDraftWorkspace(selectDefault: Bool = false) {
        guard let engine else { return }
        newDraft.invalidateWorkspaceCheck()
        draftWorkspaceChecks += 1
        let generation = draftWorkspaceChecks
        let provider = newDraft.provider
        let permission = newDraft.settings.permission
        let recovery = failedDrafts.first { $0.id == failedDraftKey }?.create
        let selectingDefault = draftNeedsWorkspaceDefault && (selectDefault || newDraft.workspace == nil)
        let candidates = selectingDefault ? recentWorkspaces() : []
        // Unknown readiness is not an old daemon. Keep the draft and explain
        // why Start must wait for a capability handshake and folder validation.
        guard let capabilities = state.availability.capabilities else {
            if newDraft.resolvedWorkspace == nil { newDraft.scratchWorkspace = scratchWorkspace(support: paths.support).path }
            if let selected = newDraft.resolvedWorkspace {
                let reason = state.availability.banner?.detail ?? "Waiting for the daemon to become ready."
                newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: false, reason: "Could not check folder: \(reason)",
                                                                 fix: "Try again when the daemon is available."),
                                             workspace: selected, provider: provider, permission: permission, transient: true)
            }
            draftCheckInFlight = false
            return
        }
        // C-25.1: older daemons decide admission at create. Do not call an op
        // they have not advertised, either here or at Start.
        if !capabilities.has("workspace.check.v1") {
            if selectingDefault { newDraft.workspace = candidates.first; draftNeedsWorkspaceDefault = false }
            if newDraft.resolvedWorkspace == nil { newDraft.scratchWorkspace = scratchWorkspace(support: paths.support).path }
            if let selected = newDraft.resolvedWorkspace {
                newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: true), workspace: selected,
                                             provider: provider, permission: permission)
            }
            draftCheckInFlight = false
            return
        }
        draftCheckInFlight = true
        Task {
            defer { if generation == draftWorkspaceChecks { draftCheckInFlight = false } }
            do {
                if selectingDefault {
                    var accepted: (String, WorkspaceCheckResult)?
                    for path in candidates {
                        let result = try await onReads { try engine.checkWorkspace(path, provider: provider, permission: permission) }
                        guard generation == draftWorkspaceChecks else { return }
                        if result.ok { accepted = (path, result); break }
                    }
                    guard generation == draftWorkspaceChecks else { return }
                    draftNeedsWorkspaceDefault = false
                    if let (path, result) = accepted {
                        newDraft.workspace = path
                        newDraft.applyWorkspaceCheck(result, workspace: path, provider: provider, permission: permission)
                        return
                    }
                    newDraft.workspace = nil
                }
                if newDraft.workspace == nil, newDraft.scratchWorkspace == nil {
                    newDraft.scratchWorkspace = scratchWorkspace(support: paths.support).path
                }
                guard let selected = newDraft.resolvedWorkspace else { return }
                // The scratch folder is not created until Start. Checking its
                // existing parent applies the daemon's folder policy now; Start
                // checks the new directory itself before journaling anything.
                let checkPath = newDraft.workspace ?? paths.support.path
                let result = try await onReads {
                    try engine.checkWorkspace(checkPath, provider: provider, permission: permission,
                                              kind: recovery?.workspace_kind ?? "in-place", allowMain: recovery?.allow_main)
                }
                guard generation == draftWorkspaceChecks else { return }
                newDraft.applyWorkspaceCheck(result, workspace: selected, provider: provider, permission: permission)
                if !result.ok, defaults.string(forKey: "lastWorkspace") == newDraft.workspace {
                    defaults.removeObject(forKey: "lastWorkspace")
                }
            } catch {
                guard generation == draftWorkspaceChecks, let selected = newDraft.resolvedWorkspace else { return }
                newDraft.applyWorkspaceCheck(WorkspaceCheckResult(ok: false, reason: "Could not check folder: \(error)",
                                                                   fix: "Try again when the daemon is available."),
                                             workspace: selected, provider: provider, permission: permission, transient: true)
            }
        }
    }

    private func saveNewDraft() {
        guard !restoringComposer else { return }
        var saved = newDraft
        if saved.workspaceCheck?.ok == false && saved.workspaceCheckTransient != true {
            saved.workspace = nil
            saved.scratchWorkspace = nil
            saved.invalidateWorkspaceCheck()
        }
        guard let data = try? JSONEncoder().encode(saved) else { return }
        let url = failedDraftKey.map(retryDraftURL) ?? paths.support.appendingPathComponent("new-conversation-draft.json")
        try? atomicWrite(data, to: url)
    }

    func sendNewDraft(stayHere: Bool) {
        guard let engine, newDraft.canStart, let workspace = newDraft.resolvedWorkspace else { return }
        guard state.availability.isReady else { validateNewDraftWorkspace(); return }
        let recovering = failedDraftKey
        if recovering == nil {
            if newDraft.requestID == nil { newDraft.requestID = "app-" + UUID().uuidString.lowercased() }
            if newDraft.messageID == nil { newDraft.messageID = Outbox.newMessageID() }
        }
        let draft = newDraft
        let recovery = failedDrafts.first { $0.id == recovering }?.create
        guard recovering == nil || recovery != nil else { return }
        let token = navigation
        let messageID = failedDrafts.first(where: { $0.id == recovering })?.messages.first?.key
            ?? draft.messageID ?? Outbox.newMessageID()
        let checkSupported = state.availability.capabilities.map { $0.has("workspace.check.v1") } ?? true
        if !stayHere { draftDestinations[messageID] = token }
        newDraft.isSubmitting = true
        Task {
            do {
                try await onReads {
                    if draft.workspace == nil { try ensurePrivateDirectory(URL(fileURLWithPath: workspace, isDirectory: true)) }
                    if checkSupported {
                        let check = try engine.checkWorkspace(workspace, provider: draft.provider, permission: draft.settings.permission,
                                                         kind: recovery?.workspace_kind ?? "in-place", allowMain: recovery?.allow_main)
                        guard check.ok else {
                            throw DaemonClientError.daemon(DaemonError(code: 7, message: check.reason ?? "Folder refused", fix: check.fix))
                        }
                    }
                }
                try await onOutbox {
                    if let recovering, var args = recovery {
                        args.provider = draft.provider
                        args.workspace = workspace
                        args.settings = draft.settings
                        args.confirm_widen = draft.confirmWiden
                        try engine.outbox.retryFailedCreate(recovering, args: args, text: draft.text, staged: draft.attachments)
                    } else {
                        let key = try engine.createConversation(provider: draft.provider, workspace: workspace,
                                                                settings: draft.settings, confirmWiden: draft.confirmWiden,
                                                                requestID: draft.requestID!)
                        _ = try engine.send(conversation: key, text: draft.text, staged: draft.attachments,
                                            settings: draft.settings, messageID: messageID)
                    }
                }
                if let recovering {
                    try? FileManager.default.removeItem(at: retryDraftURL(recovering))
                    if failedDraftKey == recovering { endFailedDraftEditing() }
                } else if newDraft.messageID == messageID {
                    newDraft.journaled()
                    if draft.workspace == nil { newDraft.scratchWorkspace = nil; validateNewDraftWorkspace() }
                } else if composerBeforeRetry?.messageID == messageID {
                    composerBeforeRetry?.journaled()
                    if draft.workspace == nil { composerBeforeRetry?.scratchWorkspace = nil }
                }
                defaults.set(draft.providerChoice ?? draft.provider, forKey: "providerChoice")
                defaults.set(draft.settings.model, forKey: "lastModel.\(draft.provider)")
                defaults.set(draft.settings.permission, forKey: "lastPermission")
                pump()
            } catch {
                draftDestinations.removeValue(forKey: messageID)
                if failedDraftKey == recovering { newDraft.isSubmitting = false }
                report(error)
                validateNewDraftWorkspace()
            }
        }
    }

    /// Journal a message and send it now; the optimistic row appears at once.
    /// With `steer`, it is submitted and then steered into the running turn (C-24.9).
    func send(conversationID: String, text: String, staged: [StagedAttachment], settings: ConversationSettings,
              steer: Bool = false) {
        guard let engine else { return }
        let provider = state.conversation(conversationID)?.provider
        let messageID = Outbox.newMessageID()
        // The turn the person steers into now: a steer that reaches the daemon after
        // it ended is refused, never joined to the next turn (C-24.9).
        let into = steer ? state.steerHost(forComposerOf: conversationID)?.messageID : nil
        state.addLocalMessage(conversationID: conversationID, messageID: messageID, text: text,
                              attachments: staged.map(\.sha256), settings: settings, steer: steer)
        Task {
            do {
                _ = try await onOutbox {
                    try engine.send(conversation: conversationID, text: text, staged: staged, settings: settings,
                                    messageID: messageID, steer: steer, into: into)
                }
                if let provider { defaults.set(settings.model, forKey: "lastModel.\(provider)") }
                pump()
            } catch {
                report(error)
            }
        }
    }

    /// Steer a queued message into the running turn (C-24.9): journaled, then sent.
    /// A refusal leaves it queued and its status line says why. The queue tray's
    /// "Send now" (DESIGN.md sections 7 to 9) calls this.
    func steer(messageID: String, conversationID: String) {
        guard let engine else { return }
        // Slash commands and shell input wait for the turn to end (DESIGN.md section 9).
        let text = state.timelines[conversationID]?.turn(messageID)?.personText ?? ""
        guard steerable(text: text) else { return }
        let into = state.timelines[conversationID]?.liveMessageID
        state.requestSteer(conversationID: conversationID, messageID: messageID)
        Task {
            do {
                _ = try await onOutbox {
                    try engine.steer(messageID: messageID, conversationID: conversationID, into: into)
                }
                pump()
            } catch {
                state.noteSteer(conversationID: conversationID, messageID: messageID, refusal: nil)
                report(error)
            }
        }
    }

    /// Esc while a turn runs (DESIGN.md sections 8 and 9): the latest steer the
    /// provider has not read comes back to the composer; with none, the turn stops.
    /// Stop keeps unread steers and queued messages: they run next.
    func escape(conversationID: String, assistant: String) {
        guard let engine, let timeline = state.timelines[conversationID] else { return }
        // A steer that moved on since it was too late (back in the queue, steered
        // into another turn) may be taken back again (C-24.9).
        let unread: String
        switch tooLateSteers.escape(timeline) {
        case .none: return
        case .stop(let action):
            stop(action)
            return
        case .recall(let messageID): unread = messageID
        }
        guard let turn = timeline.turn(unread) else { return }
        let (messageState, text) = (turn.state, turn.personText)
        Task {
            do {
                let outcome = try await onOutbox { try engine.recall(messageID: unread, state: messageState, text: text) }
                switch outcome {
                case .recalled(let words, let staged, let receipt):
                    state.noteSteer(conversationID: conversationID, messageID: unread, refusal: nil)
                    if let receipt { state.apply(receipt: receipt) } else {
                        state.withdrawLocal(conversationID: conversationID, messageID: unread)
                    }
                    // The words go into the draft first: the message is withdrawn, and they
                    // must outlive leaving this conversation or quitting before it is shown.
                    let draft = recalledDraft(drafts.load(conversationID), text: words, staged: staged,
                                              now: ISO8601DateFormatter().string(from: Date()))
                    var inDraft = false
                    do { try drafts.save(draft, for: conversationID); inDraft = true } catch { report(error) }
                    composerRecall[conversationID] = ComposerRecall(text: words, staged: staged, inDraft: inDraft)
                case .tooLate(let receipt):
                    if let receipt { state.apply(receipt: receipt) }
                    // Its frame is written: the turn's next step reads it.
                    guard let now = state.timelines[conversationID] else { return }
                    problem = tooLateSteers.tooLate(unread, in: now, assistant: assistant)
                case .inFlight:
                    problem = "That message is still being sent; press Esc again in a moment."
                }
            } catch {
                report(error)
            }
        }
    }

    func pump() {
        guard let engine, state.availability.isReady else { return }
        Task {
            let (report, texts, refusedDrafts) = await onOutbox { () -> (OutboxSender.Report, [String: String], [FailedConversationDraft]) in
                let report = engine.pump()
                var texts: [String: String] = [:]
                for receipt in report.receipts {
                    if let text = engine.outbox.text(of: receipt.message_id) { texts[receipt.message_id] = text }
                }
                return (report, texts, engine.outbox.failedDrafts)
            }
            failedDrafts = refusedDrafts
            if refusedDrafts.isEmpty && problem == refusedDraftNotice { problem = nil }
            guard !report.receipts.isEmpty || !report.conversations.isEmpty || !report.failed.isEmpty
                    || !report.steers.isEmpty else { return }
            for receipt in report.receipts {
                state.apply(receipt: receipt)
                if let cid = receipt.conversation_id, let text = texts[receipt.message_id] {
                    state.setPersonText(text, conversationID: cid, messageID: receipt.message_id)
                }
            }
            state.apply(steers: report.steers)
            for conversation in report.conversations {
                state.upsert(conversation)
            }
            for receipt in report.receipts {
                if let token = draftDestinations.removeValue(forKey: receipt.message_id),
                   token == navigation, let id = receipt.conversation_id {
                    focus(id)
                }
            }
            if !report.failed.isEmpty {
                problem = refusedDrafts.isEmpty ? "\(report.failed.count) message(s) could not be sent; see the conversation"
                    : refusedDraftNotice
            }
        }
    }

    // MARK: Refused conversations

    var selectedFailedDraft: FailedConversationDraft? {
        failedDrafts.first { $0.id == selectedFailedDraftID }
    }

    func selectFailedDraft(_ id: String? = nil) {
        guard let draft = failedDrafts.first(where: { id == nil || $0.id == id }) else { return }
        navigation += 1
        endFailedDraftEditing()
        newDraft.leave()
        lockedEntry = nil
        state.focus(nil)
        eventsGeneration += 1
        selectedFailedDraftID = draft.id
    }

    func changeFailedDraftFolder(_ draft: FailedConversationDraft) {
        guard failedDrafts.contains(where: { $0.id == draft.id }) else { return }
        endFailedDraftEditing()
        let savedRetry = try? Data(contentsOf: retryDraftURL(draft.id))
        composerBeforeRetry = newDraft
        workspaceDefaultBeforeRetry = draftNeedsWorkspaceDefault
        failedDraftKey = draft.id
        navigation += 1
        lockedEntry = nil
        state.focus(nil)
        eventsGeneration += 1
        draftNeedsWorkspaceDefault = false
        selectedFailedDraftID = nil
        newDraft = NewConversationDraft()
        newDraft.provider = draft.create.provider
        newDraft.providerChoice = draft.create.provider
        newDraft.workspace = draft.create.workspace
        newDraft.text = draft.firstMessage?.text ?? ""
        newDraft.attachments = draft.firstMessage?.staged ?? []
        newDraft.settings = draft.create.settings
        newDraft.confirmWiden = draft.create.confirm_widen ?? false
        newDraft.workspaceCheck = nil
        if let data = savedRetry,
           var saved = try? JSONDecoder().decode(NewConversationDraft.self, from: data) {
            saved.restore()
            newDraft = saved
        }
        newDraft.open()
        reconcileNewDraft()
        validateNewDraftWorkspace()
    }

    private func retryDraftURL(_ key: String) -> URL {
        paths.support.appendingPathComponent("retry-\(sha256Hex(Data(key.utf8))).json")
    }

    private func endFailedDraftEditing() {
        guard failedDraftKey != nil else { return }
        draftWorkspaceChecks += 1
        draftCheckInFlight = false
        // Keep the retry key until restoring, so didSet cannot save retry text
        // over the ordinary composer, even during navigation.
        if var saved = composerBeforeRetry {
            saved.restore()
            restoringComposer = true
            newDraft = saved
            restoringComposer = false
        }
        composerBeforeRetry = nil
        draftNeedsWorkspaceDefault = workspaceDefaultBeforeRetry
        failedDraftKey = nil
    }

    func copyFailedDraft(_ draft: FailedConversationDraft) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(draft.text, forType: .string)
    }

    func discardFailedDraft(_ id: String) {
        guard let engine else { return }
        Task {
            do {
                failedDrafts = try await onOutbox {
                    try engine.outbox.discardFailedDraft(id)
                    return engine.outbox.failedDrafts
                }
                if selectedFailedDraftID == id { selectedFailedDraftID = nil }
                try? FileManager.default.removeItem(at: retryDraftURL(id))
                if failedDraftKey == id { endFailedDraftEditing(); newDraft.leave() }
                if failedDrafts.isEmpty && problem == refusedDraftNotice { problem = nil }
            } catch { report(error) }
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

    /// Bring a conversation's oldest waiting card into view. Each request is its
    /// own, so asking again for the same conversation asks again.
    func revealApprovals(in conversationID: String) {
        reveals += 1
        approvalReveal = ApprovalReveal(conversationID: conversationID, token: reveals)
    }

    /// The view brought the requested card into view.
    func revealed(_ conversationID: String) {
        if approvalReveal?.conversationID == conversationID { approvalReveal = nil }
    }

    /// The daemon counts fewer pending approvals in the focused conversation than
    /// the timeline shows cards: one ended with no event saying so (C-27.5).
    /// Read the pending set again, which withdraws the cards it no longer lists;
    /// a mismatch that reading leaves as it was is not read again.
    private func refreshApprovalsIfStale() {
        guard let engine, let id = state.focusedConversationID, let timeline = state.timelines[id],
              let count = state.pendingApprovals[id] else { return }
        let shown = timeline.pendingApprovalItems.count
        // Agreeing again ends the mismatch, so a later one with the same counts is read too.
        guard count < shown else { staleRead[id] = nil; return }
        let seen = [count, shown]
        guard staleRead[id] != seen else { return }
        staleRead[id] = seen
        Task {
            if let approvals = try? await onOutbox({ try engine.approvals(conversationID: id) }) {
                state.apply(approvals: approvals, conversationID: id)
                updateBadge()
            }
        }
    }

    /// The approval id for a card the events made before `approval.list` was read.
    func approvalID(for card: ApprovalCard, conversationID: String) async -> String? {
        guard card.isPending else {
            problem = "That approval is no longer pending."
            return nil
        }
        if let id = card.approvalID { return id }
        guard let requestID = card.requestID else { return nil }
        // Provider request ids can repeat across turns. Keep the original
        // message as well as the request when the approval list fills its id.
        let owners = state.timelines[conversationID]?.turns.values.filter { turn in
            turn.pendingApprovals.contains {
                $0.requestID == requestID && $0.kind == card.kind && $0.display == card.display
            }
        } ?? []
        guard owners.count == 1, let messageID = owners.first?.messageID else {
            problem = "That approval is no longer pending."
            return nil
        }
        guard let engine, let approvals = try? await onOutbox({ try engine.approvals(conversationID: conversationID) }) else {
            return nil
        }
        state.apply(approvals: approvals, conversationID: conversationID)
        // The card joined to one of the daemon's pending approvals, or none: another
        // pending approval of the same kind is not this one (C-27.5). A missing
        // provider id cannot authenticate an event card: its legacy approval is
        // answered on its own immutable approval-id card (C-27.1).
        let matches = state.timelines[conversationID]?.turns[messageID]?.pendingApprovals.filter {
            $0.requestID == requestID && $0.kind == card.kind && $0.display == card.display
        } ?? []
        guard matches.count == 1, let id = matches.first?.approvalID,
              approvals.contains(where: {
                  $0.approval_id == id && $0.message_id == messageID && $0.conversation_id == conversationID
                      && $0.requestID == requestID
                      && $0.state == "pending" && $0.kind == card.kind && $0.display == card.display
              }) else {
            problem = "That approval is no longer pending."
            return nil
        }
        return id
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
            case .steerTooLate: return "Too late to withdraw it: the running turn has read that message."
            }
        }
        return "\(error)"
    }

    private func updateBadge() {
        let count = state.pendingApprovalCount
        NSApp?.dockTile.badgeLabel = count > 0 ? String(count) : nil
    }

    private func post(_ intent: NotificationIntent) {
        Task {
            let content = UNMutableNotificationContent()
            content.title = intent.title
            content.body = intent.body
            content.userInfo = ["conversation_id": intent.conversationID]
            if intent.kind == .approval, let engine {
                let detail: ApprovalDetail? = try? await onOutbox {
                    let approvals = try engine.approvals(conversationID: intent.conversationID)
                    guard let approval = ApprovalNotificationTarget.candidate(
                        from: approvals, conversationID: intent.conversationID, messageID: intent.messageID) else { return nil }
                    return try engine.approvalDetail(approval.approval_id)
                }
                if let detail, let target = ApprovalNotificationTarget(detail: detail) {
                    content.categoryIdentifier = ApprovalNotificationDelegate.categoryID
                    content.userInfo = target.userInfo
                    let display = detail.approval.display
                    let summary = display.command ?? display.input ?? display.description ?? display.tool
                    if let summary { content.body += "\n" + summary }
                }
            }
            let request = UNNotificationRequest(identifier: intent.id, content: content, trigger: nil)
            try? await UNUserNotificationCenter.current().add(request)
        }
    }

    private func handleNotification(action: String, userInfo: [AnyHashable: Any]) {
        guard action != UNNotificationDismissActionIdentifier,
              let conversationID = userInfo["conversation_id"] as? String, !conversationID.isEmpty else { return }
        #if !SUBFLEET_VIEW_TEST
        SubfleetAppDelegate.openMain?()
        #endif
        NSApp?.activate(ignoringOtherApps: true)
        focus(conversationID)
        guard action == ApprovalNotificationDelegate.allowOnceID else { return }
        guard let target = ApprovalNotificationTarget(userInfo: userInfo) else {
            problem = "Review the pending request in the conversation."
            return
        }
        Task {
            guard let detail = await approvalDetail(target.approvalID, reveal: false) else { return }
            guard target.canAllow(detail) else {
                problem = "This request needs review. Use the approval card's details in the conversation."
                return
            }
            _ = await respond(detail, decision: "allow", answers: nil, message: nil, reviewedMasked: false)
        }
    }
}
/// Words (and images) taken back from the running turn, for the composer to show again.
struct ComposerRecall: Equatable, Identifiable {
    let id = UUID()
    var text: String
    var staged: [StagedAttachment]
    /// The words and images are in the conversation's draft on disk as well: a
    /// composer that loads that draft has them already. False when saving it
    /// failed, so the composer merges them in itself.
    var inDraft = false
}
#endif
