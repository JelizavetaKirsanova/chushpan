import Combine
import Foundation
import UIKit

enum ChatExecutionTarget: String, CaseIterable, Identifiable {
    case thisDevice = "Этот iPhone/iPad"
    case mac = "Mac"
    var id: String { rawValue }
}

enum MacActionState {
    case submitted, running, completed, failed, confirmationRequired, unknown

    var title: String {
        switch self {
        case .submitted: "Команда отправлена"
        case .running: "Выполняется на Mac"
        case .completed: "Выполнено"
        case .failed: "Ошибка"
        case .confirmationRequired: "Нужно подтверждение"
        case .unknown: "Результат неизвестен"
        }
    }
}

struct MacActionFeedback {
    var state: MacActionState
    var message: String
    var commandID: String?
}

@MainActor
final class AppModel: ObservableObject {
    @Published var database: MobileDatabase
    @Published var selectedConversationID: String?
    @Published var composer = ""
    @Published var isGenerating = false
    @Published var includeRelevantMemory: Bool
    @Published var speakResponses: Bool
    @Published var voiceIdentifier: String
    @Published var hasAPIKey = false
    @Published var storageError: String?
    @Published var cloudSyncEnabled: Bool
    @Published var cloudSyncStatus = "Облачная синхронизация выключена."
    @Published var isCloudSyncing = false
    @Published var macBaseURL: String
    @Published var macRemoteURL: String
    @Published var macFingerprint: String
    @Published var macPairCode: String?
    @Published var macTrusted: Bool
    @Published var macAutoReconnectEnabled: Bool
    @Published var macStatus: MacRemoteStatus?
    @Published var macConnectionMessage = "Mac не сопряжён."
    @Published var macDiscoveryMessage = "Ищу Чушпан через Bonjour…"
    @Published var discoveredMacs: [DiscoveredMac] = []
    @Published var macLastConnectedAt: Date?
    @Published var macConnectionKind = "Локальная сеть"
    @Published var macActionFeedback: MacActionFeedback?
    @Published var macConfirmationCode = ""

    let speech = SpeechService()
    private let store: MobileStore
    private let keychain = KeychainVault()
    private let gemini = GeminiClient()
#if !CHUSHPAN_PERSONAL_DEBUG
    private let cloud = CloudSyncService()
#endif
    private let macBrowser = MacServiceBrowser()
    private var generationTask: Task<Void, Never>?
    private var localSaveTask: Task<Void, Never>?
    private var cloudSaveTask: Task<Void, Never>?
    private var cloudSyncTask: Task<Void, Never>?
    private var cloudSyncRequestedAgain = false
    private var cloudSyncGeneration = 0
    private var macClient: MacBridgeClient?
    private var reconnectTask: Task<Void, Never>?
    private var pairingInProgress = false
    private var sceneIsActive = true

    init() {
        includeRelevantMemory = UserDefaults.standard.bool(forKey: "mobile.includeRelevantMemory")
        speakResponses = UserDefaults.standard.bool(forKey: "mobile.speakResponses")
        voiceIdentifier = UserDefaults.standard.string(forKey: "mobile.voiceIdentifier") ?? ""
#if CHUSHPAN_PERSONAL_DEBUG
        cloudSyncEnabled = false
        cloudSyncStatus = "Синхронизация iCloud отключена в локальной сборке."
#else
        cloudSyncEnabled = UserDefaults.standard.bool(forKey: "mobile.cloudSyncEnabled")
#endif
        macBaseURL = UserDefaults.standard.string(forKey: "mobile.macBaseURL") ?? ""
        macRemoteURL = UserDefaults.standard.string(forKey: "mobile.macRemoteURL") ?? ""
        macFingerprint = UserDefaults.standard.string(forKey: "mobile.macFingerprint") ?? ""
        macTrusted = UserDefaults.standard.bool(forKey: "mobile.macTrusted")
        macAutoReconnectEnabled = UserDefaults.standard.object(forKey: "mobile.macAutoReconnect") as? Bool ?? true
        let lastConnected = UserDefaults.standard.double(forKey: "mobile.macLastConnectedAt")
        macLastConnectedAt = lastConnected > 0 ? Date(timeIntervalSince1970: lastConnected) : nil
        let resolvedStore: MobileStore
        do {
            let applicationStore = try MobileStore.applicationDefault()
            database = try applicationStore.load()
            resolvedStore = applicationStore
        } catch {
            storageError = error.localizedDescription
            let fallback = FileManager.default.temporaryDirectory
                .appendingPathComponent("chushpan-storage-unavailable.json")
            resolvedStore = MobileStore(fileURL: fallback)
            database = MobileDatabase()
        }
        store = resolvedStore
        hasAPIKey = (try? keychain.read("gemini-api-key")) != nil
        if cloudSyncEnabled {
            cloudSyncStatus = "Синхронизация iCloud включена. Проверяю доступность…"
            requestCloudSync()
        }
        macBrowser.onChange = { [weak self] services in
            guard let self else { return }
            self.discoveredMacs = services
            if self.macTrusted && self.macAutoReconnectEnabled && self.sceneIsActive {
                self.scheduleMacReconnect()
            }
        }
        macBrowser.onMessage = { [weak self] message in self?.macDiscoveryMessage = message }
        macBrowser.start()
        if macTrusted, !macBaseURL.isEmpty, !macFingerprint.isEmpty {
            Task { await refreshMac() }
        }
    }

    var activeConversation: MobileConversation? {
        database.conversations.first(where: { $0.id == selectedConversationID && $0.deletedAt == nil })
    }

    var visibleConversations: [MobileConversation] {
        database.conversations.filter { $0.deletedAt == nil }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    var visibleFacts: [MobileMemoryFact] {
        database.facts.filter { $0.deletedAt == nil }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    var visibleMedia: [MobileMediaItem] {
        database.media.filter { $0.deletedAt == nil }
            .sorted { $0.updatedAt > $1.updatedAt }
    }

    var visibleLightPresets: [MobileLightPreset] {
        database.lightPresets.filter { $0.deletedAt == nil }
            .sorted { $0.isFavorite == $1.isFavorite ? $0.name < $1.name : $0.isFavorite }
    }

    var continueWatching: MobileMediaItem? {
        visibleMedia.filter { $0.status == .watching && $0.positionSeconds > 0 }
            .max { $0.updatedAt < $1.updatedAt }
    }

    var appearance: String { database.sharedSettings.appearance }
    var selectedModel: String { database.sharedSettings.model }

    func createConversation() {
        var conversation = MobileConversation()
        conversation.modifiedBy = database.deviceID
        updateDatabase { $0.conversations.append(conversation) }
        selectedConversationID = conversation.id
    }

    func selectConversation(_ id: String) { selectedConversationID = id }

    func renameConversation(_ id: String, title: String) {
        let clean = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        updateDatabase { data in
            guard let index = data.conversations.firstIndex(where: { $0.id == id }) else { return }
            data.conversations[index].title = String(clean.prefix(100))
            stamp(&data.conversations[index], deviceID: data.deviceID)
        }
    }

    func setConversationPrivate(_ id: String, isPrivate: Bool) {
        updateDatabase { data in
            guard let index = data.conversations.firstIndex(where: { $0.id == id }) else { return }
            data.conversations[index].isPrivate = isPrivate
            stamp(&data.conversations[index], deviceID: data.deviceID)
        }
    }

    func deleteConversation(_ id: String) {
        updateDatabase { data in
            guard let index = data.conversations.firstIndex(where: { $0.id == id }) else { return }
            data.conversations[index].deletedAt = .now
            stamp(&data.conversations[index], deviceID: data.deviceID)
        }
        if selectedConversationID == id { selectedConversationID = nil }
    }

    func sendMessage() {
        let text = composer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isGenerating else { return }
        if activeConversation == nil { createConversation() }
        guard let id = selectedConversationID,
              let index = database.conversations.firstIndex(where: { $0.id == id }) else { return }
        let userMessage = ChatMessage(role: .user, text: text, delivery: .complete)
        var assistant = ChatMessage(role: .assistant, text: "", delivery: .streaming)
        assistant.errorMessage = nil
        let userID = userMessage.id
        assistant.replyToMessageID = userID
        let assistantID = assistant.id
        updateDatabase { data in
            var conversation = data.conversations[index]
            if conversation.title == "Новый диалог" {
                conversation.title = String(text.prefix(48))
            }
            conversation.messages.append(userMessage)
            conversation.messages.append(assistant)
            stamp(&conversation, deviceID: data.deviceID)
            data.conversations[index] = conversation
        }
        composer = ""
        runReply(conversationID: id, userID: userID, assistantID: assistantID)
    }

    func retryReply(_ assistantID: String) {
        guard !isGenerating,
              let conversation = activeConversation,
              let assistant = conversation.messages.first(where: { $0.id == assistantID }),
              let userID = assistant.replyToMessageID else { return }
        updateMessage(conversationID: conversation.id, messageID: assistantID) { message in
            message.text = ""
            message.delivery = .streaming
            message.errorMessage = nil
        }
        runReply(conversationID: conversation.id, userID: userID, assistantID: assistantID)
    }

    func cancelGeneration() {
        generationTask?.cancel()
        speech.stopListening()
    }

    func clearComposer() { composer = "" }

    func saveAPIKey(_ raw: String) throws {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, value.count <= 512 else { return }
        try keychain.write(Data(value.utf8), account: "gemini-api-key")
        hasAPIKey = true
    }

    func clearAPIKey() throws {
        try keychain.remove("gemini-api-key")
        hasAPIKey = false
    }

    func setModel(_ model: String) {
        guard ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"].contains(model) else { return }
        updateDatabase { data in
            data.sharedSettings.model = model
            data.sharedSettings.updatedAt = .now
            data.sharedSettings.modifiedBy = data.deviceID
        }
    }

    func setAppearance(_ value: String) {
        guard ["system", "light", "dark"].contains(value) else { return }
        updateDatabase { data in
            data.sharedSettings.appearance = value
            data.sharedSettings.updatedAt = .now
            data.sharedSettings.modifiedBy = data.deviceID
        }
    }

    func setIncludeRelevantMemory(_ value: Bool) {
        includeRelevantMemory = value
        UserDefaults.standard.set(value, forKey: "mobile.includeRelevantMemory")
    }

    func setSpeakResponses(_ value: Bool) {
        speakResponses = value
        UserDefaults.standard.set(value, forKey: "mobile.speakResponses")
    }

    func setVoiceIdentifier(_ value: String) {
        voiceIdentifier = value
        UserDefaults.standard.set(value, forKey: "mobile.voiceIdentifier")
    }

    func addFact(content: String, category: String, isPrivate: Bool) {
        let clean = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        var fact = MobileMemoryFact(content: String(clean.prefix(2000)), category: String(category.prefix(80)), isPrivate: isPrivate)
        fact.modifiedBy = database.deviceID
        updateDatabase { $0.facts.append(fact) }
    }

    func updateFact(_ fact: MobileMemoryFact) {
        updateDatabase { data in
            guard let index = data.facts.firstIndex(where: { $0.id == fact.id }) else { return }
            var updated = fact
            updated.content = String(updated.content.trimmingCharacters(in: .whitespacesAndNewlines).prefix(2000))
            updated.category = String(updated.category.prefix(80))
            stamp(&updated, deviceID: data.deviceID)
            data.facts[index] = updated
        }
    }

    func deleteFact(_ id: String) {
        updateDatabase { data in
            guard let index = data.facts.firstIndex(where: { $0.id == id }) else { return }
            data.facts[index].deletedAt = .now
            stamp(&data.facts[index], deviceID: data.deviceID)
        }
    }

    func addMedia(_ value: MobileMediaItem) {
        var item = value
        item.title = String(item.title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(240))
        guard !item.title.isEmpty else { return }
        item.modifiedBy = database.deviceID
        item.updatedAt = .now
        updateDatabase { $0.media.append(item) }
    }

    func updateMedia(_ value: MobileMediaItem) {
        updateDatabase { data in
            guard let index = data.media.firstIndex(where: { $0.id == value.id }) else { return }
            var item = value
            item.title = String(item.title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(240))
            item.positionSeconds = max(0, min(item.positionSeconds, 604800))
            item.durationSeconds = max(0, min(item.durationSeconds, 604800))
            if let rating = item.rating { item.rating = max(1, min(rating, 10)) }
            stamp(&item, deviceID: data.deviceID)
            data.media[index] = item
        }
    }

    func deleteMedia(_ id: String) {
        updateDatabase { data in
            guard let index = data.media.firstIndex(where: { $0.id == id }) else { return }
            data.media[index].deletedAt = .now
            stamp(&data.media[index], deviceID: data.deviceID)
        }
    }

    func importMacProgress(_ progress: MacMediaProgress) {
        if var item = visibleMedia.first(where: { $0.title.localizedCaseInsensitiveCompare(progress.title) == .orderedSame }) {
            item.kind = .series
            item.status = progress.watched ? .watched : .watching
            item.season = progress.season
            item.episode = progress.episode
            item.positionSeconds = max(0, progress.position)
            item.durationSeconds = max(0, progress.duration)
            updateMedia(item)
        } else {
            var item = MobileMediaItem(title: progress.title, kind: .series, status: progress.watched ? .watched : .watching)
            item.season = progress.season
            item.episode = progress.episode
            item.positionSeconds = max(0, progress.position)
            item.durationSeconds = max(0, progress.duration)
            addMedia(item)
        }
    }

    func addLightPreset(name: String, rgb: RGBColor, brightness: Int, favorite: Bool = false) {
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        var item = MobileLightPreset(name: String(clean.prefix(80)), rgb: rgb, brightness: max(0, min(brightness, 100)), isFavorite: favorite)
        item.modifiedBy = database.deviceID
        updateDatabase { $0.lightPresets.append(item) }
    }

    func deleteLightPreset(_ id: String) {
        updateDatabase { data in
            guard let index = data.lightPresets.firstIndex(where: { $0.id == id }) else { return }
            data.lightPresets[index].deletedAt = .now
            stamp(&data.lightPresets[index], deviceID: data.deviceID)
        }
    }

    func toggleLightFavorite(_ id: String) {
        updateDatabase { data in
            guard let index = data.lightPresets.firstIndex(where: { $0.id == id }) else { return }
            data.lightPresets[index].isFavorite.toggle()
            stamp(&data.lightPresets[index], deviceID: data.deviceID)
        }
    }

    func enableCloudSync() {
#if CHUSHPAN_PERSONAL_DEBUG
        cloudSyncEnabled = false
        cloudSyncStatus = "Синхронизация iCloud отключена в локальной сборке."
        return
#else
        cloudSyncEnabled = true
        UserDefaults.standard.set(true, forKey: "mobile.cloudSyncEnabled")
        cloudSyncStatus = "iCloud включён. Синхронизирую выбранные данные с личной базой iCloud."
        requestCloudSync()
#endif
    }

    func disableCloudSync() {
        cloudSyncEnabled = false
        UserDefaults.standard.set(false, forKey: "mobile.cloudSyncEnabled")
        cloudSaveTask?.cancel()
        cloudSyncRequestedAgain = false
        cloudSyncGeneration += 1
        cloudSyncTask?.cancel()
        cloudSyncStatus = "Синхронизация выключена. Существующая копия в iCloud не удалена."
    }

    func requestCloudSync() {
#if CHUSHPAN_PERSONAL_DEBUG
        return
#else
        guard cloudSyncEnabled else { return }
        if isCloudSyncing || cloudSyncTask != nil {
            cloudSyncRequestedAgain = true
            return
        }
        cloudSyncTask = Task { await synchronizeCloud() }
#endif
    }

#if !CHUSHPAN_PERSONAL_DEBUG
    private func synchronizeCloud() async {
        guard cloudSyncEnabled, !isCloudSyncing else { return }
        isCloudSyncing = true
        let generation = cloudSyncGeneration
        cloudSyncStatus = "Синхронизирую диалоги, факты, медиатеку и общие настройки…"
        defer {
            isCloudSyncing = false
            cloudSyncTask = nil
            if cloudSyncRequestedAgain && cloudSyncEnabled {
                cloudSyncRequestedAgain = false
                requestCloudSync()
            }
        }
        do {
            let local = try database.cloudItems()
            let merged = try await cloud.synchronize(local: local)
            try Task.checkCancellation()
            guard cloudSyncEnabled, generation == cloudSyncGeneration else { return }
            var updated = database
            try updated.applyCloudItems(merged)
            database = updated
            try store.save(updated)
            cloudSyncStatus = "Синхронизация завершена · \(Date.now.formatted(date: .abbreviated, time: .shortened))"
        } catch is CancellationError {
            if cloudSyncEnabled && generation == cloudSyncGeneration {
                cloudSyncStatus = "Синхронизация остановлена."
            }
        } catch {
            if cloudSyncEnabled && generation == cloudSyncGeneration {
                cloudSyncStatus = error.localizedDescription
            }
        }
    }
#endif

    func pairWithMac(invitation: String, fingerprint: String) async {
        macConnectionMessage = "Проверяю TLS-сертификат и одноразовое приглашение…"
        do {
            let name = UIDevice.current.model.contains("iPad") ? "iPad" : "iPhone"
            let kind = name == "iPad" ? "ipad" : "iphone"
            let (client, code, _) = try await MacBridgeClient.pair(
                invitation: invitation,
                fingerprint: fingerprint,
                deviceName: name,
                kind: kind
            )
            macClient?.close()
            macClient = client
            macBaseURL = Self.baseAddress(from: invitation)
            macFingerprint = fingerprint.filter(\.isHexDigit).lowercased()
            macPairCode = code
            macTrusted = false
            UserDefaults.standard.set(macBaseURL, forKey: "mobile.macBaseURL")
            UserDefaults.standard.set(macFingerprint, forKey: "mobile.macFingerprint")
            UserDefaults.standard.set(false, forKey: "mobile.macTrusted")
            UserDefaults.standard.set(true, forKey: "mobile.macAutoReconnect")
            macAutoReconnectEnabled = true
            macConnectionMessage = "Сверь код с Mac и подтверди новое устройство в его панели сопряжения."
            pollPairingApproval()
        } catch {
            macConnectionMessage = error.localizedDescription
        }
    }

    func pairWithMac(_ device: DiscoveredMac) async {
        guard !pairingInProgress else { return }
        pairingInProgress = true
        defer { pairingInProgress = false }
        macConnectionMessage = "Устанавливаю защищённую связь с \(device.name)…"
        do {
            let name = UIDevice.current.model.contains("iPad") ? "iPad" : "iPhone"
            let kind = name == "iPad" ? "ipad" : "iphone"
            let (client, code, _, fingerprint) = try await MacBridgeClient.pair(
                baseURL: device.localURL,
                deviceName: name,
                kind: kind
            )
            macClient?.close()
            macClient = client
            macBaseURL = device.localURL
            macRemoteURL = device.remoteURL ?? ""
            macFingerprint = fingerprint
            macPairCode = code
            macTrusted = false
            UserDefaults.standard.set(macBaseURL, forKey: "mobile.macBaseURL")
            UserDefaults.standard.set(macRemoteURL, forKey: "mobile.macRemoteURL")
            UserDefaults.standard.set(macFingerprint, forKey: "mobile.macFingerprint")
            UserDefaults.standard.set(false, forKey: "mobile.macTrusted")
            UserDefaults.standard.set(true, forKey: "mobile.macAutoReconnect")
            macAutoReconnectEnabled = true
            macConnectionMessage = "Сравни код на Mac и iPhone/iPad. Подтверждение нужно только для этого нового устройства."
            pollPairingApproval()
        } catch {
            macConnectionMessage = error.localizedDescription
        }
    }

    func connectToMac(_ device: DiscoveredMac) async {
        if !macTrusted {
            await pairWithMac(device)
            return
        }
        let candidates = [device.localURL, device.remoteURL].compactMap { $0 }
        for address in candidates {
            do {
                let client = try await MacBridgeClient.restore(
                    baseURL: address,
                    fingerprint: macFingerprint
                )
                let status = try await client.status()
                macClient?.close()
                macClient = client
                macStatus = status
                macBaseURL = address
                recordMacConnection(at: address)
                macConnectionMessage = "Соединение с Mac восстановлено."
                return
            } catch {
                macConnectionMessage = error.localizedDescription
            }
        }
    }

    func pairWithMac(address: String) async {
        let baseURL = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !baseURL.isEmpty, !pairingInProgress else { return }
        if macTrusted {
            do {
                let client = try await MacBridgeClient.restore(baseURL: baseURL, fingerprint: macFingerprint)
                let status = try await client.status()
                macClient?.close()
                macClient = client
                macBaseURL = baseURL
                macRemoteURL = baseURL
                macStatus = status
                UserDefaults.standard.set(baseURL, forKey: "mobile.macBaseURL")
                UserDefaults.standard.set(baseURL, forKey: "mobile.macRemoteURL")
                recordMacConnection(at: baseURL)
                macConnectionMessage = "Соединение с Mac восстановлено по настроенному адресу."
            } catch {
                macConnectionMessage = error.localizedDescription
            }
            return
        }
        pairingInProgress = true
        defer { pairingInProgress = false }
        macConnectionMessage = "Проверяю защищённый адрес Mac…"
        do {
            let name = UIDevice.current.model.contains("iPad") ? "iPad" : "iPhone"
            let kind = name == "iPad" ? "ipad" : "iphone"
            let (client, code, _, fingerprint) = try await MacBridgeClient.pair(
                baseURL: baseURL,
                deviceName: name,
                kind: kind
            )
            macClient?.close()
            macClient = client
            macBaseURL = baseURL
            macRemoteURL = baseURL
            macFingerprint = fingerprint
            macPairCode = code
            macTrusted = false
            UserDefaults.standard.set(macBaseURL, forKey: "mobile.macBaseURL")
            UserDefaults.standard.set(macRemoteURL, forKey: "mobile.macRemoteURL")
            UserDefaults.standard.set(macFingerprint, forKey: "mobile.macFingerprint")
            UserDefaults.standard.set(false, forKey: "mobile.macTrusted")
            UserDefaults.standard.set(true, forKey: "mobile.macAutoReconnect")
            macAutoReconnectEnabled = true
            macConnectionMessage = "Сравни код на Mac и iPhone/iPad, затем подтверди сопряжение на Mac."
            pollPairingApproval()
        } catch {
            macConnectionMessage = error.localizedDescription
        }
    }

    func refreshMac() async {
        if macClient == nil {
            guard macTrusted || macPairCode != nil else {
                macConnectionMessage = "Выбери найденный Mac, чтобы подключиться."
                return
            }
            let candidates = connectionCandidates()
            var restored: MacBridgeClient?
            var lastError: Error = MacBridgeError.transport
            for candidate in candidates {
                do {
                    restored = try await MacBridgeClient.restore(
                        baseURL: candidate,
                        fingerprint: macFingerprint
                    )
                    macBaseURL = candidate
                    break
                } catch { lastError = error }
            }
            guard let restored else {
                macConnectionMessage = lastError.localizedDescription
                macStatus = nil
                return
            }
            macClient = restored
        }
        guard let client = macClient else { return }
        do {
            if !macTrusted {
                let pairing = try await client.pairStatus()
                if pairing.status == "pending" {
                    macPairCode = pairing.code
                    macConnectionMessage = "Жду подтверждения этого кода на Mac."
                    return
                }
                guard pairing.status == "trusted" else { throw MacBridgeError.notPaired }
                macTrusted = true
                UserDefaults.standard.set(true, forKey: "mobile.macTrusted")
            }
            macStatus = try await client.status()
            recordMacConnection(at: macBaseURL)
            macConnectionMessage = "Mac на связи по защищённому соединению."
        } catch {
            macStatus = nil
            macConnectionMessage = error.localizedDescription
            if let bridgeError = error as? MacBridgeError, case .pairingEnded = bridgeError {
                macPairCode = nil
                macClient?.close()
                macClient = nil
            }
        }
    }

    func setSceneActive(_ active: Bool) {
        sceneIsActive = active
        if active {
            macBrowser.start()
            if macTrusted && macAutoReconnectEnabled { scheduleMacReconnect() }
        } else {
            macBrowser.stop()
            reconnectTask?.cancel()
            reconnectTask = nil
        }
    }

    func disconnectMac() {
        reconnectTask?.cancel()
        reconnectTask = nil
        macClient?.close()
        macClient = nil
        macStatus = nil
        macAutoReconnectEnabled = false
        UserDefaults.standard.set(false, forKey: "mobile.macAutoReconnect")
        macConnectionMessage = "Отключено. Нажми «Подключить» для следующего соединения."
    }

    func setMacAutoReconnect(_ enabled: Bool) {
        macAutoReconnectEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: "mobile.macAutoReconnect")
        if enabled { scheduleMacReconnect() }
        else {
            reconnectTask?.cancel()
            reconnectTask = nil
        }
    }

    private func pollPairingApproval() {
        Task { [weak self] in
            guard let self else { return }
            for _ in 0..<60 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled, !self.macTrusted, self.macPairCode != nil else { return }
                await self.refreshMac()
            }
            if !self.macTrusted {
                self.macConnectionMessage = "Сопряжение истекло. Подключи устройство снова, если запрос был отклонён."
                self.macPairCode = nil
            }
        }
    }

    private func scheduleMacReconnect() {
        guard macTrusted, macAutoReconnectEnabled, sceneIsActive else { return }
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            guard let self else { return }
            let delays: [UInt64] = [0, 1, 2, 4, 8, 16, 30]
            for delay in delays {
                if delay > 0 { try? await Task.sleep(nanoseconds: delay * 1_000_000_000) }
                guard !Task.isCancelled else { return }
                for candidate in self.connectionCandidates() {
                    do {
                        let client = try await MacBridgeClient.restore(
                            baseURL: candidate,
                            fingerprint: self.macFingerprint
                        )
                        let status = try await client.status()
                        self.macClient?.close()
                        self.macClient = client
                        self.macStatus = status
                        self.macBaseURL = candidate
                        self.recordMacConnection(at: candidate)
                        self.macConnectionMessage = "Соединение с Mac восстановлено."
                        return
                    } catch {
                        self.macStatus = nil
                    }
                }
            }
            if !Task.isCancelled {
                self.macConnectionMessage = "Mac пока недоступен. Повторю подключение при следующем обнаружении сети."
            }
        }
    }

    private func connectionCandidates() -> [String] {
        let local = discoveredMacs.map(\.localURL)
        let remote = discoveredMacs.compactMap(\.remoteURL)
        var result: [String] = []
        for candidate in local + remote + [macRemoteURL, macBaseURL] where
            !candidate.isEmpty && !result.contains(candidate) {
            result.append(candidate)
        }
        return result
    }

    private func recordMacConnection(at address: String) {
        macLastConnectedAt = Date()
        UserDefaults.standard.set(macLastConnectedAt?.timeIntervalSince1970, forKey: "mobile.macLastConnectedAt")
        UserDefaults.standard.set(address, forKey: "mobile.macBaseURL")
        macConnectionKind = isTailscaleAddress(address) ? "Удалённая сеть Tailscale" : "Локальная сеть Bonjour"
    }

    private func isTailscaleAddress(_ value: String) -> Bool {
        guard let host = URLComponents(string: value)?.host else { return false }
        let octets = host.split(separator: ".").compactMap { UInt8($0) }
        if octets.count == 4 {
            return octets[0] == 100 && (64...127).contains(Int(octets[1]))
        }
        return host.localizedCaseInsensitiveContains("ts.net")
    }

    func syncFromMac() async {
        guard macTrusted, let macClient else {
            macConnectionMessage = "Сначала подключи и подтверди Mac."
            return
        }
        macConnectionMessage = "Запрашиваю только категории, разрешённые на Mac…"
        do {
            let response = try await macClient.synchronize()
            updateDatabase(scheduleCloud: false) { data in
                data.macMemoryCache = response.data.memory ?? []
                data.macMediaCache = response.data.mediaProgress ?? []
                data.macCacheUpdatedAt = Date(timeIntervalSince1970: response.serverTime)
            }
            macConnectionMessage = "Получены разрешённые данные Mac. Они сохранены локально и не отправлены в iCloud."
        } catch {
            macConnectionMessage = error.localizedDescription
        }
    }

    func forgetMacOnThisDevice() {
        reconnectTask?.cancel()
        reconnectTask = nil
        macClient?.close()
        macClient = nil
        try? keychain.remove("mac-device-signing-key")
        macBaseURL = ""
        macRemoteURL = ""
        macFingerprint = ""
        macStatus = nil
        macPairCode = nil
        macTrusted = false
        macAutoReconnectEnabled = true
        UserDefaults.standard.removeObject(forKey: "mobile.macBaseURL")
        UserDefaults.standard.removeObject(forKey: "mobile.macRemoteURL")
        UserDefaults.standard.removeObject(forKey: "mobile.macFingerprint")
        UserDefaults.standard.set(false, forKey: "mobile.macTrusted")
        UserDefaults.standard.set(true, forKey: "mobile.macAutoReconnect")
        macConnectionMessage = "Данные сопряжения удалены с этого устройства. Отзови устройство в панели Mac, чтобы удалить его доверие там."
    }

    func runMacScenario(_ scenario: MacRemoteScenario, variant: String, values: [String: Any] = [:]) {
        performMacCommand("scenario", arguments: [
            "id": scenario.id,
            "revision": scenario.revision,
            "variant": variant,
            "values": values,
        ], label: "Сценарий «\(scenario.name)»")
    }

    func runMacLight(deviceID: String, rgb: RGBColor?, brightness: Int?, power: Bool?) {
        var arguments: [String: Any] = ["target": deviceID, "action": "led"]
        if let rgb { arguments["color"] = rgb.hex }
        if let brightness { arguments["brightness"] = max(0, min(brightness, 100)) }
        if let power { arguments["power"] = power }
        performMacCommand("device", arguments: arguments, label: "Подсветка Mac")
    }

    func runMacDevice(deviceID: String, action: String, parameters: [String: Any], label: String) {
        var arguments = parameters
        arguments["target"] = deviceID
        arguments["action"] = action
        performMacCommand("device", arguments: arguments, label: label)
    }

    func applyLightPreset(_ preset: MobileLightPreset, to deviceID: String) {
        runMacLight(deviceID: deviceID, rgb: preset.rgb, brightness: preset.brightness, power: true)
    }

    func confirmMacAction() {
        guard let client = macClient else { return }
        let code = macConfirmationCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else { return }
        macConfirmationCode = ""
        macActionFeedback = MacActionFeedback(state: .submitted, message: "Подтверждение отправлено на Mac.", commandID: nil)
        Task { await executeConfirm(client: client, code: code) }
    }

    func setComposerFromSpeech(_ value: String) { composer = value }

    private func runReply(conversationID: String, userID: String, assistantID: String) {
        guard let conversation = database.conversations.first(where: { $0.id == conversationID }),
              let user = conversation.messages.first(where: { $0.id == userID }) else { return }
        isGenerating = true
        persist()
        let target = chatTarget
        generationTask = Task { [weak self] in
            guard let self else { return }
            if target == .mac {
                await self.runMacChat(text: user.text, conversationID: conversationID, assistantID: assistantID)
            } else {
                await self.runLocalChat(conversation: conversation, conversationID: conversationID, assistantID: assistantID)
            }
        }
    }

    @Published var chatTarget: ChatExecutionTarget = .thisDevice

    private func runLocalChat(conversation: MobileConversation, conversationID: String, assistantID: String) async {
        defer { isGenerating = false; generationTask = nil }
        do {
            guard let keyData = try keychain.read("gemini-api-key"),
                  let key = String(data: keyData, encoding: .utf8) else { throw GeminiError.missingKey }
            let messages = conversation.messages
                .filter { $0.delivery == .complete && !$0.text.isEmpty }
                .map { GeminiInputMessage(role: $0.role, text: $0.text) }
            let memory = includeRelevantMemory && !conversation.isPrivate
                ? relevantMemory(for: messages.last?.text ?? "") : ""
            try await gemini.stream(
                key: key,
                model: selectedModel,
                messages: messages,
                memoryContext: memory
            ) { [weak self] delta in
                guard let self else { return }
                self.updateMessage(conversationID: conversationID, messageID: assistantID) {
                    $0.text += delta
                    $0.delivery = .streaming
                }
                self.scheduleSave()
            }
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .complete
                $0.errorMessage = nil
            }
            persist()
            if speakResponses, let message = findMessage(conversationID: conversationID, id: assistantID) {
                speech.speak(message.text, voiceIdentifier: voiceIdentifier.isEmpty ? nil : voiceIdentifier)
            }
        } catch is CancellationError {
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .cancelled
                $0.errorMessage = "Ответ остановлен."
            }
            persist()
        } catch {
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .failed
                $0.errorMessage = error.localizedDescription
            }
            persist()
        }
    }

    private func runMacChat(text: String, conversationID: String, assistantID: String) async {
        defer { isGenerating = false; generationTask = nil }
        guard macTrusted, let client = macClient, macStatus?.capabilities.contains("chat") == true else {
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .failed
                $0.errorMessage = "Mac недоступен или у него нет разрешения «Чат». Сообщение сохранено; локальный ИИ автоматически не запускается."
            }
            persist()
            return
        }
        do {
            let receipt = try await client.submit(operation: "chat", arguments: ["text": text])
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.text = ""
                $0.delivery = .streaming
                $0.errorMessage = "Запрос отправлен на Mac."
            }
            guard let id = receipt.id else { throw MacBridgeError.transport }
            var current = receipt
            let started = Date()
            while current.status == "running" && Date().timeIntervalSince(started) < 88 {
                try Task.checkCancellation()
                try await Task.sleep(for: .seconds(1.5))
                current = try await client.result(id: id)
            }
            if current.status == "completed", let answer = current.result?.message {
                updateMessage(conversationID: conversationID, messageID: assistantID) {
                    $0.text = answer
                    $0.delivery = .complete
                    $0.errorMessage = nil
                }
            } else {
                updateMessage(conversationID: conversationID, messageID: assistantID) {
                    $0.delivery = current.status == "running" ? .failed : .failed
                    $0.errorMessage = current.result?.error
                        ?? "Статус Mac: \(current.status). Переподключение не повторит запрос. Проверь журнал на Mac."
                }
            }
            persist()
        } catch is CancellationError {
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .cancelled
                $0.errorMessage = "Ожидание ответа остановлено. Уже отправленная команда продолжает выполняться на Mac."
            }
            persist()
        } catch {
            updateMessage(conversationID: conversationID, messageID: assistantID) {
                $0.delivery = .failed
                $0.errorMessage = "Результат неизвестен. \(error.localizedDescription) Проверь журнал Mac перед повтором."
            }
            persist()
        }
    }

    private func performMacCommand(_ operation: String, arguments: [String: Any], label: String) {
        guard macTrusted, let client = macClient else {
            macActionFeedback = MacActionFeedback(state: .failed, message: "Mac не сопряжён. Команда не отправлена.", commandID: nil)
            return
        }
        macActionFeedback = MacActionFeedback(state: .submitted, message: "Отправляю выбранную команду на Mac…", commandID: nil)
        Task {
            do {
                let receipt = try await client.submit(operation: operation, arguments: arguments)
                guard let id = receipt.id else { throw MacBridgeError.transport }
                macActionFeedback = MacActionFeedback(
                    state: receipt.status == "running" ? .running : .submitted,
                    message: "\(label): запрос принят Mac. Жду подтверждённый результат.",
                    commandID: id
                )
                await pollMacCommand(client: client, id: id, label: label, current: receipt)
            } catch {
                macActionFeedback = MacActionFeedback(
                    state: .unknown,
                    message: "\(label): результат неизвестен. \(error.localizedDescription) Не отправляла повторно.",
                    commandID: nil
                )
            }
        }
    }

    private func executeConfirm(client: MacBridgeClient, code: String) async {
        do {
            let receipt = try await client.confirm(code: code)
            guard let id = receipt.id else { throw MacBridgeError.transport }
            macActionFeedback = MacActionFeedback(state: .submitted, message: "Подтверждение принято; проверяю результат.", commandID: id)
            await pollMacCommand(client: client, id: id, label: "Подтверждённое действие", current: receipt)
        } catch {
            macActionFeedback = MacActionFeedback(state: .unknown, message: "Результат подтверждения неизвестен. \(error.localizedDescription)", commandID: nil)
        }
    }

    private func pollMacCommand(client: MacBridgeClient, id: String, label: String, current initial: MacCommandReceipt) async {
        var receipt = initial
        let started = Date()
        while receipt.status == "running" && Date().timeIntervalSince(started) < 88 {
            macActionFeedback = MacActionFeedback(state: .running, message: "\(label): Mac выполняет команду.", commandID: id)
            do {
                try await Task.sleep(for: .seconds(1.5))
                receipt = try await client.result(id: id)
            } catch {
                macActionFeedback = MacActionFeedback(state: .unknown, message: "Связь оборвалась во время выполнения. Проверь журнал Mac; повторной отправки нет.", commandID: id)
                return
            }
        }
        if receipt.status == "confirmation_required" {
            macActionFeedback = MacActionFeedback(
                state: .confirmationRequired,
                message: receipt.result?.message ?? "На Mac требуется свежий код подтверждения.",
                commandID: id
            )
        } else if receipt.status == "completed" {
            let resultStatus = receipt.result?.resultStatus
            let outcome = resultStatus == "success"
                ? "Выполнение подтверждено Mac."
                : resultStatus == "sent"
                    ? "Команда отправлена. Состояние устройства не подтверждено."
                    : (receipt.result?.message ?? "Mac завершил обработку, но физический эффект не проверен.")
            macActionFeedback = MacActionFeedback(state: .completed, message: outcome, commandID: id)
        } else if receipt.status == "failed" || receipt.status == "rejected" || receipt.status == "interrupted" {
            macActionFeedback = MacActionFeedback(state: .failed, message: receipt.result?.error ?? "Mac: \(receipt.status).", commandID: id)
        } else {
            macActionFeedback = MacActionFeedback(state: .unknown, message: "Статус Mac: \(receipt.status). Проверь журнал; команда автоматически не повторялась.", commandID: id)
        }
    }

    private func relevantMemory(for query: String) -> String {
        let terms = Set(query.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init).filter { $0.count > 2 })
        guard !terms.isEmpty else { return "" }
        let ranked = visibleFacts.filter { !$0.isPrivate }.map { fact -> (MobileMemoryFact, Int) in
            let text = (fact.category + " " + fact.content).lowercased()
            return (fact, terms.reduce(0) { $0 + (text.contains($1) ? 1 : 0) })
        }.filter { $0.1 > 0 }.sorted { $0.1 > $1.1 }.prefix(8)
        var total = 0
        return ranked.compactMap { fact, _ in
            let line = "[\(fact.category)] \(fact.content)"
            guard total + line.count <= 2400 else { return nil }
            total += line.count
            return line
        }.joined(separator: "\n")
    }

    private func updateMessage(conversationID: String, messageID: String, change: (inout ChatMessage) -> Void) {
        updateDatabase(scheduleCloud: false) { data in
            guard let conversationIndex = data.conversations.firstIndex(where: { $0.id == conversationID }),
                  let messageIndex = data.conversations[conversationIndex].messages.firstIndex(where: { $0.id == messageID }) else { return }
            change(&data.conversations[conversationIndex].messages[messageIndex])
            data.conversations[conversationIndex].messages[messageIndex].updatedAt = .now
            stamp(&data.conversations[conversationIndex], deviceID: data.deviceID)
        }
    }

    private func findMessage(conversationID: String, id: String) -> ChatMessage? {
        database.conversations.first(where: { $0.id == conversationID })?.messages.first(where: { $0.id == id })
    }

    private func updateDatabase(scheduleCloud: Bool = true, _ change: (inout MobileDatabase) -> Void) {
        var next = database
        change(&next)
        database = next
        persist(scheduleCloud: scheduleCloud)
    }

    private func persist(scheduleCloud: Bool = true) {
        guard storageError == nil else { return }
        do { try store.save(database) }
        catch { storageError = error.localizedDescription; return }
        if scheduleCloud, cloudSyncEnabled { scheduleCloudSync() }
    }

    private func scheduleSave() {
        localSaveTask?.cancel()
        localSaveTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            self?.persist(scheduleCloud: false)
        }
    }

    private func scheduleCloudSync() {
        cloudSaveTask?.cancel()
        cloudSaveTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            guard let self, !Task.isCancelled else { return }
            self.requestCloudSync()
        }
    }

    private func stamp(_ value: inout MobileConversation, deviceID: String) { value.updatedAt = .now; value.modifiedBy = deviceID }
    private func stamp(_ value: inout MobileMemoryFact, deviceID: String) { value.updatedAt = .now; value.modifiedBy = deviceID }
    private func stamp(_ value: inout MobileMediaItem, deviceID: String) { value.updatedAt = .now; value.modifiedBy = deviceID }
    private func stamp(_ value: inout MobileLightPreset, deviceID: String) { value.updatedAt = .now; value.modifiedBy = deviceID }

    private static func baseAddress(from invitation: String) -> String {
        guard var components = URLComponents(string: invitation) else { return "" }
        components.fragment = nil
        components.query = nil
        components.path = ""
        return components.url?.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
    }
}
