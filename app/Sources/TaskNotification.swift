// Claude's background-task transcript rows are system notices. This is a
// display projection only: the native history and provider input stay intact.
import Foundation

struct TaskNotification: Equatable {
    var summary: String
    var status: String
    var exitCode: Int?

    var detail: String {
        let state = status.prefix(1).uppercased() + status.dropFirst()
        return state + (exitCode.map { " · Exit code \($0)" } ?? "")
    }

    /// Recognize only a whole user turn. Claude writes command text inside the
    /// fields without consistently escaping XML (`&&`, for example), so read
    /// its named fields instead of rejecting a useful notice as malformed XML.
    static func parse(_ text: String) -> TaskNotification? {
        guard let body = capture("\\A\\s*<task-notification>([\\s\\S]*?)</task-notification>\\s*\\z", in: text),
              !body.contains("<task-notification>"),
              let summary = field("summary", in: body), !summary.isEmpty,
              let status = field("status", in: body), !status.isEmpty else { return nil }
        let explicitCode = field("exit-code", in: body) ?? field("exit_code", in: body)
        let summaryCode = capture("(?i)\\bexit code\\s+(-?[0-9]+)\\b", in: summary)
        return TaskNotification(summary: summary, status: status, exitCode: (explicitCode ?? summaryCode).flatMap(Int.init))
    }

    private static func field(_ name: String, in text: String) -> String? {
        capture("<\(name)>([\\s\\S]*?)</\(name)>", in: text).map {
            unescaped($0).trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    private static func capture(_ pattern: String, in text: String) -> String? {
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let range = Range(match.range(at: 1), in: text) else { return nil }
        return String(text[range])
    }

    private static func unescaped(_ text: String) -> String {
        var value = text
        for (entity, literal) in [("&lt;", "<"), ("&gt;", ">"), ("&quot;", "\""), ("&apos;", "'"), ("&amp;", "&")] {
            value = value.replacingOccurrences(of: entity, with: literal)
        }
        return value
    }
}
