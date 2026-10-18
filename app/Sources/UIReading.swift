// Subfleet: conversation text in reading sizes at one scale (C-29.13).
//
// Views set text with `.readingFont(_:)` instead of the system's text styles;
// the window puts the person's scale in the environment, and View > Bigger,
// Smaller and Actual size change it (TextScale.swift has the sizes and steps).

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

private struct TextScaleKey: EnvironmentKey {
    static let defaultValue = TextScale.actual
}

extension EnvironmentValues {
    /// The person's text scale, always within `TextScale.range`.
    var textScale: Double {
        get { self[TextScaleKey.self] }
        set { self[TextScaleKey.self] = TextScale.clamp(newValue) }
    }
}

extension ReadingStyle.Weight {
    var fontWeight: Font.Weight {
        switch self {
        case .regular: return .regular
        case .medium: return .medium
        case .semibold: return .semibold
        case .bold: return .bold
        }
    }

    var nsFontWeight: NSFont.Weight {
        switch self {
        case .regular: return .regular
        case .medium: return .medium
        case .semibold: return .semibold
        case .bold: return .bold
        }
    }
}

extension ReadingStyle {
    func font(scale: Double, weight: Font.Weight? = nil, design: Font.Design = .default) -> Font {
        .system(size: pointSize(scale: scale), weight: weight ?? self.weight.fontWeight, design: design)
    }

    /// For AppKit text (the composer, the palette's field).
    func nsFont(scale: Double) -> NSFont {
        .systemFont(ofSize: pointSize(scale: scale), weight: weight.nsFontWeight)
    }
}

private struct ReadingFont: ViewModifier {
    @Environment(\.textScale) private var scale
    let style: ReadingStyle
    let weight: Font.Weight?
    let design: Font.Design

    func body(content: Content) -> some View {
        content.font(style.font(scale: scale, weight: weight, design: design))
    }
}

private struct ReadingColumn: ViewModifier {
    @Environment(\.textScale) private var scale

    func body(content: Content) -> some View {
        content.frame(maxWidth: ReadingStyle.columnWidth(scale: scale), alignment: .leading)
    }
}

extension View {
    /// Text in a reading size at the window's text scale.
    func readingFont(_ style: ReadingStyle, weight: Font.Weight? = nil, design: Font.Design = .default) -> some View {
        modifier(ReadingFont(style: style, weight: weight, design: design))
    }

    /// The conversation column, whose widest grows with the text.
    func readingColumn() -> some View { modifier(ReadingColumn()) }
}

/// View > Bigger (⌘+), Smaller (⌘−) and Actual size (⌘0).
struct TextSizeCommands: Commands {
    @Binding var scale: Double

    var body: some Commands {
        CommandGroup(after: .toolbar) {
            Divider()
            Button("Bigger") { scale = TextScale.bigger(scale) }
                .keyboardShortcut("+", modifiers: .command)
                .disabled(!TextScale.canEnlarge(scale))
            Button("Smaller") { scale = TextScale.smaller(scale) }
                .keyboardShortcut("-", modifiers: .command)
                .disabled(!TextScale.canReduce(scale))
            Button("Actual size") { scale = TextScale.actual }
                .keyboardShortcut("0", modifiers: .command)
                .disabled(TextScale.isActual(scale))
        }
    }
}

/// ⌘= makes text bigger too: ⌘+ takes Shift on most layouts, and browsers and
/// editors take both. A menu item has one shortcut, so this one is a button
/// in the window that shows nothing.
struct TextScaleEqualsShortcut: View {
    @Binding var scale: Double

    var body: some View {
        Button("Bigger") { scale = TextScale.bigger(scale) }
            .keyboardShortcut("=", modifiers: .command)
            .disabled(!TextScale.canEnlarge(scale))
            .opacity(0)
            .frame(width: 0, height: 0)
            .accessibilityHidden(true)
    }
}
#endif
