import AVFoundation
import SwiftUI
import VisionKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var apiKey = ""
    @State private var keyMessage: String?
    @State private var pairInvitation = ""
    @State private var pairFingerprint = ""
    @State private var remoteAddress = ""
    @State private var pairMessage: String?
    @State private var showingCloudConsent = false
    @State private var showingScanner = false
    @State private var showingForgetMac = false

    var body: some View {
        Form {
            Section {
                Picker("Модель", selection: Binding(get: { model.selectedModel }, set: model.setModel)) {
                    Text("Gemini 3.8 Flash · по умолчанию").tag("gemini-3.8-flash")
                    Text("Gemini 3.7 Flash").tag("gemini-3.7-flash")
                    Text("Gemini 3.6 Flash").tag("gemini-3.6-flash")
                }
                SecureField("Личный API-ключ", text: $apiKey)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textContentType(.password)
                    .accessibilityLabel("Gemini API-ключ")
                HStack {
                    Button("Сохранить ключ") { saveKey() }
                        .disabled(apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    if model.hasAPIKey {
                        Button("Удалить ключ", role: .destructive) { clearKey() }
                    }
                }
                Label(model.hasAPIKey ? "Ключ сохранён в Keychain этого устройства" : "Ключ не добавлен", systemImage: model.hasAPIKey ? "key.fill" : "key")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                if let keyMessage { Text(keyMessage).font(.footnote).foregroundStyle(.secondary) }
                Text("При выборе «Этот iPhone/iPad» запрос к Google отправляется только после нажатия «Отправить»: сообщение, последние до 24 сообщений диалога (не более 32 000 знаков), системная инструкция и, при включённом переключателе ниже, до 8 подходящих фактов. При выборе Mac текст идёт по защищённому сопряжению в существующее приложение Mac. Ключ Gemini не включается в текст запроса и не передаётся Mac.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Link("Модели Gemini API", destination: URL(string: "https://ai.google.dev/gemini-api/docs/models")!)
                    .font(.footnote)
                Text("Приложение не включает подписку и не запускает платные запросы при настройке. Стоимость и доступность зависят от проекта и квот ключа Google.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } header: { Text("Облачный ИИ") }

            Section("Приватность ИИ") {
                Toggle("Добавлять подходящие факты памяти к запросу", isOn: Binding(
                    get: { model.includeRelevantMemory }, set: model.setIncludeRelevantMemory
                ))
                Text("Поиск выполняется только по фактам этого устройства. Приватные факты и приватные диалоги не добавляются. Переключатель выключен по умолчанию.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            Section("Голос") {
                Toggle("Озвучивать готовые ответы", isOn: Binding(
                    get: { model.speakResponses }, set: model.setSpeakResponses
                ))
                Picker("Русский голос", selection: Binding(
                    get: { model.voiceIdentifier }, set: model.setVoiceIdentifier
                )) {
                    Text("Автоматический русский").tag("")
                    ForEach(SpeechService.russianVoices, id: \.identifier) { voice in
                        Text("\(voice.name) · \(voice.quality == .enhanced ? "улучшенный" : "стандартный")")
                            .tag(voice.identifier)
                    }
                }
                Text("Голосовой ввод использует Apple Speech и включается только по кнопке; распознанный текст попадёт выбранному ИИ только после отправки. Обработка речи зависит от доступности API Apple и прекращается при уходе приложения в фон. Постоянного прослушивания нет.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            Section("Синхронизация iCloud") {
                Toggle("Синхронизировать данные Чушпана", isOn: Binding(
                    get: { model.cloudSyncEnabled },
                    set: { value in
                        if value { showingCloudConsent = true } else { model.disableCloudSync() }
                    }
                ))
                Text(model.cloudSyncStatus).font(.footnote).foregroundStyle(.secondary)
                Button("Синхронизировать сейчас", systemImage: "arrow.triangle.2.circlepath") {
                    model.requestCloudSync()
                }
                .disabled(!model.cloudSyncEnabled || model.isCloudSyncing)
                if model.isCloudSyncing { ProgressView() }
                Text("Перед включением приложение отправит в личную базу CloudKit незакрытые приватным режимом диалоги, сообщения, обычные факты памяти, медиатеку, пресеты света и тему/модель. Приватные факты и диалоги, секреты, разрешения, адрес Mac и его кэш исключены. Удаления хранятся как метки, чтобы старые записи не вернулись.")
                    .font(.footnote).foregroundStyle(.secondary)
                Text("На обоих устройствах нужен один Apple Account и включённый iCloud. Для CloudKit включи iCloud/CloudKit capability и контейнер в Xcode Signing & Capabilities; Apple указывает активное членство Apple Developer Program для настройки capability. Без подписи с CloudKit приложение продолжит локальную работу.")
                    .font(.footnote).foregroundStyle(.secondary)
                Link("Настройка CloudKit в Xcode", destination: URL(string: "https://developer.apple.com/documentation/cloudkit/enabling-cloudkit-in-your-app")!)
                    .font(.footnote)
            }

            Section("Подключение к Чушпану") {
                if model.macTrusted {
                    Label(
                        model.macStatus.map { "\($0.name ?? "Mac") — Подключено" } ?? "Mac переподключается",
                        systemImage: model.macStatus == nil ? "wifi.slash" : "lock.shield.fill"
                    )
                    .foregroundStyle(model.macStatus == nil ? Color.gray : Color.green)
                    Text(model.macStatus == nil ? model.macDiscoveryMessage : model.macConnectionKind)
                        .font(.footnote).foregroundStyle(.secondary)
                    if let last = model.macLastConnectedAt {
                        Text("Последнее подключение · \(last.formatted(date: .abbreviated, time: .shortened))")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    Toggle("Переподключаться автоматически", isOn: Binding(
                        get: { model.macAutoReconnectEnabled },
                        set: { model.setMacAutoReconnect($0) }
                    ))
                    HStack {
                        Button("Проверить") { Task { await model.refreshMac() } }
                        Button("Отключить", role: .destructive) { model.disconnectMac() }
                        Button("Забыть") { showingForgetMac = true }
                    }
                } else {
                    Label("Найденные устройства", systemImage: "dot.radiowaves.left.and.right")
                    if model.discoveredMacs.isEmpty {
                        HStack(alignment: .top, spacing: 10) {
                            if model.macDiscoveryMessage.hasPrefix("Ищу") { ProgressView() }
                            Text(model.macDiscoveryMessage).font(.footnote).foregroundStyle(.secondary)
                        }
                    } else {
                        ForEach(model.discoveredMacs) { device in
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(device.name).font(.headline)
                                    Text("Локальная сеть · защищённое сопряжение")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Подключить") {
                                    Task { await model.connectToMac(device) }
                                }
                                .buttonStyle(.borderedProminent)
                            }
                            .padding(.vertical, 4)
                        }
                    }
                }

                if !model.macTrusted, let code = model.macPairCode {
                    Text("Код подтверждения")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text(code).font(.title.monospacedDigit().weight(.bold))
                        .accessibilityLabel("Код сопряжения \(code). Сравни его с кодом на Mac.")
                    Text("Сравни код с окном Чушпана на Mac. Если он совпадает, подтверди запрос там; иначе отклони его.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Text(model.macConnectionMessage).font(.footnote).foregroundStyle(.secondary)

                if model.macTrusted {
                    Button("Запросить разрешённые данные Mac", systemImage: "arrow.down.doc") {
                        Task { await model.syncFromMac() }
                    }
                    Text("Mac возвращает только категории, разрешённые на Mac. Копия сохраняется здесь и не отправляется в CloudKit.")
                        .font(.footnote).foregroundStyle(.secondary)
                    if let date = model.database.macCacheUpdatedAt {
                        Text("Локальная копия Mac · \(date.formatted(date: .abbreviated, time: .shortened))")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }

                DisclosureGroup("Дополнительные настройки") {
                    Text("Bonjour может быть заблокирован гостевой или корпоративной Wi-Fi сетью. При такой изоляции используй Tailscale на обоих устройствах.")
                        .font(.footnote).foregroundStyle(.secondary)
                    TextField("https://адрес-Tailscale-Mac:порт", text: $remoteAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    Button("Подключить через Tailscale") {
                        Task { await model.pairWithMac(address: remoteAddress) }
                    }
                    .disabled(remoteAddress.isEmpty)
                    Text("Установи Tailscale и разреши VPN-профиль на iPhone/iPad. Чушпан не открывает сервер в публичный интернет. После первого сопряжения адрес сохраняется для переподключения.")
                        .font(.footnote).foregroundStyle(.secondary)

                    DisclosureGroup("Старое приглашение и диагностика") {
                        TextField("Ссылка из одноразового QR Mac", text: $pairInvitation, axis: .vertical)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                        TextField("SHA-256 отпечаток CA (совместимость со старым Mac)", text: $pairFingerprint)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.asciiCapable)
                        HStack {
                            Button("Сканировать QR") {
                                Task {
                                    let granted = await AVCaptureDevice.requestAccess(for: .video)
                                    if granted && DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                                        showingScanner = true
                                    } else {
                                        pairMessage = "Разреши камеру для сканирования или введи ссылку вручную."
                                    }
                                }
                            }
                            .disabled(!DataScannerViewController.isSupported)
                            Button("Сопрячь") {
                                Task { await model.pairWithMac(invitation: pairInvitation, fingerprint: pairFingerprint) }
                            }
                            .disabled(pairInvitation.isEmpty || pairFingerprint.isEmpty)
                        }
                        if let pairMessage { Text(pairMessage).font(.footnote).foregroundStyle(.secondary) }
                        Text("Подключение: \(model.macBaseURL.isEmpty ? "не настроено" : model.macBaseURL)")
                            .font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                }
            }

            Section("Оформление") {
                Picker("Тема", selection: Binding(get: { model.appearance }, set: model.setAppearance)) {
                    Text("Как в системе").tag("system")
                    Text("Светлая").tag("light")
                    Text("Тёмная").tag("dark")
                }
            }

            Section("Локальное хранение") {
                Text("История, факты и медиатека сохраняются в Application Support приложения. Ключ Gemini и закрытый ключ сопряжения хранятся только в Keychain этого устройства. Уход приложения в фон останавливает распознавание и текущую генерацию.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .navigationTitle("Настройки")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Включить синхронизацию личных данных?", isPresented: $showingCloudConsent, titleVisibility: .visible) {
            Button("Включить CloudKit") { model.enableCloudSync() }
            Button("Отмена", role: .cancel) { }
        } message: {
            Text("Только после этого локальные диалоги, обычная память, медиатека и общие настройки будут отправляться в личный iCloud. Приватные данные, ключи и копия Mac исключены.")
        }
        .confirmationDialog("Забыть сопряжение?", isPresented: $showingForgetMac, titleVisibility: .visible) {
            Button("Удалить данные сопряжения", role: .destructive) { model.forgetMacOnThisDevice() }
        } message: {
            Text("Это удалит закрытый ключ только с iPhone/iPad. Для полного отзыва также удали устройство в панели Mac.")
        }
        .sheet(isPresented: $showingScanner) {
            NavigationStack {
                PairInvitationScanner { pairInvitation = $0 }
                    .ignoresSafeArea()
                    .navigationTitle("QR-приглашение Mac")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Готово") { showingScanner = false } } }
            }
        }
        .task {
            if remoteAddress.isEmpty { remoteAddress = model.macRemoteURL }
        }
    }

    private func saveKey() {
        do {
            try model.saveAPIKey(apiKey)
            apiKey = ""
            keyMessage = "Ключ сохранён. Он не показывается повторно и не синхронизируется."
        } catch { keyMessage = error.localizedDescription }
    }

    private func clearKey() {
        do { try model.clearAPIKey(); keyMessage = "Ключ удалён с этого устройства." }
        catch { keyMessage = error.localizedDescription }
    }
}
