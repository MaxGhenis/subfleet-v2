import Foundation

@main struct R4NullPresentationProbe {
    static func main() throws {
        let fixtures = try JSONValue.parse(Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))).array!
        var result: [String: Any] = [:]
        for fixture in fixtures {
            let request = fixture["request"]!
            var display = fixture["display"]!.object!
            if let input = request["input"] {
                // Older Claude summaries encode input as a JSON string.
                display["input"] = .string(String(data: try JSONEncoder().encode(input), encoding: .utf8)!)
            } else if let params = request["params"]?.object {
                display.merge(params) { _, value in value }
            }
            let card = ApprovalCard(approvalID: fixture["id"]!.string!, kind: fixture["kind"]!.string!,
                display: ApprovalDisplay(fields: display), options: ["allow", "deny"], state: .pending)
            func fields(_ value: JSONValue?) -> [String: String] {
                Dictionary(uniqueKeysWithValues: ApprovalPresentation.grantedFields(card, request: value).map { ($0.key, $0.value) })
            }
            result[fixture["id"]!.string!] = ["loaded": fields(request), "summary": fields(nil)]
        }
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
