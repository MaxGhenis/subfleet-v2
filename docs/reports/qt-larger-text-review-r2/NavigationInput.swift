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

// All six orders run through the app's actual navigation identity boundary.
@MainActor func navigationMatrix(_ root: URL) async -> [String: Any] {
    let order = ProcessInfo.processInfo.environment["R2_NAVIGATION_ORDER"]!.split(separator: ",").map(String.init)
    var aTrace: [[String: Any]] = [], bTrace: [[String: Any]] = []
    var navigated = false
    ConversationFollowTest.record = { cid, entry in
        if cid == "matrix-a" { aTrace.append(entry) }
        if cid == "matrix-b" && navigated { bTrace.append(entry) }
    }
    defer { ConversationFollowTest.record = nil }
    func pins(_ view: NSView) -> [OpeningPinView] {
        descendants(view).compactMap { $0 as? OpeningPinView }
    }
    let a = await Scene("matrix-a", root: root, prior: 120,
                        priorText: { "matrix A row \($0)" }, tray: [],
                        pageDelay: 0.25, pageSize: 40, pauseCatchup: true, holdCatchup: true)
    defer { a.close() }
    let oldPin = pins(a.host).first!
    NotificationCenter.default.post(name: .conversationRowScroll, object: a.cid,
                                    userInfo: ["request": UUID(), "target": "person:m000100"])
    await waitReady("matrix.a_suspended", view: a.host, state: { a.readiness() }) {
        aTrace.last?["suspended"] as? Bool == true
    }
    let aSuspended = aTrace.last?["suspended"] as? Bool == true
    let b = await Scene("matrix-b", root: root, prior: 120,
                        priorText: { "matrix B row \($0)" }, tray: [],
                        pageDelay: 0.25, pageSize: 40, pauseCatchup: true, holdCatchup: true)
    let shown = b.model.state.conversation(b.cid)!
    b.window.contentView = nil
    b.close()
    var operations: [[String: Any]] = []
    for action in order {
        switch action {
        case "change":
            navigated = true
            a.host.rootView = AnyView(ConversationView(model: b.model, conversation: shown).id(b.cid)
                .environment(\.searchPalette, b.palette).environment(\.textScale, ProbeLayout.textScale)
                .transaction { $0.animation = nil; $0.disablesAnimations = true })
            b.host = a.host
            b.window = a.window
            await waitReady("matrix.b_pin", view: b.host, state: { b.readiness() }) {
                pins(b.host).contains { $0.conversationID == b.cid }
            }
        case "reveal":
            let active = navigated ? b : a
            active.palette.reveal = SearchReveal(conversationID: active.cid, itemID: "person:m000119")
        case "shrink":
            if let scroll = timelineScroll(a.host), let document = scroll.documentView {
                let height = max(800, document.frame.height - 2000)
                document.setFrameSize(NSSize(width: document.frame.width, height: height))
                scroll.contentView.scroll(to: scroll.contentView.constrainBoundsRect(scroll.contentView.bounds).origin)
                scroll.reflectScrolledClipView(scroll.contentView)
            }
        default: preconditionFailure(action)
        }
        operations.append(["action": action, "current": navigated ? b.cid : a.cid,
                           "position": sample(a.host)])
    }
    let newPin = pins(b.host).first { $0.conversationID == b.cid }!
    await waitReady("matrix.b_reveal_consumed", view: b.host, state: { b.readiness() }) {
        b.palette.reveal == nil
    }
    await stablePosition(b.host)
    b.daemon.resumeCatchup()
    await b.waitFolded(b.daemon.lastSeq, "matrix.b_caught_up")
    await stablePosition(b.host)
    return ["order": order, "operations": operations, "a_suspended": aSuspended,
            "pin_replaced": oldPin !== newPin, "b_trace": bTrace,
            "b_caught_up": b.caughtUp, "b_position": sample(b.host), "a_trace": aTrace]
}
