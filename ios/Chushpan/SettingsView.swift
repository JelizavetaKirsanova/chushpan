import AVFoundation
import SwiftUI
import VisionKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var apiKey = ""
    @State private var keyMessage: String?
    @State private var pairInvitation = ""
    @State private var pairFingerprint = ""
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

            Section("Подключение к Mac") {
                HStack {
                    Label(model.macStatus == nil ? "Mac недоступен" : "Mac на связи", systemImage: model.macStatus == nil ? "wifi.slash" : "wifi")
                        .foregroundStyle(model.macStatus == nil ? .secondary : .green)
                    Spacer()
                    Button("Проверить") { Task { await model.refreshMac() } }
                        .disabled(model.macBaseURL.isEmpty)
                }
                Text(model.macConnectionMessage).font(.footnote).foregroundStyle(.secondary)
                if let code = model.macPairCode {
                    Text("Код подтверждения: \(code)")
                        .font(.title3.monospacedDigit().weight(.semibold))
                        .accessibilityLabel("Код сопряжения \(code). Подтверди этот же код на Mac.")
                    Text("Подтверди тот же код в разделе «Мои устройства» на Mac. Пока подтверждения нет, команды и данные Mac недоступны.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Button("Проверить подтверждение") { Task { await model.refreshMac() } }
                }
                TextField("Ссылка из QR-приглашения Mac", text: $pairInvitation, axis: .vertical)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .accessibilityLabel("Ссылка приглашения Mac")
                TextField("SHA-256 отпечаток центра TLS с Mac", text: $pairFingerprint)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.asciiCapable)
                    .accessibilityLabel("Отпечаток TLS центра сертификации Mac")
                HStack {
                    Button("Сканировать QR") {
                        Task {
                            let granted = await AVCaptureDevice.requestAccess(for: .video)
                            if granted && DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                                showingScanner = true
                            } else {
                                pairMessage = "Разреши камеру для сканирования или вставь ссылку приглашения вручную."
                            }
                        }
                    }
                    .disabled(!DataScannerViewController.isSupported)
                    Button("Сопрячь с Mac") {
                        Task { await model.pairWithMac(invitation: pairInvitation, fingerprint: pairFingerprint) }
                    }
                    .disabled(pairInvitation.isEmpty || pairFingerprint.isEmpty)
                }
                Text("В панели сопряжения Mac сначала запусти локальный TLS-сервер, создай одноразовое приглашение и сравни SHA-256 отпечаток сертификата. Подключение работает в одной защищённой локальной сети; порты роутера не открываются.")
                    .font(.footnote).foregroundStyle(.secondary)
                if model.macTrusted {
                    Button("Запросить разрешённые данные Mac", systemImage: "arrow.down.doc") {
                        Task { await model.syncFromMac() }
                    }
                    Text("Этот запрос не запускается автоматически. Mac возвращает только категории, разрешённые на Mac; полученная копия сохраняется здесь и не отправляется в CloudKit.")
                        .font(.footnote).foregroundStyle(.secondary)
                    Button("Забыть сопряжение на этом устройстве", role: .destructive) { showingForgetMac = true }
                }
                if let pairMessage { Text(pairMessage).font(.footnote).foregroundStyle(.secondary) }
                if let date = model.database.macCacheUpdatedAt {
                    Text("Локальная копия Mac · \(date.formatted(date: .abbreviated, time: .shortened))")
                        .font(.footnote).foregroundStyle(.secondary)
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
        .task { await model.refreshMac() }
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
