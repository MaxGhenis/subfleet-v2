// Round-three review probe: what the grant list shows for a Codex 0.159 writeStdin approval.
import Foundation

@main struct KindProbe {
    static func main() throws {
        let scenes = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).object!
        let scene = scenes["r3-codex-write-stdin"]!
        let card = ApprovalCard(approvalID: "a", kind: "command", display: ApprovalDisplay(fields: scene["display"]!.object!),
                                options: ["allow", "deny"], state: .pending)
        for (label, request) in [("loaded", scene["request"]), ("summary", nil)] as [(String, JSONValue?)] {
            let fields = ApprovalPresentation.grantedFields(card, request: request).map { "\($0.key): \($0.value)" }
            print(label, fields.filter { $0.lowercased().contains("kind") || $0.contains("Stdin") })
        }
        print("headline", ApprovalPresentation.headline(card))
    }
}
