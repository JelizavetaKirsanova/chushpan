import SwiftUI

struct ScenariosView: View {
    @EnvironmentObject private var model: AppModel
    @State private var variants: [String: String] = [:]
    @State private var editingScenario: MacRemoteScenario?

    private var scenarios: [MacRemoteScenario] { model.macStatus?.scenarios ?? [] }

    var body: some View {
        List {
            Section {
                MobilePageHeader(
                    eyebrow: "ТВОИ КОМАНДЫ",
                    title: "Сценарии",
                    subtitle: "Запускай привычные команды одним нажатием",
                    symbol: "sparkles"
                )
                Button("Обновить список") { Task { await model.refreshMac() } }
            }

            if model.macStatus == nil {
                Section {
                    Text("Сопряги Mac в настройках и проверь, что оба устройства находятся в одной локальной сети.")
                        .foregroundStyle(.secondary)
                    NavigationLink(destination: SettingsView()) {
                        Label("Настроить Mac", systemImage: "link")
                    }
                }
            } else if !model.macStatus!.capabilities.contains("scenario") {
                Section {
                    ContentUnavailableView(
                        "Нет разрешения на сценарии",
                        systemImage: "lock",
                        description: Text("Владелец Mac выдаёт разрешение «Сценарии» отдельно для каждого сопряжённого устройства.")
                    )
                }
            } else if scenarios.isEmpty {
                Section {
                    ContentUnavailableView("Нет доступных сценариев", systemImage: "sparkles")
                }
            } else {
                Section("На Mac") {
                    ForEach(scenarios) { scenario in
                        VStack(alignment: .leading, spacing: 12) {
                            Text(scenario.name).font(.headline).fixedSize(horizontal: false, vertical: true)
                            Picker("Версия сценария", selection: variantBinding(scenario.id)) {
                                Text("Место определит Mac").tag("")
                                Text("Общий").tag("general")
                                ForEach(scenario.variants.filter(\.enabled)) { variant in
                                    Text(variant.name).tag(variant.id)
                                }
                            }
                            .pickerStyle(.menu)
                            Button("Выполнить на Mac", systemImage: "play.fill") {
                                if scenario.variables.isEmpty {
                                    model.runMacScenario(scenario, variant: variants[scenario.id] ?? "")
                                } else {
                                    editingScenario = scenario
                                }
                            }
                            .buttonStyle(.borderedProminent)
                        }
                        .padding(.vertical, 7)
                    }
                }
            }

            if let feedback = model.macActionFeedback {
                Section(feedback.state.title) {
                    Text(feedback.message).fixedSize(horizontal: false, vertical: true)
                    if feedback.state == .confirmationRequired {
                        TextField("Свежий код подтверждения", text: $model.macConfirmationCode)
                            .keyboardType(.numberPad)
                        Button("Подтвердить на Mac") { model.confirmMacAction() }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Сценарии")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $editingScenario) { scenario in
            ScenarioValuesSheet(scenario: scenario) { values in
                model.runMacScenario(scenario, variant: variants[scenario.id] ?? "", values: values)
                editingScenario = nil
            }
        }
        .task { await model.refreshMac() }
    }

    private func variantBinding(_ id: String) -> Binding<String> {
        Binding(get: { variants[id] ?? "" }, set: { variants[id] = $0 })
    }
}

struct ScenarioValuesSheet: View {
    let scenario: MacRemoteScenario
    let run: ([String: Any]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var values: [String: String] = [:]
    @State private var validationMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Оставь поле пустым, чтобы Mac использовал сохранённое значение сценария.")
                        .font(.footnote).foregroundStyle(.secondary)
                    ForEach(scenario.variables) { variable in
                        TextField(variable.name, text: binding(for: variable.name), prompt: Text(placeholder(for: variable.kind)))
                            .keyboardType(variable.kind == "number" ? .decimalPad : .default)
                            .textInputAutocapitalization(variable.kind == "string" ? .sentences : .never)
                            .accessibilityLabel("Значение сценария \(variable.name), тип \(variable.kind)")
                    }
                    if let validationMessage {
                        Text(validationMessage).font(.footnote).foregroundStyle(.red)
                    }
                } header: {
                    Text("Параметры сценария «\(scenario.name)»")
                }
            }
            .navigationTitle("Параметры")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Выполнить на Mac", action: submit) }
            }
        }
    }

    private func binding(for name: String) -> Binding<String> {
        Binding(get: { values[name, default: ""] }, set: { values[name] = $0 })
    }

    private func placeholder(for kind: String) -> String {
        switch kind {
        case "number": "Число"
        case "boolean": "true или false"
        default: "Текст"
        }
    }

    private func submit() {
        var parsed: [String: Any] = [:]
        for variable in scenario.variables {
            let value = values[variable.name, default: ""].trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else { continue }
            switch variable.kind {
            case "number":
                guard let number = Double(value.replacingOccurrences(of: ",", with: ".")), number.isFinite else {
                    validationMessage = "«\(variable.name)» должно быть числом."
                    return
                }
                parsed[variable.name] = number
            case "boolean":
                guard let boolean = Bool(value.lowercased()) else {
                    validationMessage = "Для «\(variable.name)» введи true или false."
                    return
                }
                parsed[variable.name] = boolean
            default:
                parsed[variable.name] = String(value.prefix(1000))
            }
        }
        run(parsed)
    }
}
