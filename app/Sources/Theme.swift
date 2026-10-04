// All window colors, radii and spacing. RGB values also feed contrast tests.
import Foundation

enum Theme {
    struct Pair {
        let dark: UInt32
        let light: UInt32
    }
    enum surface {
        static let conversation = Pair(dark: 0x161615, light: 0xFFFFFF)
        static let sidebar = Pair(dark: 0x111110, light: 0xF5F4F1)
        static let raised = Pair(dark: 0x222221, light: 0xF0EFEC)
        static let selected = Pair(dark: 0x333332, light: 0xE6E5E1)
        static let hover = Pair(dark: 0x1E1E1D, light: 0xECEBE8)
        static let all = [conversation, sidebar, raised, selected, hover]
    }
    enum text {
        static let primary = Pair(dark: 0xECEBE8, light: 0x1A1A19)
        static let secondary = Pair(dark: 0xA3A29E, light: 0x5E5D59)
        // The spec's #73726E / #8C8B86 miss 3:1 on selected rows.
        static let tertiary = Pair(dark: 0x8B8A86, light: 0x777670)
    }
    enum radius {
        static let control: CGFloat = 8
        static let card: CGFloat = 12
        static let container: CGFloat = 16
    }
    enum space {
        static let step: CGFloat = 4
        static let inset: CGFloat = step * 2
        static let column: CGFloat = step * 6
        static let reply: CGFloat = step * 5
        static let paragraph: CGFloat = 10
        static let row: CGFloat = step * 8
    }
    static func luminance(_ rgb: UInt32) -> Double {
        func linear(_ channel: UInt32) -> Double {
            let c = Double(channel) / 255
            return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * linear((rgb >> 16) & 255) + 0.7152 * linear((rgb >> 8) & 255) + 0.0722 * linear(rgb & 255)
    }
    static func contrast(_ a: UInt32, _ b: UInt32) -> Double {
        let x = luminance(a), y = luminance(b)
        return (max(x, y) + 0.05) / (min(x, y) + 0.05)
    }
}

#if !SUBFLEET_MODEL_TEST
import AppKit
import SwiftUI

extension Theme.Pair {
    var nsColor: NSColor {
        NSColor(name: nil) { appearance in
            let rgb = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: Double((rgb >> 16) & 255) / 255,
                           green: Double((rgb >> 8) & 255) / 255, blue: Double(rgb & 255) / 255, alpha: 1)
        }
    }
    var color: Color { Color(nsColor: nsColor) }
}

extension Theme {
    enum line {
        static let hairline = Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                ? NSColor.white.withAlphaComponent(0.09) : NSColor.black.withAlphaComponent(0.09)
        })
    }
    enum state {
        static let attention = Color(nsColor: .systemOrange)
        static let error = Color(nsColor: .systemRed)
        static let success = Color(nsColor: .systemGreen)
        static let added = success.opacity(0.12)
        static let removed = error.opacity(0.12)
        static let changed = Color(nsColor: .systemBlue).opacity(0.07)
        static let search = attention.opacity(0.25)
    }
    static let accent = Color.accentColor
    static let accentNS = NSColor.controlAccentColor
    static let templateInk = NSColor.black
    static let clear = Color.clear
    static let onAccent = Color.white
    static let scrim = Color.black.opacity(0.18)
    static let shadow = Color.black.opacity(0.25)
}

/// Sizes specific to navigation and controls, all following the text scale.
enum WindowType: Double {
    case sidebar = 14, heading = 12, title = 15, control = 13
}
private struct WindowFont: ViewModifier {
    @Environment(\.textScale) private var scale
    let type: WindowType
    func body(content: Content) -> some View {
        content.font(.system(size: max(10, type.rawValue * scale), weight: .regular))
    }
}
extension View {
    func windowFont(_ type: WindowType) -> some View { modifier(WindowFont(type: type)) }
}

/// Quiet controls still show their keyboard focus and pointer hover.
struct QuietButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        QuietButtonLabel(configuration: configuration)
    }
    private struct QuietButtonLabel: View {
        let configuration: ButtonStyle.Configuration
        @Environment(\.isFocused) private var focused
        @State private var hovered = false
        var body: some View {
            configuration.label
                .background(RoundedRectangle(cornerRadius: Theme.radius.control)
                    .fill(hovered || configuration.isPressed ? Theme.surface.hover.color : Theme.clear))
                .overlay(RoundedRectangle(cornerRadius: Theme.radius.control)
                    .stroke(focused ? Theme.accent : Theme.clear, lineWidth: 2))
                .onHover { hovered = $0 }
        }
    }
}

/// Notices share one neutral container; the symbol and words carry the state.
struct NoticeRow<Content: View>: View {
    let symbol: String
    @ViewBuilder var content: Content
    var body: some View {
        HStack(alignment: .top, spacing: Theme.space.inset) {
            Image(systemName: symbol).foregroundStyle(Theme.text.secondary.color)
            content.frame(maxWidth: .infinity, alignment: .leading)
        }
        .readingFont(.secondary)
        .foregroundStyle(Theme.text.primary.color)
        .padding(Theme.space.inset * 1.5)
        .background(RoundedRectangle(cornerRadius: Theme.radius.card).fill(Theme.surface.raised.color))
    }
}
#endif
