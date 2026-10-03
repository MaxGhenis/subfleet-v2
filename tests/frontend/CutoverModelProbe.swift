// Real UIModel actions, isolated from the complete SwiftUI view compilation.
import AppKit

@main struct CutoverModelProbe {
    @MainActor static func main() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let result = try await runCutover(CommandLine.arguments[1])
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
