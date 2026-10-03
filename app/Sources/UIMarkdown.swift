// Subfleet: Markdown blocks (Markdown.swift parses them) drawn with SwiftUI, in
// reading sizes at the window's text scale (C-29.13).

#if !SUBFLEET_MODEL_TEST
import AppKit
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
                Text("\(parsed.hidden) more blocks not shown").readingFont(.caption).foregroundStyle(.secondary)
            }
        }
        // Paragraphs, list items and table cells take the body size from here.
        .readingFont(.body)
        .textSelection(.enabled)
    }
}

struct MarkdownBlockView: View {
    let block: MarkdownBlock

    var body: some View {
        switch block {
        case .heading(let level, let content):
            Text(Markdown.attributed(content))
                .readingFont(level <= 1 ? .title : level == 2 ? .heading : .subheading)
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
            CodeBlockView(language: language, text: text)
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

/// A code block: its language, a Copy button for all of it, and past the
/// first `MarkdownBounds.codeLines` lines a control that shows more, a step at a
/// time so a huge block stays bounded (C-29.8).
struct CodeBlockView: View {
    let language: String?
    let text: String
    @State private var limit = MarkdownBounds.codeLines
    @State private var copied = false

    var body: some View {
        let shown = MarkdownBounds.code(text, maxLines: limit)
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                if let language, !language.isEmpty {
                    Text(language).readingFont(.footnote).foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: copy) {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                }
                .buttonStyle(.borderless).readingFont(.footnote).foregroundStyle(.secondary)
                .help("Copy the whole block")
            }
            .padding(.horizontal, 8).padding(.top, 5)
            ScrollView(.horizontal, showsIndicators: true) {
                Text(shown.shown).readingFont(.code, design: .monospaced).fixedSize()
                    .padding(.horizontal, 8).padding(.vertical, 6)
            }
            if shown.hiddenLines > 0 {
                Button(moreWords(hidden: shown.hiddenLines)) {
                    limit = MarkdownBounds.expandedCodeLines(from: limit, total: limit + shown.hiddenLines)
                }
                .buttonStyle(.link).readingFont(.footnote)
                .padding(.horizontal, 8).padding(.bottom, 6)
            }
        }
        .background(Color(nsColor: .textBackgroundColor).opacity(0.6))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.25)))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private func moreWords(hidden: Int) -> String {
        hidden <= MarkdownBounds.codeExpansion ? "Show \(hidden) more line\(hidden == 1 ? "" : "s")"
            : "Show \(MarkdownBounds.codeExpansion) more lines (\(hidden) hidden)"
    }

    private func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        copied = true
        Task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            copied = false
        }
    }
}
#endif
