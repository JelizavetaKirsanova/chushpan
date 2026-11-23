import SwiftUI

struct DevicesView: View {
    @EnvironmentObject private var model: AppModel
    var focusDeviceID: String? = nil

    private var devices: [MacRemoteDevice] { model.macStatus?.devices ?? [] }
    private var ledDevices: [MacRemoteDevice] { devices.filter { $0.kind == "led_strip" } }

    var body: some View {
        ScrollViewReader { proxy in
        List {
            Section {
                MobilePageHeader(
                    eyebrow: "Подключённый Mac",
                    title: "Устройства",
                    subtitle: "Управляй устройствами, добавленными в Чушпан",
                    symbol: "lightbulb.2.fill"
                )
                HStack {
                    Label(
                        model.macStatus == nil ? "Mac недоступен" : "Подключено к Mac", systemImage: "desktopcomputer")
                    Spacer()
                    Button("Обновить") { Task { await model.refreshMac() } }
                }
                Text("Выбери устройство, чтобы управлять им.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            if model.macStatus == nil {
                Section {
                    StatusCard(
                        title: "Устройства Mac недоступны",
                        detail: "Сопряги Mac в настройках и подключи оба устройства к одной локальной сети. Разрешения устройств задаются на Mac.",
                        symbol: "wifi.slash",
                        tint: .orange
                    )
                    NavigationLink(destination: SettingsView()) {
                        Label("Настроить сопряжение", systemImage: "link")
                    }
                }
            } else if devices.isEmpty {
                ContentUnavailableView(
                    "Нет разрешённых устройств",
                    systemImage: "lightbulb.2",
                    description: Text("Выдай этому устройству разрешение «Устройства» в панели сопряжения Mac.")
                )
                Section("Подсветка") {
                    StatusCard(title: "Подключение пока не разрешено", detail: "Лента и устройства не запускаются до разрешения на Mac.", symbol: "lock")
                }
            } else {
                Section("Устройства Mac") {
                    ForEach(devices) { device in
                        VStack(alignment: .leading, spacing: 8) {
                            if let location = device.locationText {
                                Label(location, systemImage: "mappin.and.ellipse")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Label(device.availabilityText, systemImage: device.availabilitySymbol)
                                .font(.caption)
                                .foregroundStyle(device.availability == "offline" ? Color.orange : Color.secondary)
                            Group {
                                switch device.kind {
                                case "led_strip":
                                    LEDDeviceCard(device: device)
                                case "mac":
                                    MacSystemDeviceCard(device: device)
                                case "external_display":
                                    ExternalDisplayCard(device: device)
                                default:
                                    VStack(alignment: .leading, spacing: 5) {
                                        Label(device.name, systemImage: "externaldrive.connected.to.line.below")
                                        Text("Тип «\(device.kind)» виден Mac, но мобильное приложение не показывает неподтверждённые команды для него.")
                                            .font(.footnote).foregroundStyle(.secondary)
                                    }
                                    .padding(.vertical, 4)
                                }
                            }
                            .disabled(device.availability == "offline" || device.availability == "remote-unavailable")
                        }
                        .id(device.id)
                    }
                }

                if !ledDevices.isEmpty, !model.visibleLightPresets.isEmpty {
                    Section("Избранные цвета") {
                        ForEach(model.visibleLightPresets.filter(\.isFavorite)) { preset in
                            HStack {
                                Circle().fill(Color(red: Double(preset.rgb.red) / 255, green: Double(preset.rgb.green) / 255, blue: Double(preset.rgb.blue) / 255))
                                    .frame(width: 25, height: 25)
                                    .accessibilityLabel(preset.rgb.hex)
                                VStack(alignment: .leading) {
                                    Text(preset.name)
                                    Text("RGB \(preset.rgb.red), \(preset.rgb.green), \(preset.rgb.blue) · \(preset.brightness)%")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Menu {
                                    ForEach(ledDevices) { device in
                                        Button("На Mac · \(device.name)") { model.applyLightPreset(preset, to: device.id) }
                                    }
                                    Button("Убрать из избранного", systemImage: "heart.slash") { model.toggleLightFavorite(preset.id) }
                                } label: {
                                    Image(systemName: "paperplane.circle.fill")
                                        .font(.title2)
                                        .accessibilityLabel("Применить пресет на Mac")
                                }
                            }
                        }
                    }
                }
            }

            if let feedback = model.macActionFeedback {
                Section(feedback.state.title) {
                    Text(feedback.message).font(.footnote).fixedSize(horizontal: false, vertical: true)
                    if feedback.state == .confirmationRequired {
                        TextField("Код подтверждения Mac", text: $model.macConfirmationCode)
                            .keyboardType(.numberPad)
                            .accessibilityLabel("Код подтверждения чувствительного действия")
                        Button("Подтвердить на Mac", systemImage: "checkmark.shield") { model.confirmMacAction() }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Устройства")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.refreshMac() }
        .onAppear {
            guard let focusDeviceID else { return }
            DispatchQueue.main.async { proxy.scrollTo(focusDeviceID, anchor: .center) }
        }
        }
    }
}

struct LightingView: View {
    @EnvironmentObject private var model: AppModel

    private var ledDevices: [MacRemoteDevice] {
        (model.macStatus?.devices ?? []).filter { $0.kind == "led_strip" }
    }

    var body: some View {
        List {
            Section {
                MobilePageHeader(
                    eyebrow: "Цвет и яркость",
                    title: "Освещение",
                    subtitle: "Настрой цвет и яркость света",
                    symbol: "lightbulb.led.wide"
                )
                HStack {
                    Label(
                        model.macStatus == nil ? "Mac недоступен" : "Подсветка Mac", systemImage: "lightbulb.led.wide")
                    Spacer()
                    Button("Обновить") { Task { await model.refreshMac() } }
                }
                Text("Выбери световое устройство и установи нужный цвет.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            if model.macStatus == nil {
                Section {
                    StatusCard(
                        title: "Нет соединения с Mac",
                        detail: "Подключи сопряжённый Mac, чтобы увидеть разрешённые световые устройства.",
                        symbol: "wifi.slash",
                        tint: .orange
                    )
                    NavigationLink(destination: SettingsView()) {
                        Label("Настроить сопряжение", systemImage: "link")
                    }
                }
            } else if ledDevices.isEmpty {
                ContentUnavailableView(
                    "Подсветка недоступна",
                    systemImage: "lightbulb.led.wide",
                    description: Text(model.macStatus?.capabilities.contains("devices") == true
                        ? "Mac не передал ни одной доступной LED-ленты."
                        : "Выдай этому iPhone разрешение «Устройства» в панели сопряжения Mac.")
                )
            } else {
                Section("Световые устройства") {
                    ForEach(ledDevices) { device in
                        LEDDeviceCard(device: device)
                    }
                }
            }

            if let feedback = model.macActionFeedback {
                Section(feedback.state.title) {
                    Text(feedback.message).font(.footnote).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Освещение")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.refreshMac() }
    }
}

private struct LEDDeviceCard: View {
    @EnvironmentObject private var model: AppModel
    let device: MacRemoteDevice
    @State private var red = 140.0
    @State private var green = 70.0
    @State private var blue = 210.0
    @State private var brightness = 60.0
    @State private var presetName = ""
    @State private var showingSavePreset = false

    private var rgb: RGBColor {
        RGBColor(red: Int(red.rounded()), green: Int(green.rounded()), blue: Int(blue.rounded()))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(device.name, systemImage: "lightbulb.led.wide")
                .font(.headline)
            Text("Через подключённый Mac · состояние устройства не читается постоянно")
                .font(.caption).foregroundStyle(.secondary)

            rgbSlider("Красный", value: $red, tint: .red)
            rgbSlider("Зелёный", value: $green, tint: .green)
            rgbSlider("Синий", value: $blue, tint: .blue)
            HStack {
                Text("Яркость").accessibilityHidden(true)
                Slider(value: $brightness, in: 0...100, step: 1)
                    .accessibilityLabel("Яркость ленты, процентов")
                Text("\(Int(brightness))%").monospacedDigit().frame(width: 42, alignment: .trailing)
            }

            HStack(spacing: 10) {
                Button("Применить RGB", systemImage: "paintpalette") {
                    model.runMacLight(deviceID: device.id, rgb: rgb, brightness: nil, power: nil)
                }
                .buttonStyle(.borderedProminent)
                Button("Яркость", systemImage: "sun.max") {
                    model.runMacLight(deviceID: device.id, rgb: nil, brightness: Int(brightness), power: nil)
                }
                .buttonStyle(.bordered)
            }
            HStack {
                Button("Включить", systemImage: "power") {
                    model.runMacLight(deviceID: device.id, rgb: nil, brightness: nil, power: true)
                }
                .buttonStyle(.bordered)
                Button("Выключить", systemImage: "poweroff") {
                    model.runMacLight(deviceID: device.id, rgb: nil, brightness: nil, power: false)
                }
                .buttonStyle(.bordered)
                Spacer()
                Button("Сохранить пресет", systemImage: "heart") { showingSavePreset = true }
                    .labelStyle(.iconOnly)
                    .accessibilityLabel("Сохранить RGB как пресет")
            }
            if !model.visibleLightPresets.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(model.visibleLightPresets) { preset in
                            Menu {
                                Button("Применить на Mac", systemImage: "paperplane") {
                                    model.applyLightPreset(preset, to: device.id)
                                }
                                Button(preset.isFavorite ? "Убрать из избранного" : "В избранное", systemImage: preset.isFavorite ? "heart.slash" : "heart") {
                                    model.toggleLightFavorite(preset.id)
                                }
                                Button("Удалить пресет", systemImage: "trash", role: .destructive) {
                                    model.deleteLightPreset(preset.id)
                                }
                            } label: {
                                HStack(spacing: 6) {
                                    Circle().fill(Color(red: Double(preset.rgb.red) / 255, green: Double(preset.rgb.green) / 255, blue: Double(preset.rgb.blue) / 255)).frame(width: 13, height: 13)
                                    Text(preset.name).lineLimit(1)
                                }
                                .font(.caption)
                                .padding(.horizontal, 10).padding(.vertical, 7)
                                .background(Color(uiColor: .tertiarySystemGroupedBackground), in: Capsule())
                            }
                            .accessibilityLabel("Пресет \(preset.name), RGB \(preset.rgb.hex)")
                        }
                    }
                }
                .scrollIndicators(.hidden)
            }
        }
        .padding(.vertical, 6)
        .alert("Сохранить пресет", isPresented: $showingSavePreset) {
            TextField("Название", text: $presetName)
            Button("Сохранить") {
                model.addLightPreset(name: presetName, rgb: rgb, brightness: Int(brightness), favorite: true)
                presetName = ""
            }
            Button("Отмена", role: .cancel) { presetName = "" }
        } message: {
            Text("RGB \(rgb.red), \(rgb.green), \(rgb.blue) · яркость \(Int(brightness))%")
        }
    }

    private func rgbSlider(_ name: String, value: Binding<Double>, tint: Color) -> some View {
        HStack {
            Text(name).frame(width: 68, alignment: .leading).accessibilityHidden(true)
            Slider(value: value, in: 0...255, step: 1).tint(tint).accessibilityLabel("\(name), от 0 до 255")
            Text("\(Int(value.wrappedValue))").monospacedDigit().frame(width: 34, alignment: .trailing)
        }
    }
}

private struct MacSystemDeviceCard: View {
    @EnvironmentObject private var model: AppModel
    let device: MacRemoteDevice

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(device.name, systemImage: "desktopcomputer").font(.headline)
            RemoteDeviceLevelControl(
                device: device,
                title: "Яркость Mac",
                action: "brightness",
                stateKey: "brightness",
                featureKey: "brightness"
            )
            RemoteDeviceLevelControl(
                device: device,
                title: "Громкость Mac",
                action: "volume",
                stateKey: "volume_percent",
                featureKey: "volume"
            )
            HStack {
                Button("Пауза музыки") { model.runMacDevice(deviceID: device.id, action: "playback", parameters: ["playback": "pause"], label: "Музыка Mac") }
                Button("Продолжить") { model.runMacDevice(deviceID: device.id, action: "playback", parameters: ["playback": "resume"], label: "Музыка Mac") }
            }
            .buttonStyle(.bordered)
        }
        .padding(.vertical, 6)
    }
}

private struct ExternalDisplayCard: View {
    let device: MacRemoteDevice

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(device.name, systemImage: "display")
            RemoteDeviceLevelControl(
                device: device,
                title: "Яркость внешнего дисплея",
                action: "brightness",
                stateKey: "brightness",
                featureKey: "brightness"
            )
        }
        .padding(.vertical, 6)
    }
}

private struct RemoteDeviceLevelControl: View {
    @EnvironmentObject private var model: AppModel
    let device: MacRemoteDevice
    let title: String
    let action: String
    let stateKey: String
    let featureKey: String
    @State private var value: Double?
    @State private var isEditing = false
    @State private var lastSubmitted: Int?
    @State private var debounceTask: Task<Void, Never>?

    private var confirmedLevel: Int? {
        if stateKey == "volume_percent" { return device.volumePercent }
        return device.brightness
    }

    private var displayedLevel: Int? {
        value.map { Int($0.rounded()) } ?? confirmedLevel
    }

    var body: some View {
        Group {
            if device.features?[featureKey] == false {
                Label(device.controlReason ?? "\(title): управление недоступно в настройках Mac.", systemImage: "lock.slash")
                    .font(.footnote).foregroundStyle(.secondary)
            } else if device.kind == "external_display",
                      device.connection == "error" || device.connection == "disconnected" {
                Label(device.connectionError ?? "Внешний дисплей сейчас недоступен.", systemImage: "display.trianglebadge.exclamationmark")
                    .font(.footnote).foregroundStyle(.orange)
            } else if let level = displayedLevel {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(title)
                        Spacer()
                        Text("\(displayedLevel ?? level)%")
                            .monospacedDigit()
                    }
                    Slider(
                        value: Binding(
                            get: { value ?? Double(level) },
                            set: { value = $0 }
                        ),
                        in: 0...100,
                        step: 1,
                        onEditingChanged: { editing in
                            isEditing = editing
                            if !editing { scheduleSend(immediately: true) }
                        }
                    )
                    .accessibilityLabel("\(title), процентов")
                    .onChange(of: value) { _, newValue in
                        guard isEditing, newValue != nil else { return }
                        scheduleSend(immediately: false)
                    }
                    if stateKey == "volume_percent", device.isMuted == true {
                        Label("Системный звук выключен", systemImage: "speaker.slash.fill")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Label("\(title): Mac не подтвердил текущее значение; шкала скрыта.", systemImage: "exclamationmark.triangle")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .onAppear {
            if value == nil { value = confirmedLevel.map(Double.init) }
        }
        .onChange(of: device.state) { _, _ in
            guard !isEditing else { return }
            value = confirmedLevel.map(Double.init)
            lastSubmitted = nil
        }
        .onChange(of: model.macActionFeedback?.message) { _, _ in
            if model.macActionFeedback?.state == .failed || model.macActionFeedback?.state == .unknown {
                lastSubmitted = nil
            }
        }
        .onDisappear { debounceTask?.cancel() }
    }

    private func scheduleSend(immediately: Bool) {
        debounceTask?.cancel()
        guard let level = displayedLevel, level != lastSubmitted else { return }
        debounceTask = Task {
            if !immediately {
                try? await Task.sleep(for: .milliseconds(450))
            }
            guard !Task.isCancelled else { return }
            lastSubmitted = level
            model.runMacDevice(
                deviceID: device.id,
                action: action,
                parameters: ["level": level],
                label: title
            )
        }
    }
}

private extension MacRemoteDevice {
    var availabilitySymbol: String {
        switch availability ?? "unknown" {
        case "online": "checkmark.circle.fill"
        case "offline": "wifi.slash"
        case "remote-unavailable": "exclamationmark.triangle.fill"
        default: "questionmark.circle"
        }
    }
}
