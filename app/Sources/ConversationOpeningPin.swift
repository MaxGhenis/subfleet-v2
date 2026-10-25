// Opening a conversation stays at its end through lazy row measurement and the
// first events page, until the person scrolls. The later follow rule belongs to
// ConversationView. This observer is shared by macOS 14 and 15+.
#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct ConversationOpeningPin: NSViewRepresentable {
    var conversationID: String? = nil
    var onRevealEndVisible: (UUID) -> Void = { _ in }
    func makeNSView(context: Context) -> OpeningPinView {
        let view = OpeningPinView()
        view.conversationID = conversationID
        view.onRevealEndVisible = onRevealEndVisible
        return view
    }
    func updateNSView(_ view: OpeningPinView, context: Context) {
        view.conversationID = conversationID
        view.onRevealEndVisible = onRevealEndVisible
        view.attach()
    }

    @discardableResult static func revealRow(in conversationID: String) -> UUID {
        let request = UUID()
        NotificationCenter.default.post(name: .conversationRowScroll, object: conversationID,
                                        userInfo: ["request": request])
        return request
    }

    static func didRevealRow(in conversationID: String, request: UUID) {
        NotificationCenter.default.post(name: .conversationRowScrollCompleted, object: conversationID,
                                        userInfo: ["request": request])
    }

    static func dismantleNSView(_ view: OpeningPinView, coordinator: ()) { view.detach() }
}

extension Notification.Name {
    static let conversationRowScroll = Notification.Name("SubfleetConversationRowScroll")
    static let conversationRowScrollCompleted = Notification.Name("SubfleetConversationRowScrollCompleted")
}

final class OpeningPinView: NSView {
    var conversationID: String?
    var onRevealEndVisible: (UUID) -> Void = { _ in }
    private weak var scroll: NSScrollView?
    private var observers: [NSObjectProtocol] = []
    private var inputMonitor: Any?
    private var pinScheduled = false
    private var personScrolled = false
    private var viewportSize = NSSize.zero

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
                                             queue: .main) { [weak self] _ in self?.schedulePin() })
        observers.append(center.addObserver(forName: NSView.boundsDidChangeNotification, object: found.contentView,
                                             queue: .main) { [weak self] _ in self?.viewportChanged() })
        observers.append(center.addObserver(forName: NSScrollView.willStartLiveScrollNotification, object: found,
                                             queue: .main) { [weak self] _ in self?.personScrolled = true })
        // Keyboard paging need not start an AppKit live-scroll gesture. Watch
        // only this view's input, so scrolling the sidebar or typing in the
        // composer does not release the opening pin.
        inputMonitor = NSEvent.addLocalMonitorForEvents(matching: [.scrollWheel, .keyDown]) { [weak self] event in
            self?.noteScrollInput(event)
            return event
        }
        schedulePin()
    }

    func detach() {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        if let inputMonitor { NSEvent.removeMonitor(inputMonitor) }
        inputMonitor = nil
        scroll = nil
    }

    private func confirmEndVisible(_ request: UUID) {
        guard let scroll, let document = scroll.documentView else { return }
        // Apply the reveal's pending scroll before reading the clip; reading
        // first can confirm the end the reveal is about to leave.
        (scroll.window?.contentView ?? scroll).layoutSubtreeIfNeeded()
        let clip = scroll.contentView.bounds
        let distance = document.isFlipped ? document.bounds.maxY - clip.maxY : clip.minY - document.bounds.minY
        if distance <= Theme.space.column + 1 { onRevealEndVisible(request) }
    }

    private func noteScrollInput(_ event: NSEvent) {
        guard let scroll, event.window === scroll.window else { return }
        if event.type == .scrollWheel {
            let clip = scroll.contentView
            if clip.bounds.contains(clip.convert(event.locationInWindow, from: nil)) { personScrolled = true }
        } else if [49, 115, 116, 119, 121, 125, 126].contains(event.keyCode),
                  let responder = scroll.window?.firstResponder as? NSView,
                  responder === scroll || responder.isDescendant(of: scroll) {
            personScrolled = true
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
        guard !personScrolled, !pinScheduled else { return }
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
        guard !personScrolled, let scroll, let document = scroll.documentView else { return }
        let clip = scroll.contentView
        let end = document.isFlipped ? max(document.bounds.minY, document.bounds.maxY - clip.bounds.height)
            : document.bounds.minY
        guard abs(clip.bounds.minY - end) > 0.5 else { return }
        // Direct clip scrolling has no animation on either supported OS path.
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: end))
        scroll.reflectScrolledClipView(clip)
    }
}
#endif
