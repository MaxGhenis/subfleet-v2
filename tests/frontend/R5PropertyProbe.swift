import Foundation

// Loaded-request grant rows, in display order, for each generated scene.
@main struct R5PropertyProbe {
    static func main() throws {
        let scenes = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).array!
        var result: [[String: Any]] = []
        for scene in scenes {
            let card = ApprovalCard(approvalID: "p", kind: scene["kind"]!.string!,
                                    display: ApprovalDisplay(fields: scene["display"]!.object!),
                                    options: ["allow", "deny"], state: .pending)
            let request = scene["request"]!
            result.append([
                "rows": ApprovalPresentation.grantedFields(card, request: request).map { [$0.key, $0.value] },
                "summary": ApprovalPresentation.grantedFields(card, request: nil).map { [$0.key, $0.value] },
            ])
        }
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
