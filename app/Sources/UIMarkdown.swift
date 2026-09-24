// Subfleet: Markdown blocks (Markdown.swift parses them) drawn with SwiftUI.

#if !SUBFLEET_MODEL_TEST
import SwiftUI

struct MarkdownView: View {
    let text: String
    var streaming = false

    var body: some View {
        let parsed = MarkdownBounds.blocks(Markdown.parse(text))
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(parsed.shown.enumerated()), id: \.offset) { _, block in
                MarkdownBlockView(block: block)
            }
            if parsed.hidden > 0 {
                Text("\(parsed.hidden) more blocks not shown").font(.caption).foregroundStyle(.secondary)
            }
        }
        .textSelection(.enabled)
    }
}

struct MarkdownBlockView: View {
    let block: MarkdownBlock

    var body: some View {
        switch block {
        case .heading(let level, let content):
            Text(Markdown.attributed(content))
                .font(level <= 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline)
                .padding(.top, 4)
        case .paragraph(let content):
            Text(Markdown.attributed(content)).fixedSize(horizontal: false, vertical: true)
        case .list(let ordered, let start, _, let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if let checked = item.checked {
                            Image(systemName: checked ? "checkmark.square" : "square").foregroundStyle(.secondary)
                        } else {
                            Text(ordered ? "\(start + index)." : "•").foregroundStyle(.secondary)
                                .monospacedDigit()
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(Array(item.blocks.enumerated()), id: \.offset) { _, child in
                                MarkdownBlockView(block: child)
                            }
                        }
                    }
                }
            }
        case .code(let language, let text, _):
            let shown = MarkdownBounds.code(text)
            VStack(alignment: .leading, spacing: 2) {
                if let language, !language.isEmpty {
                    Text(language).font(.caption2).foregroundStyle(.secondary)
                }
                ScrollView(.horizontal, showsIndicators: true) {
                    Text(shown.shown).font(.system(.callout, design: .monospaced)).fixedSize()
                        .padding(8)
                }
                if shown.hiddenLines > 0 {
                    Text("\(shown.hiddenLines) more lines").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .background(Color(nsColor: .textBackgroundColor).opacity(0.6))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25)))
            .clipShape(RoundedRectangle(cornerRadius: 6))
        case .quote(let blocks):
            HStack(alignment: .top, spacing: 8) {
                Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 3)
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(blocks.enumerated()), id: \.offset) { _, child in
                        MarkdownBlockView(block: child)
                    }
                }.foregroundStyle(.secondary)
            }
        case .table(let header, _, let rows):
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                        Text(Markdown.attributed(cell)).bold()
                    }
                }
                Divider()
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(Markdown.attributed(cell))
                        }
                    }
                }
            }
            .padding(6)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25)))
        case .rule:
            Divider()
        }
    }
}
#endif
