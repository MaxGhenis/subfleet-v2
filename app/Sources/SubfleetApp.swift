// Subfleet: the app entry point — a normal window app with a Dock icon and
// menus (design §12), and the menu bar bolt with the quota panel and an
// always-present "Open Subfleet".

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI
import UserNotifications

#if !SUBFLEET_VIEW_TEST
@MainActor
final class SubfleetAppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    /// Set by the window scene once SwiftUI has an `openWindow` action.
    static var openMain: (() -> Void)?
    /// The app's one model: the windows bind to it, and a notification's click
    /// reaches it here, the window open or not.
    let model = UIModel()

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Before launch ends, as the system requires for the click that
        // launched the app to be delivered here.
        UNUserNotificationCenter.current().delegate = self
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }

    /// A Dock click with no window open brings the main window back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { SubfleetAppDelegate.openMain?() }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: Notifications (C-29.9)

    /// A click opens the main window on the notification's conversation, and an
    /// approval's at that conversation's oldest waiting card. A dismissal, or
    /// any other response, opens nothing.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let request = response.notification.request
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier,
              let target = NotificationTarget(requestID: request.identifier, userInfo: request.content.userInfo)
        else { return }
        await open(target)
    }

    /// One that arrives while the app is frontmost shows as it would in the
    /// background (without this the system shows nothing), unless its
    /// conversation is on screen by now: the focused one, with the main window
    /// visible and holding the key window (D-24).
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        let request = notification.request
        let target = NotificationTarget(requestID: request.identifier, userInfo: request.content.userInfo)
        return await showsWhileFrontmost(target) ? [.banner, .list] : []
    }

    private func open(_ target: NotificationTarget) {
        model.show(target)
        if let openMain = SubfleetAppDelegate.openMain {
            openMain()
        } else {
            // The click that launched the app, before the window first appeared:
            // the window, the app's first scene, presents itself at launch
            // (SwiftUI's automatic launch behavior), and `connect` then opens
            // the held click in it.
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func showsWhileFrontmost(_ target: NotificationTarget?) -> Bool {
        target?.showsWhileFrontmost(onScreenConversationID: model.onScreenConversationID()) ?? true
    }
}

@main
struct SubfleetApp: App {
    @NSApplicationDelegateAdaptor(SubfleetAppDelegate.self) private var delegate
    @StateObject private var store = QuotaStore()

    var body: some Scene {
        Window("Subfleet", id: "main") {
            MainWindow(model: delegate.model)
                .frame(minWidth: 860, minHeight: 560)
                .background(OpenMainRegistrar())
        }
        .defaultSize(width: 1180, height: 780)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New conversation") {
                    NotificationCenter.default.post(name: .subfleetNewConversation, object: nil)
                }.keyboardShortcut("n")
            }
        }

        MenuBarExtra {
            VStack(alignment: .leading, spacing: 0) {
                OpenSubfleetButton().padding([.horizontal, .top], 12)
                ContentView(store: store)
            }
        } label: {
            HStack(spacing: 2) {
                Image(systemName: store.hasProblem ? "bolt.trianglebadge.exclamationmark" : "bolt.fill")
                Text(store.barLabel).font(.system(.body, design: .monospaced))
            }
        }
        .menuBarExtraStyle(.window)
    }
}

/// Keeps a working `openWindow` for the Dock-click and menu paths.
private struct OpenMainRegistrar: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Color.clear.onAppear {
            let open = openWindow
            SubfleetAppDelegate.openMain = {
                open(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
    }
}

private struct OpenSubfleetButton: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Button {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        } label: {
            Label("Open Subfleet", systemImage: "macwindow")
        }
        .keyboardShortcut("o")
    }
}
#endif
#endif
