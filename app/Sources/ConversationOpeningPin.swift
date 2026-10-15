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
    private var pinScheduled = false
    private var personScrolled = false

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
        document.postsFrameChangedNotifications = true
        found.contentView.postsBoundsChangedNotifications = true
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSView.frameDidChangeNotification, object: document,
                                             queue: .main) { [weak self] _ in self?.schedulePin() })
        observers.append(center.addObserver(forName: NSView.boundsDidChangeNotification, object: found.contentView,
                                             queue: .main) { [weak self] _ in self?.schedulePin() })
        observers.append(center.addObserver(forName: NSScrollView.willStartLiveScrollNotification, object: found,
                                             queue: .main) { [weak self] _ in self?.personScrolled = true })
        schedulePin()
    }

    func detach() {
        for observer in observers { NotificationCenter.default.removeObserver(observer) }
        observers = []
        scroll = nil
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
