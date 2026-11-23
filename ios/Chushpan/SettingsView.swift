import AVFoundation
import SwiftUI
import VisionKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var pairInvitation = ""
    @State private var coreInvitationJSON = ""
    @State private var pairFingerprint = ""
    @State private var remoteAddress = ""
    @State private var pairMessage: String?
    @State private var showingCloudConsent = false
    @State private var showingScanner = false
    @State private var showingForgetMac = false

    var body: some View {
        Form {
            Section {
                MobilePageHeader(
                    eyebrow: "Чушпан на iPhone",
                    title: "Настройки",
                    subtitle: "GPT, голос, сопряжение, оформление и локальное хранение",
                    symbol: "gearshape.fill"
                )
                .listRowInsets(EdgeInsets(top: 10, leading: 16, bottom: 10, trailing: 16))
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
            }

            Section {
                DisclosureGroup("ИИ и подключение") {
                    Label("GPT через сервер Чушпана", systemImage: "brain.head.profile")
                        .font(.headline)
                    if let chat = model.macChatStatus {
                        LabeledContent("Мобильный провайдер", value: "\(chat.provider.uppercased()) · \(chat.model)")
                        LabeledContent("Резервная модель", value: chat.fallback ? "Включена" : "Выключена")
                        LabeledContent("Провайдер macOS", value: chat.desktopProvider?.uppercased() ?? "Не сообщён")
                    } else {
                        Text(model.coreAPIState == .connected
                             ? "В автономном режиме GPT может идти через защищённый Core API."
                             : "GPT работает через Mac, если он подключён. Для автономного GPT настрой Core API ниже.")
                            .foregroundStyle(.secondary)
                    }
                    if let last = model.visibleConversations
                        .flatMap(\.messages)
                        .last(where: { $0.role == .assistant && $0.provider != nil && $0.model != nil }) {
                        LabeledContent("Последний ответ", value: "\(last.provider!.uppercased()) · \(last.model!)")
                    }
                    Text("При подключённом Mac чат использует его провайдер. Если Mac недоступен, отдельный маршрут Core API отправляет только текст диалога; команды Mac через него не выполняются. Ключ GPT хранится на сервере.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }

            Section("GPT без Mac · Core API") {
                LabeledContent("Состояние", value: model.coreAPIState.title)
                if let provider = model.coreAPIProvider {
                    LabeledContent("Провайдер", value: provider.uppercased())
                }
                if model.coreAPIState == .connected {
                    LabeledContent("GPT на сервере", value: model.coreAPIConfigured ? "Настроен" : "Не настроен")
                }
                if let last = model.coreAPILastConnectedAt {
                    LabeledContent("Последнее соединение", value: last.formatted(date: .abbreviated, time: .shortened))
                }
                Text(model.coreAPIMessage).font(.footnote).foregroundStyle(.secondary)
                TextField("Одноразовое приглашение Core API в JSON", text: $coreInvitationJSON, axis: .vertical)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.footnote.monospaced())
                    .lineLimit(2...5)
                    .accessibilityLabel("Одноразовое приглашение Core API")
                HStack {
                    Button("Сопрячь Core API") {
                        Task { await model.pairWithCoreAPI(invitationJSON: coreInvitationJSON) }
                    }
                    .disabled(coreInvitationJSON.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("Проверить") { Task { await model.refreshCoreAPIStatus() } }
                        .disabled(model.coreAPIState == .unconfigured)
                    if model.coreAPIState != .unconfigured {
                        Button("Забыть", role: .destructive) { model.forgetCoreAPI() }
                    }
                }
                Text("Приглашение создаётся администратором Core API в Tailscale и должно содержать только разрешение llm.chat. Оно одноразовое и не сохраняется приложением. Настройка сервера и его GPT-ключа выполняется отдельно; ключ не вводится на iPhone.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            Section("Приватность ИИ") {
                Text("История и пользовательские данные остаются локально на iPhone и доступны офлайн. Запрос к GPT отправляет текст и ограниченный контекст диалога выбранному серверу. Автономный маршрут Core API не получает разрешений на память или команды Mac.")
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

#if CHUSHPAN_PERSONAL_DEBUG
            Section("Синхронизация iCloud") {
                Text("Синхронизация iCloud отключена в локальной сборке. Обычная Debug- и Release-сборки сохраняют CloudKit.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
#else
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
#endif

            Section("Подключение к Чушпану") {
                if model.macTrusted {
                    Label(
                        model.macStatus.map { "\($0.name ?? "Mac") — Подключено" } ?? model.macConnectionState.title,
                        systemImage: model.macStatus == nil ? model.macConnectionState.symbol : "lock.shield.fill"
                    )
                    .foregroundStyle(model.macConnectionState == .connected ? Color.green : Color.secondary)
                    Text(model.macStatus == nil ? model.macConnectionMessage : model.macConnectionKind)
                        .font(.footnote).foregroundStyle(.secondary)
                    if !model.macAutoReconnectEnabled {
                        Text("Автопереподключение выключено. Проверка вручную доступна кнопкой ниже.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
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
                Text("История и пользовательские настройки сохраняются в Application Support; разрешённые данные Mac кэшируются для автономного чтения и не отправляются в iCloud. API-ключ хранится только на сервере Mac, закрытый ключ сопряжения — в Keychain этого устройства.")
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

}
