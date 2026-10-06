// Round-three review probe: grant-list order for a 12-step Codex chain and 12 write roots.
import Foundation

@main struct OrderProbe {
    static func main() throws {
        let scenes = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).object!
        for id in ["r3-codex-12-actions", "r3-codex-permissions-12"] {
            let scene = scenes[id]!
            let card = ApprovalCard(approvalID: "a", kind: scene["kind"]!.string!, display: ApprovalDisplay(fields: scene["display"]!.object!),
                                    options: ["allow", "deny"], state: .pending)
            let keys = ApprovalPresentation.grantedFields(card, request: scene["request"]).map(\.key)
            print(id, "rows", keys.count, "first 14:", Array(keys.prefix(14)))
            if let i = keys.firstIndex(where: { $0 == "params.command" }) { print("   params.command at row", i) }
        }
    }
}
