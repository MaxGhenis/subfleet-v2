#if !SUBFLEET_MODEL_TEST
import SwiftUI

struct TaskChipCard: View {
    @ObservedObject var model: UIModel
    let chip: TaskChip

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(chip.state == "dismissed" ? "Dismissed task" : "Suggested task", systemImage: "arrow.triangle.branch")
                .font(.caption).foregroundStyle(.secondary)
            Text(chip.title).font(.headline).textSelection(.enabled)
            Text(chip.tldr).font(.callout).textSelection(.enabled)
            Label(abbreviatedPath(chip.cwd), systemImage: "folder")
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            if chip.isPending {
                HStack {
                    Button("Start") { model.chooseChip(chip, start: true) }.buttonStyle(.borderedProminent)
                    Button("Dismiss") { model.chooseChip(chip, start: false) }
                }
                .disabled(model.chipActions[chip.chip_id]?.state == .queued || model.chipActions[chip.chip_id]?.state == .sending)
                if let action = model.chipActions[chip.chip_id] {
                    if let failure = action.failure {
                        Text(failure.message).font(.caption).foregroundStyle(.red)
                        if failure.retryable { Text("Will retry when the daemon is available.").font(.caption).foregroundStyle(.secondary) }
                    } else if action.isOpen {
                        ProgressView(action.kind == .chipStart ? "Starting…" : "Dismissing…").controlSize(.small)
                    }
                }
            } else if chip.state == "started", let child = chip.child_conversation_id {
                Button("Open session") { model.focus(child) }.buttonStyle(.link)
            } else if let reason = chip.dismissal_reason, !reason.isEmpty {
                Text(reason).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(.separator.opacity(0.4)))
        .accessibilityElement(children: .contain)
    }
}
#endif
