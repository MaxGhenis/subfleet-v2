// Subfleet: the app entry point.

#if !SUBFLEET_MODEL_TEST
import SwiftUI

#if !SUBFLEET_VIEW_TEST
@main
struct SubfleetApp: App {
    @StateObject private var store = QuotaStore()
    var body: some Scene {
        MenuBarExtra {
            ContentView(store: store)
        } label: {
            HStack(spacing: 2) {
                Image(systemName: store.hasProblem ? "bolt.trianglebadge.exclamationmark" : "bolt.fill")
                Text(store.barLabel).font(.system(.body, design: .monospaced))
            }
        }.menuBarExtraStyle(.window)
    }
}
#endif
#endif
