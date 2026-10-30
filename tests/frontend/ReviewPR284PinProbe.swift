import AppKit
import SwiftUI

// Compile the real pin without SUBFLEET_VIEW_TEST. Only its theme dependency is stubbed.
enum Theme { static let space = Spacing(); struct Spacing { let column: CGFloat = 24 } }
final class ReviewDocument: NSView {
    let coordinateFlipped: Bool
    init(flipped: Bool) { self.coordinateFlipped = flipped; super.init(frame: NSRect(x: 0, y: 0, width: 400, height: 12000)) }
    required init?(coder: NSCoder) { fatalError() }
    override var isFlipped: Bool { coordinateFlipped }
}

@main struct ReviewPR284PinProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var results: [[String: Any]] = []
        for flipped in [true, false] {
            for action in ["identity", "pending_identity", "repeat", "coalesce", "person", "abandon", "unrelated_abandon", "own_send", "target_removed", "detach", "guard", "fallback", "threshold", "growth_after_input"] {
                if action == "growth_after_input" && !flipped { continue }
                let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 550), styleMask: [.borderless], backing: .buffered, defer: false)
                window.isReleasedWhenClosed = false
                let scroll = NSScrollView(frame: window.contentRect(forFrameRect: window.frame))
                let document = ReviewDocument(flipped: flipped)
                scroll.documentView = document
                let pin = OpeningPinView(frame: NSRect(x: 0, y: 0, width: 1, height: 1))
                pin.conversationID = "A"
                let mark = RevealTargetView(frame: NSRect(x: 0, y: action == "pending_identity" ? 100000 : 1000, width: 400, height: 100))
                mark.conversationID = "A"; mark.itemID = "target"
                document.addSubview(pin); document.addSubview(mark)
                window.contentView = scroll
                window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
                var landed = 0, callbacks = 0, releases = 0, ownLanded = 0
                var growthGap: Double = -1, replyGap: Double = -1
                var pending = true
                var lastRequest: UUID?
                pin.onRevealLanded = { _ in landed += 1; pending = false }
                pin.onOwnSendLanded = { ownLanded += 1 }
                pin.onRevealEndVisible = { request in callbacks += 1; releases += 1; lastRequest = request }
                pin.onLandedEndVisible = { request in
                    callbacks += 1; lastRequest = request
                    if !pending { releases += 1 }
                }
                pin.attach()
                func drain() async {
                    for _ in 0..<5 { await withCheckedContinuation { c in DispatchQueue.main.async { c.resume() } } }
                }
                func position(_ gap: CGFloat) {
                    let y = flipped ? document.bounds.maxY - scroll.contentView.bounds.height - gap : document.bounds.minY + gap
                    scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
                    scroll.reflectScrolledClipView(scroll.contentView)
                }
                await drain()
                let request = ConversationOpeningPin.revealRow(in: "A", target: "target")
                scroll.contentView.scroll(to: NSPoint(x: 0, y: 1000))
                scroll.reflectScrolledClipView(scroll.contentView)
                ConversationOpeningPin.didRevealRow(in: "A", request: request)
                await drain()
                let prepared = landed
                switch action {
                case "identity": pin.conversationID = "B"
                case "pending_identity":
                    pin.conversationID = "B"
                    mark.setFrameOrigin(NSPoint(x: 0, y: flipped ? 11500 : 0))
                case "person": NotificationCenter.default.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
                case "abandon": ConversationOpeningPin.abandonReveal(in: "A", request: request)
                case "unrelated_abandon": ConversationOpeningPin.abandonReveal(in: "A", request: UUID())
                case "own_send": pin.ownSendLanding = true
                case "target_removed": mark.removeFromSuperview()
                case "detach": pin.detach()
                case "guard": pending = true
                case "fallback": pending = true; pin.onLandedEndVisible = nil
                case "growth_after_input":
                    NotificationCenter.default.post(name: NSScrollView.willStartLiveScrollNotification, object: scroll)
                    pin.ownSendLanding = true
                    let deadline = Date().addingTimeInterval(5)
                    while ownLanded == 0 && Date() < deadline { try await Task.sleep(nanoseconds: 10_000_000) }
                default: break
                }
                // No live scroll or keyboard input accompanies this geometry change.
                position(action == "threshold" ? 26 : 0)
                await drain()
                let at26 = callbacks
                if action == "threshold" { position(25); await drain() }
                let first = callbacks
                let offset = scroll.contentView.bounds.minY
                if action == "repeat" {
                    for _ in 0..<10 {
                        NotificationCenter.default.post(name: .conversationRevealTargetMoved, object: pin.conversationID)
                        await drain()
                    }
                }
                if action == "coalesce" {
                    for _ in 0..<1000 { NotificationCenter.default.post(name: .conversationRevealTargetMoved, object: pin.conversationID) }
                    await drain()
                }
                if action == "growth_after_input" {
                    document.setFrameSize(NSSize(width: 400, height: 12600))
                    await drain()
                    growthGap = Double(document.bounds.maxY - scroll.contentView.bounds.maxY)
                    document.setFrameSize(NSSize(width: 400, height: 11700))
                    scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(scroll.contentView.bounds).origin)
                    scroll.reflectScrolledClipView(scroll.contentView)
                    await drain()
                    replyGap = Double(document.bounds.maxY - scroll.contentView.bounds.maxY)
                }
                results.append(["action": action, "flipped": flipped, "landed": prepared, "landed_after": landed,
                                "first": first, "callbacks": callbacks, "releases": releases,
                                "at26": at26, "same_request": lastRequest == request,
                                "conversation": pin.conversationID ?? "", "offset_before": Double(offset),
                                "offset_after": Double(scroll.contentView.bounds.minY), "own_landed": ownLanded,
                                "growth_gap": growthGap, "reply_gap": replyGap])
                pin.detach(); window.contentView = nil; window.close()
            }
        }
        print(String(decoding: try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), as: UTF8.self))
    }
}
