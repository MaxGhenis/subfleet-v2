// Foundation-only probe of the same model used by the menu app. Never starts AppKit.
import Foundation

@main
struct StatusModelProbe {
    static func main() throws {
        let arguments = CommandLine.arguments
        if arguments[1] == "path" {
            let environment = arguments.count > 3 ? ["SUBFLEET_HOME": arguments[3]] : [:]
            print(statusFileURL(environment: environment, home: URL(fileURLWithPath: arguments[2])).path)
            return
        }
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(contentsOf: URL(fileURLWithPath: arguments[1])))
        let now = Date(timeIntervalSince1970: Double(arguments[2])!)
        func project(_ display: LaneDisplay) -> [String: Any] {
            ["percentage": display.percentage as Any? ?? NSNull(),
             "weekly_percentage": display.weeklyPercentage as Any? ?? NSNull(),
             "five_hour_reset": display.fiveHourReset?.timeIntervalSince1970 as Any? ?? NSNull(),
             "weekly_reset": display.weeklyReset?.timeIntervalSince1970 as Any? ?? NSNull(),
             "status": display.status, "detail": display.detail, "stale": display.stale,
             "tone": String(describing: display.tone)]
        }
        func show(_ job: JobRow) -> [String: Any] {
            let display = jobDisplay(job)
            return ["title": display.title, "detail": display.detail, "status": display.status,
                    "tone": String(describing: display.tone)]
        }
        let result: [String: Any] = [
            "has_jobs_section": snapshot.jobs != nil,
            "job_groups": jobGroups(snapshot.jobs?.live ?? []).map {
                ["title": $0.title as Any? ?? NSNull(), "jobs": $0.jobs.map(show)] as [String: Any]
            },
            "recent_jobs": (snapshot.jobs?.recent ?? []).map(show),
            "stale": snapshot.isStale(now: now),
            "codex": snapshot.codex.homes.map { project(codexDisplay($0, snapshot: snapshot, now: now)) },
            "claude": (snapshot.claude.accounts ?? []).map { project(claudeDisplay($0, snapshot: snapshot, now: now)) }
        ]
        print(String(data: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
