// Opening a conversation stays at its end through lazy row measurement and the
// first events page, until the person scrolls. The person's own send keeps the end
// the same way, until the person scrolls or a reveal starts. The later follow rule
// belongs to ConversationView. This observer is shared by macOS 14 and 15+.
#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct ConversationOpeningPin: NSViewRepresentable {
    var conversationID: String? = nil
    var onRevealEndVisible: (UUID) -> Void = { _ in }
    /// The request's own row was presented in the clip: the reveal is no longer outstanding.
    var onRevealLanded: (UUID) -> Void = { _ in }
    /// The person scrolled while a reveal was still outstanding.
    var onPersonScroll: () -> Void = {}
    /// The person scrolled the timeline (live scroll, wheel or paging key), reveal or not.
    var onPersonInput: () -> Void = {}
    var ownSendLanding = false
    var onOwnSendLanded: () -> Void = {}
    func makeNSView(context: Context) -> OpeningPinView {
        let view = OpeningPinView()
        update(view)
        return view
    }
    func updateNSView(_ view: OpeningPinView, context: Context) {
        update(view)
        view.attach()
    }

    private func update(_ view: OpeningPinView) {
        view.conversationID = conversationID
        view.onRevealEndVisible = onRevealEndVisible
        view.onRevealLanded = onRevealLanded
        view.onPersonScroll = onPersonScroll
        view.onPersonInput = onPersonInput
        view.onOwnSendLanded = onOwnSendLanded
        view.ownSendLanding = ownSendLanding
    }

    /// `target` is the row the reveal scrolls to; the view marks it with a
    /// `RevealTargetMark`, so its presented geometry can end the reveal.
    @discardableResult static func revealRow(in conversationID: String, target: String? = nil) -> UUID {
        let request = UUID()
        var info: [AnyHashable: Any] = ["request": request]
        if let target { info["target"] = target }
        NotificationCenter.default.post(name: .conversationRowScroll, object: conversationID, userInfo: info)
        return request
    }

    static func didRevealRow(in conversationID: String, request: UUID) {
        NotificationCenter.default.post(name: .conversationRowScrollCompleted, object: conversationID,
                                        userInfo: ["request": request])
    }

    /// The view abandoned this request (its target left the model, was never in it,
    /// or the person sent): the pin stops waiting for its row. Only this request is
    /// cancelled; a newer one stays outstanding.
    static func abandonReveal(in conversationID: String, request: UUID) {
        NotificationCenter.default.post(name: .conversationRowScrollAbandoned, object: conversationID,
                                        userInfo: ["request": request])
    }

    static func dismantleNSView(_ view: OpeningPinView, coordinator: ()) { view.detach() }
}

extension Notification.Name {
    static let conversationRowScroll = Notification.Name("SubfleetConversationRowScroll")
    static let conversationRowScrollCompleted = Notification.Name("SubfleetConversationRowScrollCompleted")
    static let conversationRowScrollAbandoned = Notification.Name("SubfleetConversationRowScrollAbandoned")
    static let conversationRevealTargetMoved = Notification.Name("SubfleetConversationRevealTargetMoved")
}

/// Marks a reveal's target row in the timeline's document. The opening pin reads
/// its presented frame to tell whether the row is in the clip.
struct RevealTargetMark: NSViewRepresentable {
    let conversationID: String
    let itemID: String
    func makeNSView(context: Context) -> RevealTargetView {
        let view = RevealTargetView()
        view.setAccessibilityElement(false)
        updateNSView(view, context: context)
        return view
    }
    func updateNSView(_ view: RevealTargetView, context: Context) {
        view.conversationID = conversationID
        view.itemID = itemID
    }
}

final class RevealTargetView: NSView {
    // A reused row gets the identity of its new target: the pin checks it again.
    var conversationID: String? { didSet { if conversationID != oldValue { placeAgain() } } }
    var itemID: String? { didSet { if itemID != oldValue { placeAgain() } } }
    /// Where the pin was last told this mark is: its frame in the document's
    /// coordinates, and whether it was hidden.
    private var placed: (frame: NSRect, hidden: Bool)?
    private var placement: CFRunLoopObserver?

    // The row's own content takes every click.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        watchPlacement(window != nil)
        placeAgain()
    }
    override func viewDidMoveToSuperview() { super.viewDidMoveToSuperview(); placeAgain() }
    override func setFrameOrigin(_ newOrigin: NSPoint) { super.setFrameOrigin(newOrigin); moved() }
    override func setFrameSize(_ newSize: NSSize) { super.setFrameSize(newSize); moved() }

    /// SwiftUI moves a row by moving the host it makes for this view, so the mark's
    /// own frame can stay put while its place in the document changes (review r6:
    /// a layout moved the target from y=300 to y=1,700 with no callback). Before the
    /// main run loop next sleeps (the last `beforeWaiting` observer), the mark compares
    /// its frame in the document's coordinates with
    /// the last one it reported. Every move happens on the main thread within some pass, whatever
    /// made it (the mark, an ancestor, the document's layout, a new parent), so the
    /// pass that presents it reports it.
    private func watchPlacement(_ on: Bool) {
        if let placement { CFRunLoopObserverInvalidate(placement); self.placement = nil }
        guard on else { return }
        let observer = CFRunLoopObserverCreateWithHandler(nil, CFRunLoopActivity.beforeWaiting.rawValue, true,
                                                          CFIndex.max) { [weak self] observer, _ in
            MainActor.assumeIsolated {
                guard let self else { if let observer { CFRunLoopObserverInvalidate(observer) }; return }
                self.moved()
            }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        placement = observer
    }

    /// A new identity, window or parent: the pin is told again wherever the mark is.
    private func placeAgain() {
        placed = nil
        moved()
    }

    /// Tells the pin when the mark's place in the document changed since it last did.
    private func moved() {
        guard window != nil, let conversationID else { return }
        let document = enclosingScrollView?.documentView
        let frame = document.map { convert(bounds, to: $0) } ?? .zero
        let hidden = isHiddenOrHasHiddenAncestor
        if let placed, placed.frame == frame, placed.hidden == hidden { return }
        placed = (frame, hidden)
        NotificationCenter.default.post(name: .conversationRevealTargetMoved, object: conversationID)
    }
}

final class OpeningPinView: NSView {
    var conversationID: String?
    var onRevealEndVisible: (UUID) -> Void = { _ in }
    var onRevealLanded: (UUID) -> Void = { _ in }
    var onPersonScroll: () -> Void = {}
    var onPersonInput: () -> Void = {}
    var onOwnSendLanded: () -> Void = {}
    var ownSendLanding = false {
        didSet {
            guard ownSendLanding != oldValue else { return }
            landingCheck?.cancel()
            if ownSendLanding { followingOwnSend = true; schedulePin(); scheduleLandingCheck() }
        }
    }
    /// After the person's own send, document growth is pinned to the end until the person
    /// scrolls or a reveal starts. The landing check measuring the end does not end it
    /// (review r8: rows above the clip grew 600 pt after a quiet landing, and the reply
    /// was left 779 pt behind).
    private var followingOwnSend = false
    private var landingCheck: DispatchWorkItem?
    private var landingRevision = 0
    private weak var scroll: NSScrollView?
    private var observers: [NSObjectProtocol] = []
    private var inputMonitor: Any?
    private var pinScheduled = false
    private var personScrolled = false
    private var viewportSize = NSSize.zero
    /// The newest reveal whose own row has not yet been presented in the clip.
    private var outstanding: (request: UUID, target: String?)?
    /// The newest request the view abandoned. Its notice can reach the pin before
    /// the request's own does, so a request already abandoned never becomes outstanding.
    private var abandoned: UUID?
    private var revealCheckScheduled = false

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        attach()
    }

    override func viewDidMoveToSuperview() {
        super.viewDidMoveToSuperview()
        attach()
    }

    func attach() {
        guard scroll == nil, let found = enclosingScrollView, let document = found.documentView else { return }
        scroll = found
        viewportSize = found.contentView.bounds.size
        document.postsFrameChangedNotifications = true
        found.contentView.postsBoundsChangedNotifications = true
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: .conversationRowScroll, object: nil, queue: .main) { [weak self] note in
            guard let self, note.object as? String == self.conversationID else { return }
            self.personScrolled = true
            self.followingOwnSend = false
            self.ownSendLanding = false
            if let request = note.userInfo?["request"] as? UUID, request != self.abandoned {
                self.outstanding = (request, note.userInfo?["target"] as? String)
            }
        })
        observers.append(center.addObserver(forName: .conversationRowScrollAbandoned, object: nil,
                                             queue: .main) { [weak self] note in
            guard let self, note.object as? String == self.conversationID,
                  let request = note.userInfo?["request"] as? UUID else { return }
            self.abandoned = request
            if self.outstanding?.request == request { self.outstanding = nil }
        })
        observers.append(center.addObserver(forName: .conversationRowScrollCompleted, object: nil,
                                             queue: .main) { [weak self] note in
            guard let self, note.object as? String == self.conversationID,
                  let request = note.userInfo?["request"] as? UUID else { return }
            // A nearby reveal can leave the marker mounted, so no new onAppear
            // would release suspension. Confirm its visibility from the presented
            // clip, independent of lazy callbacks. A completion can arrive before
            // its scroll is laid out (a loaded opening's clip still showed the old
            // end), so measure on the next turn, after the pending layout.
            DispatchQueue.main.async { [weak self] in self?.confirmEndVisible(request) }
        })
        observers.append(center.addObserver(forName: NSView.frameDidChangeNotification, object: document,
                                             queue: .main) { [weak self] _ in
            self?.schedulePin()
            self?.scheduleLandingCheck()
            self?.scheduleRevealCheck("document")
        })
        observers.append(center.addObserver(forName: NSView.boundsDidChangeNotification, object: found.contentView,
                                             queue: .main) { [weak self] _ in
            self?.viewportChanged()
            self?.scheduleLandingCheck()
            self?.scheduleRevealCheck("clip")
        })
        observers.append(center.addObserver(forName: .conversationRevealTargetMoved, object: nil,
                                             queue: .main) { [weak self] note in
            guard let self, note.object as? String == self.conversationID else { return }
            self.scheduleRevealCheck("mark")
        })
        observers.append(center.addObserver(forName: NSScrollView.willStartLiveScrollNotification, object: found,
                                             queue: .main) { [weak self] _ in self?.personScrolledNow() })
        // Keyboard paging need not start an AppKit live-scroll gesture. Watch
        // only this view's input, so scrolling the sidebar or typing in the
        // composer does not release the opening pin.
        inputMonitor = NSEvent.addLocalMonitorForEvents(matching: [.scrollWheel, .keyDown]) { [weak self] event in
            self?.noteScrollInput(event)
            return event
        }
        schedulePin()
        scheduleLandingCheck()
    }

    func detach() {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        if let inputMonitor { NSEvent.removeMonitor(inputMonitor) }
        inputMonitor = nil
        scroll = nil
        outstanding = nil
        followingOwnSend = false
        ownSendLanding = false
    }

    private func confirmEndVisible(_ request: UUID) {
        guard let scroll else { return }
        // Apply the reveal's pending scroll before reading the clip; reading
        // first can confirm the end the reveal is about to leave.
        (scroll.window?.contentView ?? scroll).layoutSubtreeIfNeeded()
        checkReveal(request, source: "completion")
    }

    /// Presented geometry decides a reveal after each change while it is outstanding:
    /// the clip, the document, or the target's mark moving in the document.
    private func scheduleRevealCheck(_ source: String) {
        guard outstanding != nil, !revealCheckScheduled else { return }
        revealCheckScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.revealCheckScheduled = false
            if let request = self.outstanding?.request { self.checkReveal(request, source: source) }
        }
    }

    /// A reveal is outstanding until its own row is presented in the clip. Until
    /// then the end's visibility releases nothing: the document can shrink under
    /// the clip and clamp it to the end before the row is shown (CI run
    /// 40251214771). Once the row is presented the reveal has landed, and the end
    /// visible beside it releases suspension (a nearby reveal).
    private func checkReveal(_ request: UUID, source: String) {
        guard let scroll, let document = scroll.documentView,
              let pending = outstanding, pending.request == request else { return }
        let clip = scroll.contentView.bounds
        let distance = document.isFlipped ? document.bounds.maxY - clip.maxY : clip.minY - document.bounds.minY
        let presented = targetPresented(pending.target, in: scroll)
        #if SUBFLEET_VIEW_TEST
        if let conversationID {
            ConversationFollowTest.record?(conversationID, ["event": "reveal_check", "source": source,
                "presented": presented, "distance": Double(distance), "target": pending.target ?? ""])
        }
        #endif
        if presented {
            outstanding = nil
            onRevealLanded(request)
        }
        if presented && distance <= Theme.space.column + 1 { onRevealEndVisible(request) }
    }

    /// Whether the target row's mark is laid out in the clip with some of its height showing.
    private func targetPresented(_ target: String?, in scroll: NSScrollView) -> Bool {
        guard let target, let document = scroll.documentView else { return false }
        let clip = scroll.contentView
        func marks(_ view: NSView) -> [RevealTargetView] {
            view.subviews.flatMap { ($0 as? RevealTargetView).map { [$0] } ?? marks($0) }
        }
        return marks(document).contains { mark in
            guard mark.itemID == target, !mark.isHiddenOrHasHiddenAncestor else { return false }
            return clip.bounds.intersection(mark.convert(mark.bounds, to: clip)).height > 1
        }
    }

    /// The person's own scroll ends an outstanding reveal: suspension is then
    /// released as it was before reveals were tracked, when the end is shown.
    private func personScrolledNow() {
        personScrolled = true
        // Cancel queued re-pins immediately, before SwiftUI publishes the new state.
        followingOwnSend = false
        ownSendLanding = false
        onPersonInput()
        guard let request = outstanding?.request else { return }
        outstanding = nil
        onPersonScroll()
        // The marker may already be mounted (a shrink showed the end), so no new
        // onAppear would follow; measure after this input's scroll as well.
        DispatchQueue.main.async { [weak self] in
            guard let self, let scroll = self.scroll, let document = scroll.documentView else { return }
            let clip = scroll.contentView.bounds
            let distance = document.isFlipped ? document.bounds.maxY - clip.maxY : clip.minY - document.bounds.minY
            if distance <= Theme.space.column + 1 { self.onRevealEndVisible(request) }
        }
    }

    private func noteScrollInput(_ event: NSEvent) {
        guard let scroll, event.window === scroll.window else { return }
        if event.type == .scrollWheel {
            let clip = scroll.contentView
            if clip.bounds.contains(clip.convert(event.locationInWindow, from: nil)) { personScrolledNow() }
        } else if [49, 115, 116, 119, 121, 125, 126].contains(event.keyCode),
                  let responder = scroll.window?.firstResponder as? NSView,
                  responder === scroll || responder.isDescendant(of: scroll) {
            personScrolledNow()
        }
    }

    private func viewportChanged() {
        guard let size = scroll?.contentView.bounds.size, size != viewportSize else { return }
        viewportSize = size
        // An offset change alone may be a Review jump to an approval card.
        // Only layout growth or a changed viewport needs the opening pin.
        schedulePin()
    }

    private func schedulePin() {
        guard !personScrolled || followingOwnSend, !pinScheduled else { return }
        // Coalesce this SwiftUI update first, then run after the scroll work
        // queued by that update. Offset-only Review jumps schedule nothing.
        pinScheduled = true
        DispatchQueue.main.async { [weak self] in
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.pinScheduled = false
                self.pin()
            }
        }
    }

    private func pin() {
        guard !personScrolled || followingOwnSend, let scroll, let document = scroll.documentView else { return }
        let clip = scroll.contentView
        let end = document.isFlipped ? max(document.bounds.minY, document.bounds.maxY - clip.bounds.height)
            : document.bounds.minY
        guard abs(clip.bounds.minY - end) > 0.5 else { return }
        // Direct clip scrolling has no animation on either supported OS path.
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: end))
        scroll.reflectScrolledClipView(clip)
    }

    /// A lazy marker can appear over 100 pt from the end, and the send's move can stop
    /// short of it. Until presented geometry, after layout and 300 ms without a document
    /// or clip change (including the send's 150 ms glide), confirms the settled end,
    /// re-pin a clip left short. Measuring the end ends only this check: growth after it
    /// is still pinned while the pin follows the own send.
    private func scheduleLandingCheck() {
        guard ownSendLanding, scroll != nil else { return }
        landingRevision += 1
        let revision = landingRevision
        landingCheck?.cancel()
        let check = DispatchWorkItem { [weak self] in
            guard let self, self.ownSendLanding, self.landingRevision == revision,
                  let scroll = self.scroll, let document = scroll.documentView else { return }
            (scroll.window?.contentView ?? scroll).layoutSubtreeIfNeeded()
            // Applying layout can itself grow the document and schedule a new check.
            guard self.ownSendLanding, self.landingRevision == revision else { return }
            let clip = scroll.contentView.bounds
            let distance = document.isFlipped ? document.bounds.maxY - clip.maxY : clip.minY - document.bounds.minY
            if distance <= Theme.space.column + 1 {
                self.ownSendLanding = false
                self.onOwnSendLanded()
            } else {
                self.schedulePin()
                self.scheduleLandingCheck()
            }
        }
        landingCheck = check
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: check)
    }
}
#endif
