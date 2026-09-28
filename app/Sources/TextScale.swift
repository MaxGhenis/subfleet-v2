// Subfleet: how large conversation text is (C-29.13), as plain Swift.
//
// macOS's SwiftUI text styles are fixed points that ignore Dynamic Type (a
// `.caption` is 10 pt at every `dynamicTypeSize`), so the window sets its text
// in explicit reading sizes: each `ReadingStyle` has a size at actual size,
// larger than the system's (body 16 pt, Claude Code's Large, where the system
// body is 13), times one scale the person picks with View > Bigger, Smaller
// and Actual size. Bigger and Smaller step the way Claude Code's ⌘+ and ⌘−
// zoom does, half a zoom level (×1.2^½) at a time, so the same presses give the
// same size: Max reads Claude Code five steps up (158 %). The scale persists in
// UserDefaults under `TextScale.defaultsKey`, and the conversation column
// widens with it. Foundation only.

import Foundation

enum TextScale {
    /// Where the scale persists (the app's `@AppStorage` reads the same key).
    static let defaultsKey = "conversationTextScale"
    /// Actual size: every reading style at its base size.
    static let actual = 1.0
    /// What Bigger and Smaller step through, smallest first: 1.2^(k/2) for k
    /// from −2 to 10, 83 % to 249 %.
    static let steps: [Double] = (-2...10).map { pow(1.2, Double($0) / 2) }
    static var range: ClosedRange<Double> { steps[0]...steps[steps.count - 1] }

    /// A usable scale: one outside the range is its nearest bound, and one that
    /// is not a number is actual size.
    static func clamp(_ value: Double) -> Double {
        guard value.isFinite else { return actual }
        return min(max(value, range.lowerBound), range.upperBound)
    }

    /// The next step above `value`, or the largest.
    static func bigger(_ value: Double) -> Double {
        let current = clamp(value)
        return steps.first { $0 > current + 0.001 } ?? range.upperBound
    }

    /// The next step below `value`, or the smallest.
    static func smaller(_ value: Double) -> Double {
        let current = clamp(value)
        return steps.last { $0 < current - 0.001 } ?? range.lowerBound
    }

    static func canEnlarge(_ value: Double) -> Bool { clamp(value) < range.upperBound - 0.001 }
    static func canReduce(_ value: Double) -> Bool { clamp(value) > range.lowerBound + 0.001 }
    static func isActual(_ value: Double) -> Bool { abs(clamp(value) - actual) < 0.001 }

    /// The persisted scale: actual size when none is stored or what is stored is
    /// not a number.
    static func load(from defaults: UserDefaults) -> Double {
        guard let number = defaults.object(forKey: defaultsKey) as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return actual }
        return clamp(number.doubleValue)
    }

    static func save(_ value: Double, to defaults: UserDefaults) {
        defaults.set(clamp(value), forKey: defaultsKey)
    }
}

/// The window's text sizes. Body is the conversation's own words (messages,
/// Markdown, the composer, sidebar titles); secondary is supporting prose
/// (thoughts, errors, banners); caption and footnote are metadata (status
/// lines, paths, chips, timestamps), kept smaller than body but never below
/// `minimumPointSize`.
enum ReadingStyle: String, CaseIterable {
    case title, heading, subheading, body, secondary, code, caption, footnote

    enum Weight: String { case regular, medium, semibold, bold }

    /// Points at actual size. The system's are 17, 15, 13, 13, 12, 12, 10, 10.
    var basePointSize: Double {
        switch self {
        case .title: return 24
        case .heading: return 20
        case .subheading: return 17
        case .body: return 16
        case .secondary: return 15
        case .code: return 14.5
        case .caption: return 13
        case .footnote: return 12
        }
    }

    var weight: Weight {
        switch self {
        case .title, .heading: return .bold
        case .subheading: return .semibold
        default: return .regular
        }
    }

    /// No reading size is smaller than this, at any scale.
    static let minimumPointSize = 10.0

    /// The size at `scale` (clamped), to the half point.
    func pointSize(scale: Double) -> Double {
        let scaled = (basePointSize * TextScale.clamp(scale) * 2).rounded() / 2
        return max(ReadingStyle.minimumPointSize, scaled)
    }

    /// The conversation column's widest, in points: about 75 characters of body
    /// text a line at every scale (896 pt at actual size, where it was a fixed 900).
    static func columnWidth(scale: Double) -> Double { ReadingStyle.body.pointSize(scale: scale) * 56 }

    /// A person's message bubble's widest.
    static func bubbleWidth(scale: Double) -> Double { ReadingStyle.body.pointSize(scale: scale) * 40 }
}
