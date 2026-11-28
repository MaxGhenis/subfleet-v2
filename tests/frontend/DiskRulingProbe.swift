// Foundation-only: the app's actual wire and menu status decoders.
import Foundation

@main
struct DiskRulingProbe {
    static func main() throws {
        let arguments = CommandLine.arguments
        let data = try Data(contentsOf: URL(fileURLWithPath: arguments[1]))
        if arguments[2] == "snapshot" {
            _ = try JSONDecoder().decode(Snapshot.self, from: data)
        } else {
            let _: JSONValue = try DaemonClient.decodeResponse(data, id: "review", op: arguments[2])
        }
        print("decoded")
    }
}
