#if !SUBFLEET_MODEL_TEST
import SwiftUI

func modelDisplayName(_ value: String, models: [ModelEntry] = []) -> String {
    if let entry = models.first(where: { $0.id == value || $0.value == value || $0.values.contains(value) }) {
        return entry.short.prefix(1).uppercased() + entry.short.dropFirst()
    }
    if value.hasPrefix("claude-") {
        return value.dropFirst(7).replacingOccurrences(of: "-5-5", with: " 5.5")
            .replacingOccurrences(of: "-", with: " ").capitalized
    }
    return value.replacingOccurrences(of: "gpt-", with: "GPT-")
}

struct ModelEffortControl: View {
    let provider: String
    @Binding var settings: ConversationSettings
    let options: ComposerOptions
    private var name: String {
        options.models.first(where: { $0.value == settings.model })?.label
            ?? modelDisplayName(settings.model, models: options.models.map(\.model))
    }
    var body: some View {
        HStack(spacing: Theme.space.step) {
        ProviderMark(provider: provider)
        Menu {
            Picker("Model", selection: Binding(get: { settings.model }, set: { value in
                settings.model = value
                let entry = options.models.first { $0.value == value }?.model
                if let effort = settings.effort, let efforts = entry?.efforts, !efforts.contains(effort) { settings.effort = nil }
                if entry?.fast.supported == false { settings.fast = false }
            })) {
                ForEach(options.models) { choice in Text(choice.label).tag(choice.value) }
                if !options.models.contains(where: { $0.value == settings.model }) {
                    Text(settings.model).tag(settings.model)
                }
            }
            Picker("Effort", selection: Binding(get: { settings.effort ?? "" }, set: {
                settings.effort = $0.isEmpty ? nil : $0
            })) {
                Text(options.defaultEffort.map { "Default (\($0.capitalized))" } ?? "Default effort").tag("")
                ForEach(options.efforts, id: \.self) { Text($0.capitalized).tag($0) }
            }
        } label: {
            Text(name + " · " + (settings.effort ?? options.defaultEffort ?? "Default").capitalized)
                .foregroundColor(Theme.text.secondary.color)
        }
        .menuStyle(.borderlessButton).fixedSize().windowFont(.control)
        .foregroundColor(Theme.text.secondary.color)
        .help("Choose the model and reasoning effort")
        .accessibilityLabel("Model and effort")
        .accessibilityValue(name + " " + (settings.effort ?? "Default"))
        }
    }
}

struct PermissionControl: View {
    let value: String
    let options: ComposerOptions
    let select: (String) -> Void
    private var labelColor: Color {
        PermissionPolicy.widens(from: "ask", to: value) ? Theme.state.attention : Theme.text.secondary.color
    }
    var body: some View {
        Menu {
            ForEach(options.permissions) { choice in
                Button(choice.policy.label) { select(choice.policy.rawValue) }.disabled(!choice.enabled)
            }
        } label: {
            Text("\(Image(systemName: "slider.horizontal.3")) \(PermissionPolicy(rawValue: value)?.label ?? value)")
                .foregroundColor(labelColor)
        }
        .menuStyle(.borderlessButton).fixedSize().windowFont(.control)
        .foregroundColor(labelColor)
        .help("What the agent may do without asking")
        .accessibilityLabel("Permission")
        .accessibilityValue(PermissionPolicy(rawValue: value)?.label ?? value)
    }
}
#endif
