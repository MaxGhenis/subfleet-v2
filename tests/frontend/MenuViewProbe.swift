// Exercise the real SwiftUI view without opening a window or running an app.
import AppKit
import SwiftUI

@main
struct MenuViewProbe {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let arguments = CommandLine.arguments
        if arguments[1] == "diff-rows" {
            // The Changes pane's row width, and a row honouring it.
            let line = UnifiedDiff.parse("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
                .flatMap(\.lines).first { $0.kind == .added }!
            let row = NSHostingController(rootView: DiffLineRow(line: line, minWidth: 480))
            let result: [String: Any] = [
                "overlay": DiffLinesView.rowWidth(pane: 500, scrollerStyle: .overlay),
                "legacy": DiffLinesView.rowWidth(pane: 500, scrollerStyle: .legacy),
                "scroller": NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy),
                "narrow": DiffLinesView.rowWidth(pane: 4, scrollerStyle: .legacy),
                "row_width": row.sizeThatFits(in: .zero).width,
                "visible_windows": NSApp.windows.filter(\.isVisible).count]
            print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
            return
        }
        let url = URL(fileURLWithPath: arguments[1])
        let store = QuotaStore(url: url, automaticallyReload: false)
        let controller = NSHostingController(rootView: ContentView(store: store))
        let minimum = controller.sizeThatFits(in: .zero)
        let proposed = controller.sizeThatFits(in: CGSize(width: 430, height: 800))
        var result: [String: Any] = [
            "minimum_width": minimum.width, "minimum_height": minimum.height,
            "proposed_height": proposed.height,
            "lanes": (store.snap?.codex.homes.count ?? 0) + (store.snap?.claude.accounts?.count ?? 0),
            "has_snapshot": store.snap != nil,
            // Inspect the actual view's selected/grouped rows, not the raw
            // snapshot (which missed its former extra five-result truncation).
            "recent_groups": JobsView(jobs: store.snap?.jobs).recentGroups.map {
                ["title": $0.title as Any? ?? NSNull(), "job_ids": $0.jobs.map(\.job_id)] as [String: Any]
            },
            "initial_feedback": store.reloadMessage as Any? ?? NSNull()
        ]
        if arguments.count > 2 {
            // A manual reload acknowledges an unchanged file without pretending
            // that the provider's snapshot timestamp has advanced.
            let originalTime = store.snap?.generated_at
            store.reload()
            result["unchanged_feedback"] = store.reloadMessage
            result["unchanged_generation"] = store.snap?.generated_at == originalTime
            result["reload_minimum_height"] = controller.sizeThatFits(in: .zero).height
            let replacement = try Data(contentsOf: URL(fileURLWithPath: arguments[2]))
            try replacement.write(to: url, options: .atomic)
            store.reload()
            result["new_generation"] = store.snap?.generated_at
            result["new_lane_count"] = store.snap?.codex.fleet.total_homes
            try Data("broken json".utf8).write(to: url, options: .atomic)
            store.reload()
            result["failed_feedback"] = store.reloadMessage
            result["failed_read_error"] = store.readError
            result["failed_snapshot_cleared"] = store.snap == nil
        }
        // A layout regression test must never create visible test windows.
        result["visible_windows"] = NSApp.windows.filter(\.isVisible).count
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
                     encoding: .utf8)!)
    }
}
