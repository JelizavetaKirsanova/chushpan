import SwiftUI

enum MobileHomeWidget: String, CaseIterable, Identifiable {
    case voice, chat, continueWatching, quickActions, favoriteScenarios, favoriteDevices

    var id: String { rawValue }

    var title: String {
        switch self {
        case .voice: "Голосовой разговор"
        case .chat: "Быстрый доступ к чату"
        case .continueWatching: "Продолжить просмотр"
        case .quickActions: "Быстрые действия"
        case .favoriteScenarios: "Избранные сценарии"
        case .favoriteDevices: "Избранные устройства"
        }
    }

    static let defaultLayout: [MobileHomeWidget] = [.voice, .chat, .continueWatching, .quickActions]

    static func decode(_ value: String) -> [MobileHomeWidget] {
        guard let data = value.data(using: .utf8),
              let names = try? JSONDecoder().decode([String].self, from: data) else {
            return defaultLayout
        }
        return names.compactMap(MobileHomeWidget.init(rawValue:)).uniqued()
    }

    static func encode(_ widgets: [MobileHomeWidget]) -> String {
        guard let data = try? JSONEncoder().encode(widgets.map(\.rawValue)),
              let value = String(data: data, encoding: .utf8) else { return "[]" }
        return value
    }
}

enum MobileHomeAction: String, CaseIterable, Identifiable {
    case devices, lighting, scenarios, library, calendar, memory, statistics

    var id: String { rawValue }

    var title: String {
        switch self {
        case .devices: "Устройства"
        case .lighting: "Освещение"
        case .scenarios: "Сценарии"
        case .library: "Медиатека"
        case .calendar: "Календарь"
        case .memory: "Память"
        case .statistics: "Статистика"
        }
    }

    var symbol: String {
        switch self {
        case .devices: "lightbulb.2"
        case .lighting: "lightbulb.led.wide"
        case .scenarios: "sparkles"
        case .library: "play.rectangle"
        case .calendar: "calendar"
        case .memory: "brain.head.profile"
        case .statistics: "chart.bar.xaxis"
        }
    }

    static let defaultActions: [MobileHomeAction] = []

    static func decode(_ value: String) -> [MobileHomeAction] {
        guard let data = value.data(using: .utf8),
              let names = try? JSONDecoder().decode([String].self, from: data) else {
            return defaultActions
        }
        return names.compactMap(MobileHomeAction.init(rawValue:)).uniqued()
    }

    static func encode(_ actions: [MobileHomeAction]) -> String {
        guard let data = try? JSONEncoder().encode(actions.map(\.rawValue)),
              let value = String(data: data, encoding: .utf8) else { return "[]" }
        return value
    }
}

enum MobileHomeFavorites {
    static func decode(_ value: String) -> Set<String> {
        guard let data = value.data(using: .utf8),
              let ids = try? JSONDecoder().decode([String].self, from: data) else { return [] }
        return Set(ids)
    }

    static func encode(_ ids: Set<String>) -> String {
        guard let data = try? JSONEncoder().encode(ids.sorted()),
              let value = String(data: data, encoding: .utf8) else { return "[]" }
        return value
    }
}

private extension Sequence where Element: Hashable {
    func uniqued() -> [Element] {
        var seen = Set<Element>()
        return filter { seen.insert($0).inserted }
    }
}

struct HomeView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("mobile.home.widgets") private var widgetsJSON = "[\"voice\",\"chat\",\"continueWatching\",\"quickActions\"]"
    @AppStorage("mobile.home.actions") private var actionsJSON = "[]"
    @State private var showingCustomization = false
    @State private var editingFavoriteScenario: MacRemoteScenario?
    @AppStorage("mobile.home.favoriteScenarioIDs") private var favoriteScenarioIDsJSON = "[]"
    @AppStorage("mobile.home.favoriteDeviceIDs") private var favoriteDeviceIDsJSON = "[]"

    private var widgets: [MobileHomeWidget] { MobileHomeWidget.decode(widgetsJSON) }
    private var actions: [MobileHomeAction] { MobileHomeAction.decode(actionsJSON) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                MobilePageHeader(
                    eyebrow: "ЛИЧНЫЙ ПОМОЩНИК",
                    title: "Привет, я Чушпан",
                    subtitle: "Что сделаем вместе?",
                    symbol: "cat.fill",
                    showsMascot: true
                )

                ForEach(widgets) { widget in
                    widgetContent(widget)
                }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 18)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle("Главная")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Настроить главную", systemImage: "slider.horizontal.3") {
                    showingCustomization = true
                }
                .labelStyle(.iconOnly)
            }
        }
        .sheet(isPresented: $showingCustomization) {
            HomeCustomizationSheet(widgetsJSON: $widgetsJSON, actionsJSON: $actionsJSON,
                                   favoriteScenarioIDsJSON: $favoriteScenarioIDsJSON,
                                   favoriteDeviceIDsJSON: $favoriteDeviceIDsJSON)
        }
        .sheet(item: $editingFavoriteScenario) { scenario in
            ScenarioValuesSheet(scenario: scenario) { values in
                model.runMacScenario(scenario, variant: "", values: values)
                editingFavoriteScenario = nil
            }
        }
        .simultaneousGesture(LongPressGesture(minimumDuration: 0.55).onEnded { _ in
            showingCustomization = true
        })
    }

    @ViewBuilder
    private func widgetContent(_ widget: MobileHomeWidget) -> some View {
        switch widget {
        case .voice:
            NavigationLink(destination: ChatView(startVoiceOnOpen: true)) {
                Label("Начать голосовой разговор", systemImage: "mic.fill")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityHint("Откроет чат и включит голосовой ввод по запросу")
        case .chat:
            NavigationLink(destination: ChatView()) {
                Label("Открыть чат", systemImage: "bubble.left.and.bubble.right.fill")
                    .font(.headline)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .mobileCard()
            }
            .buttonStyle(.plain)
        case .continueWatching:
            if let item = model.continueWatching {
                NavigationLink(destination: MediaLibraryView()) {
                    VStack(alignment: .leading, spacing: 7) {
                        Label("Продолжить просмотр", systemImage: "play.circle.fill")
                            .font(.headline)
                            .foregroundStyle(Color.accentPurple)
                        Text(item.title).font(.title3.weight(.semibold))
                        Text(progressText(item))
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .mobileCard(cornerRadius: 20)
                }
                .buttonStyle(.plain)
            }
        case .quickActions:
            if !actions.isEmpty {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    ForEach(actions) { action in
                        quickAction(action)
                    }
                }
                .accessibilityLabel("Быстрые действия")
            }
        case .favoriteScenarios:
            let selected = MobileHomeFavorites.decode(favoriteScenarioIDsJSON)
            let favorites = (model.macStatus?.scenarios ?? []).filter { selected.contains($0.id) }
            if !favorites.isEmpty {
                VStack(alignment: .leading, spacing: 9) {
                    Text("Избранные сценарии").font(.headline)
                    ForEach(favorites) { scenario in
                        Button {
                            if scenario.variables.isEmpty {
                                model.runMacScenario(scenario, variant: "")
                            } else {
                                editingFavoriteScenario = scenario
                            }
                        } label: {
                            Label(scenario.name, systemImage: "play.circle.fill")
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .mobileCard()
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        case .favoriteDevices:
            let selected = MobileHomeFavorites.decode(favoriteDeviceIDsJSON)
            let favorites = (model.macStatus?.devices ?? []).filter { selected.contains($0.id) }
            if !favorites.isEmpty {
                VStack(alignment: .leading, spacing: 9) {
                    Text("Избранные устройства").font(.headline)
                    ForEach(favorites) { device in
                        NavigationLink(destination: DevicesView(focusDeviceID: device.id)) {
                            Label(device.name, systemImage: device.kind == "led_strip" ? "lightbulb.led.wide" : "desktopcomputer")
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .mobileCard()
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private func quickAction(_ action: MobileHomeAction) -> some View {
        NavigationLink(destination: actionDestination(action)) {
            Label(action.title, systemImage: action.symbol)
                .font(.subheadline.weight(.medium))
                .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
                .mobileCard()
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func actionDestination(_ action: MobileHomeAction) -> some View {
        switch action {
        case .devices: DevicesView()
        case .lighting: LightingView()
        case .scenarios: ScenariosView()
        case .library: MediaLibraryView()
        case .calendar: CalendarView()
        case .memory: MemoryView()
        case .statistics: StatisticsView()
        }
    }

    private func progressText(_ item: MobileMediaItem) -> String {
        let minutes = Int(item.positionSeconds) / 60
        if item.kind == .series {
            return "Сезон \(item.season), серия \(item.episode) · \(minutes) мин"
        }
        return "\(minutes) мин сохранено"
    }
}

private struct HomeCustomizationSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var editMode: EditMode = .active
    @Binding var widgetsJSON: String
    @Binding var actionsJSON: String
    @Binding var favoriteScenarioIDsJSON: String
    @Binding var favoriteDeviceIDsJSON: String

    private var widgets: [MobileHomeWidget] { MobileHomeWidget.decode(widgetsJSON) }
    private var actions: [MobileHomeAction] { MobileHomeAction.decode(actionsJSON) }

    var body: some View {
        NavigationStack {
            List {
                Section("На главной") {
                    ForEach(widgets) { widget in
                        Label(widget.title, systemImage: "line.3.horizontal")
                    }
                    .onMove(perform: moveWidget)
                    .onDelete(perform: deleteWidget)
                }

                Section("Добавить блок") {
                    ForEach(MobileHomeWidget.allCases.filter { !widgets.contains($0) }) { widget in
                        Button {
                            widgetsJSON = MobileHomeWidget.encode(widgets + [widget])
                        } label: {
                            Label(widget.title, systemImage: "plus.circle")
                        }
                    }
                }

                Section("Быстрые действия") {
                    ForEach(actions) { action in
                        Label(action.title, systemImage: action.symbol)
                    }
                    .onMove(perform: moveAction)
                    .onDelete(perform: deleteAction)
                }

                Section("Добавить действие") {
                    ForEach(MobileHomeAction.allCases.filter { !actions.contains($0) }) { action in
                        Button {
                            actionsJSON = MobileHomeAction.encode(actions + [action])
                        } label: {
                            Label(action.title, systemImage: "plus.circle")
                        }
                    }
                }

                Section("Избранное на главной") {
                    if let macStatus = model.macStatus {
                        ForEach(macStatus.scenarios) { scenario in
                            Toggle(scenario.name, isOn: favoriteScenarioBinding(scenario.id))
                        }
                        ForEach(macStatus.devices) { device in
                            Toggle(device.name, isOn: favoriteDeviceBinding(device.id))
                        }
                        if macStatus.scenarios.isEmpty && macStatus.devices.isEmpty {
                            Text("Нет доступных сценариев и устройств.").foregroundStyle(.secondary)
                        }
                    } else {
                        Text("Подключи Mac в настройках, чтобы выбрать сценарии и устройства.")
                            .foregroundStyle(.secondary)
                    }
                }

                Section {
                    Button("Вернуть стандартную компоновку") {
                        widgetsJSON = MobileHomeWidget.encode(MobileHomeWidget.defaultLayout)
                        actionsJSON = MobileHomeAction.encode(MobileHomeAction.defaultActions)
                    }
                }
            }
            .environment(\.editMode, $editMode)
            .navigationTitle("Главная")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    EditButton()
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Готово") { dismiss() }
                }
            }
            .safeAreaInset(edge: .bottom) {
                Text("На iPhone и iPad размер блоков подстраивается под экран.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                    .background(.bar)
            }
        }
    }

    private func moveWidget(from source: IndexSet, to destination: Int) {
        var next = widgets
        next.move(fromOffsets: source, toOffset: destination)
        widgetsJSON = MobileHomeWidget.encode(next)
    }

    private func deleteWidget(at offsets: IndexSet) {
        var next = widgets
        next.remove(atOffsets: offsets)
        widgetsJSON = MobileHomeWidget.encode(next)
    }

    private func moveAction(from source: IndexSet, to destination: Int) {
        var next = actions
        next.move(fromOffsets: source, toOffset: destination)
        actionsJSON = MobileHomeAction.encode(next)
    }

    private func deleteAction(at offsets: IndexSet) {
        var next = actions
        next.remove(atOffsets: offsets)
        actionsJSON = MobileHomeAction.encode(next)
    }

    private func favoriteScenarioBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { MobileHomeFavorites.decode(favoriteScenarioIDsJSON).contains(id) }, set: { isFavorite in
            var ids = MobileHomeFavorites.decode(favoriteScenarioIDsJSON)
            if isFavorite { ids.insert(id) } else { ids.remove(id) }
            favoriteScenarioIDsJSON = MobileHomeFavorites.encode(ids)
        })
    }

    private func favoriteDeviceBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { MobileHomeFavorites.decode(favoriteDeviceIDsJSON).contains(id) }, set: { isFavorite in
            var ids = MobileHomeFavorites.decode(favoriteDeviceIDsJSON)
            if isFavorite { ids.insert(id) } else { ids.remove(id) }
            favoriteDeviceIDsJSON = MobileHomeFavorites.encode(ids)
        })
    }
}
