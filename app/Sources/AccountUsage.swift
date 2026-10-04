import Foundation

struct AccountUsage: Equatable {
    let account: String
    let fiveHour: Double?
    let weekly: Double?
    var words: String {
        func percent(_ value: Double?) -> String { value.map { "\(Int($0.rounded()))%" } ?? "—" }
        return "\(account) · 5h \(percent(fiveHour)) · week \(percent(weekly))"
    }
    static func make(provider: String, served: Served, laneID: String?, snapshot: Snapshot?, now: Date = Date()) -> AccountUsage? {
        let lane = served.lane_id ?? laneID
        var account = served.account ?? lane
        var display: LaneDisplay?
        if let snapshot {
            if provider == "codex", let match = snapshot.codex.homes.first(where: {
                lane != nil ? $0.lane_id == lane : served.account != nil && $0.email == served.account
            }) {
                if let recorded = served.account, let current = match.email,
                   recorded.caseInsensitiveCompare(current) != .orderedSame {
                    account = recorded
                } else {
                    account = served.account ?? match.email ?? account ?? match.home
                    display = codexDisplay(match, snapshot: snapshot, now: now)
                }
            } else if provider == "claude", let match = snapshot.claude.accounts?.first(where: {
                lane != nil ? $0.lane_id == lane : served.account != nil && $0.email == served.account
            }) {
                if let recorded = served.account, recorded.caseInsensitiveCompare(match.email) != .orderedSame {
                    account = recorded
                } else {
                    account = served.account ?? match.email
                    display = claudeDisplay(match, snapshot: snapshot, now: now)
                }
            }
        }
        guard let account else { return nil }
        return AccountUsage(account: account, fiveHour: display?.stale == false ? display?.percentage : nil,
                            weekly: display?.stale == false ? display?.weeklyPercentage : nil)
    }
}

#if !SUBFLEET_MODEL_TEST
import SwiftUI

struct AccountUsageChip: View {
    @ObservedObject var model: UIModel
    let conversation: Conversation
    var body: some View {
        let timeline = model.state.timelines[conversation.conversation_id]
        let served = timeline?.displayOrder.reversed().compactMap { timeline?.turn($0)?.served }
            .first(where: { !$0.fields.isEmpty }) ?? Served()
        if let usage = AccountUsage.make(provider: conversation.provider, served: served,
                                         laneID: conversation.lane_id, snapshot: model.accountSnapshot) {
            Text(usage.words).windowFont(.heading).foregroundStyle(Theme.text.secondary.color)
                .lineLimit(1).padding(.horizontal, Theme.space.inset).padding(.vertical, Theme.space.step)
                .background(RoundedRectangle(cornerRadius: Theme.radius.control).fill(Theme.surface.raised.color))
                .help("Serving account and its five-hour and weekly usage. A dash means usage is unavailable or stale.")
        }
    }
}
#endif
