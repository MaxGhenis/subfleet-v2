// Diagnostic only: mount the app's .id(conversation_id) navigation boundary.
@MainActor func navigationInput(_ root: URL) async -> [String: Any] {
    var aTrace: [[String: Any]] = []
    var bTrace: [[String: Any]] = []
    var navigated = false
    ConversationFollowTest.record = { cid, entry in
        if cid == "navigation-a" { aTrace.append(entry) }
        if cid == "navigation-b" && navigated { bTrace.append(entry) }
    }
    defer { ConversationFollowTest.record = nil }
    func pins(_ view: NSView) -> [OpeningPinView] {
        view.subviews.flatMap { child in
            (child as? OpeningPinView).map { [$0] } ?? pins(child)
        }
    }
    let a = await Scene("navigation-a", root: root, prior: 120,
                        priorText: { "navigation A row \($0)" }, tray: [],
                        pageDelay: 0.25, pageSize: 40, pauseCatchup: true, holdCatchup: true)
    defer { a.close() }
    let oldPin = pins(a.host).first!
    let request = UUID()
    NotificationCenter.default.post(name: .conversationRowScroll, object: a.cid,
                                    userInfo: ["request": request, "target": "person:m000100"])
    await waitReady("navigation.a_suspended", view: a.host, state: { a.readiness() }) {
        aTrace.contains { $0["event"] as? String == "reveal" && $0["suspended"] as? Bool == true }
    }
    let aSuspended = aTrace.last?["suspended"] as? Bool == true
    let aPending = aTrace.last?["event"] as? String == "reveal"
    let b = await Scene("navigation-b", root: root, prior: 120,
                        priorText: { "navigation B row \($0)" }, tray: [],
                        pageDelay: 0.25, pageSize: 40, pauseCatchup: true, holdCatchup: true)
    let shown = b.model.state.conversation(b.cid)!
    b.window.contentView = nil
    b.close()
    navigated = true
    a.host.rootView = AnyView(ConversationView(model: b.model, conversation: shown).id(b.cid)
        .environment(\.searchPalette, b.palette).environment(\.textScale, ProbeLayout.textScale)
        .transaction { $0.animation = nil; $0.disablesAnimations = true })
    b.host = a.host
    b.window = a.window
    await waitReady("navigation.b_pin", view: b.host, state: { b.readiness() }) {
        pins(b.host).contains { $0.conversationID == b.cid }
    }
    let newPin = pins(b.host).first { $0.conversationID == b.cid }!
    b.daemon.resumeCatchup()
    await b.waitFolded(b.daemon.lastSeq, "navigation.b_caught_up")
    await stablePosition(b.host)
    return ["a_suspended": aSuspended, "a_pending": aPending,
            "pin_replaced": oldPin !== newPin, "b_trace": bTrace,
            "b_caught_up": b.caughtUp, "b_position": sample(b.host),
            "a_trace": aTrace]
}
