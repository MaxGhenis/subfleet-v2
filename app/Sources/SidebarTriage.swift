// Subfleet: the sidebar's own marks — unread, pinned, unread first. They are the
// app's alone (the daemon keeps no per-reader state), saved under Application
// Support beside the drafts, 0600. Archive is the daemon's (`conversation.archive`).
// Foundation only.

import Foundation

/// What the person has seen and pinned. Unread is read from the watch feed: each
/// change row naming a message carries the feed's sequence (`changes.seq`), the one
/// sequence the app has for every conversation, focused or not (`conversation.events`
/// is read only for the focused one, C-29.9). A conversation is unread when a change
/// to one of its messages came after the newest one the person saw it at.
struct SidebarMarks: Codable, Equatable {
    /// The newest message change seen, per conversation id, while it was focused.
    var read: [String: Int] = [:]
    /// Changes up to here were there before the app kept marks, or before the
    /// daemon's feed started again from 1: all read. Nil until the first time the
    /// feed is caught up; until then nothing is unread.
    var floor: Int?
    /// Pinned conversation ids, the newest pin first.
    var pinned: [String] = []
    /// The sidebar lists unread conversations in a section of their own, first.
    var unreadFirst = false

    init() {}

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        read = try values.decodeIfPresent([String: Int].self, forKey: .read) ?? [:]
        floor = try values.decodeIfPresent(Int.self, forKey: .floor)
        pinned = try values.decodeIfPresent([String].self, forKey: .pinned) ?? []
        unreadFirst = try values.decodeIfPresent(Bool.self, forKey: .unreadFirst) ?? false
    }

    /// Whether a conversation whose newest message change is `newest` is unread.
    func isUnread(_ conversationID: String, newest: Int?) -> Bool {
        guard let floor, let newest else { return false }
        return newest > max(read[conversationID] ?? floor, floor)
    }

    /// The person saw the conversation with its messages as of change `seq`.
    mutating func markRead(_ conversationID: String, through seq: Int?) {
        guard let seq, seq > (read[conversationID] ?? Int.min) else { return }
        read[conversationID] = seq
    }

    /// The feed is caught up at `cursor`: the first time, everything so far is read.
    /// A cursor below the floor means the daemon's feed began again (its store was
    /// made anew), so the old marks name other changes: start over from here.
    mutating func caughtUp(at cursor: Int) {
        guard let floor else {
            self.floor = cursor
            return
        }
        if cursor < floor {
            self.floor = cursor
            read = [:]
        }
    }

    func isPinned(_ conversationID: String) -> Bool { pinned.contains(conversationID) }

    mutating func setPinned(_ conversationID: String, _ pin: Bool) {
        pinned.removeAll { $0 == conversationID }
        if pin { pinned.insert(conversationID, at: 0) }
    }

    // MARK: Saving

    static func load(from url: URL) -> SidebarMarks {
        guard let data = try? Data(contentsOf: url), let marks = try? JSONDecoder().decode(SidebarMarks.self, from: data)
        else { return SidebarMarks() }
        return marks
    }

    func save(to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try atomicWrite(try encoder.encode(self), to: url)
    }
}

extension AppPaths {
    var sidebarMarksURL: URL { support.appendingPathComponent("sidebar.json") }
}

/// The sections the sidebar lists, in order: Pinned (newest pin first), then, with
/// Unread first on, the unread conversations, then `grouped` (the date or workspace
/// groups) over the rest. Each entry appears once. The Archived view has neither
/// lead section: it lists archived conversations only.
func sidebarSections(_ entries: [SidebarEntry], marks: SidebarMarks, archivedView: Bool,
                     grouped: ([SidebarEntry]) -> [SidebarSection]) -> [SidebarSection] {
    guard !archivedView else { return grouped(entries) }
    var lead: [SidebarSection] = []
    let byID = Dictionary(entries.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
    let pinned = marks.pinned.compactMap { byID["cv:" + $0] }
    if !pinned.isEmpty { lead.append(SidebarSection(id: SidebarSection.pinnedID, title: "Pinned", entries: pinned)) }
    let shown = Set(pinned.map(\.id))
    var rest = entries.filter { !shown.contains($0.id) }
    if marks.unreadFirst {
        let unread = rest.filter(\.unread)
        if !unread.isEmpty {
            lead.append(SidebarSection(id: SidebarSection.unreadID, title: "Unread", entries: unread))
            rest = rest.filter { !$0.unread }
        }
    }
    return lead + grouped(rest)
}

extension SidebarSection {
    static let pinnedID = "pinned"
    static let unreadID = "unread"

    /// Pinned and Unread lead the list; the date groups below them may be folded.
    var leads: Bool { id == SidebarSection.pinnedID || id == SidebarSection.unreadID }
}
