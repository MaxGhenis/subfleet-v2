// Subfleet: the app entry point — a normal window app with a Dock icon and
// menus (design §12), and the menu bar bolt with the quota panel and an
// always-present "Open Subfleet".

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

#if !SUBFLEET_VIEW_TEST
final class SubfleetAppDelegate: NSObject, NSApplicationDelegate {
    /// Set by the window scene once SwiftUI has an `openWindow` action.
    static var openMain: (() -> Void)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
    }

    /// A Dock click with no window open brings the main window back.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { SubfleetAppDelegate.openMain?() }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

@main
struct SubfleetApp: App {
    @NSApplicationDelegateAdaptor(SubfleetAppDelegate.self) private var delegate
    @StateObject private var store = QuotaStore()
    @StateObject private var model = UIModel()

    var body: some Scene {
        Window("Subfleet", id: "main") {
            MainWindow(model: model)
                .frame(minWidth: 860, minHeight: 560)
                .background(OpenMainRegistrar())
        }
        .defaultSize(width: 1180, height: 780)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New conversation") {
                    SubfleetAppDelegate.openMain?()
                    model.openNewDraft()
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
