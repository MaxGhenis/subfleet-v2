import Foundation

// Grant rows for real adapter summaries: `summary` is what a history card (and a
// pending card before approval.get) renders; `loaded` is the card/sheet after load.
@main struct R5SummaryProbe {
    static func main() throws {
        let scenes = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).object!
        var result: [String: Any] = [:]
        for id in scenes.keys.sorted() {
            let scene = scenes[id]!
            let card = ApprovalCard(approvalID: id, kind: scene["kind"]!.string!,
                                    display: ApprovalDisplay(fields: scene["display"]!.object!),
                                    options: scene["options"]!.array!.compactMap(\.string), state: .answered("allow"))
            func rows(_ request: JSONValue?) -> [String] {
                ApprovalPresentation.grantedFields(card, request: request).map { "\($0.key): \($0.value)" }
            }
            result[id] = ["summary": rows(nil), "loaded": rows(scene["request"]!)]
        }
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted]),
                     encoding: .utf8)!)
    }
}
