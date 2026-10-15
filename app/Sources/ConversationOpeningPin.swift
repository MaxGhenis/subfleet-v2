// Opening a conversation stays at its end through lazy row measurement and the
// first events page, until the person scrolls. The later follow rule belongs to
// ConversationView. This observer is shared by macOS 14 and 15+.
#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

struct ConversationOpeningPin: NSViewRepresentable {
    func makeNSView(context: Context) -> OpeningPinView { OpeningPinView() }
    func updateNSView(_ view: OpeningPinView, context: Context) { view.attach() }

    static func dismantleNSView(_ view: OpeningPinView, coordinator: ()) { view.detach() }
}

final class OpeningPinView: NSView {
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
        // Let SwiftUI finish this layout before scrolling. A new measurement
        // schedules another pass, even when no FollowKey changed.
        pinScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.pinScheduled = false
            self.pin()
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
