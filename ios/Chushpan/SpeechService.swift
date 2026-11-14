import AVFoundation
import Combine
import Foundation
import Speech

@MainActor
final class SpeechService: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published private(set) var isListening = false
    @Published private(set) var isSpeaking = false
    @Published var transcript = ""
    @Published var statusMessage: String?

    private let engine = AVAudioEngine()
    private let synthesizer = AVSpeechSynthesizer()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var recognizer = SFSpeechRecognizer(locale: Locale(identifier: "ru-RU"))
    private var partialHandler: ((String) -> Void)?
    private var interruptionToken: NSObjectProtocol?

    override init() {
        super.init()
        synthesizer.delegate = self
        interruptionToken = NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.stopListening()
                self?.stopSpeaking()
                self?.statusMessage = "Аудиосеанс прерван. Нажми голосовую кнопку ещё раз."
            }
        }
    }

    deinit {
        if let interruptionToken { NotificationCenter.default.removeObserver(interruptionToken) }
    }

    func startListening(onPartial: @escaping (String) -> Void) async {
        guard !isListening else { return }
        stopSpeaking()
        statusMessage = nil
        transcript = ""
        partialHandler = onPartial
        do {
            let speechAllowed = await requestSpeechPermission()
            guard speechAllowed else {
                statusMessage = "Разреши распознавание речи в настройках iPhone/iPad."
                return
            }
            let microphoneAllowed = await requestMicrophonePermission()
            guard microphoneAllowed else {
                statusMessage = "Разреши микрофон в настройках iPhone/iPad."
                return
            }
            guard let recognizer, recognizer.isAvailable else {
                statusMessage = "Русское распознавание речи сейчас недоступно."
                return
            }
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playAndRecord, mode: .measurement, options: [.defaultToSpeaker, .allowBluetooth])
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            request.taskHint = .dictation
            if #available(iOS 16, *) { request.addsPunctuation = true }
            recognitionRequest = request
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
                request.append(buffer)
            }
            engine.prepare()
            try engine.start()
            isListening = true
            recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
                Task { @MainActor in
                    guard let self else { return }
                    if let result {
                        self.transcript = result.bestTranscription.formattedString
                        self.partialHandler?(self.transcript)
                        if result.isFinal { self.stopListening() }
                    }
                    if error != nil {
                        self.stopListening()
                        if self.transcript.isEmpty {
                            self.statusMessage = "Речь не распознана. Попробуй ещё раз или введи текст."
                        }
                    }
                }
            }
        } catch {
            stopListening()
            statusMessage = "Не удалось начать голосовой ввод. Проверь доступ к аудио."
        }
    }

    func stopListening() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionRequest = nil
        recognitionTask = nil
        isListening = false
        partialHandler = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    func speak(_ text: String, voiceIdentifier: String?) {
        guard !text.isEmpty else { return }
        stopListening()
        stopSpeaking()
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .spokenAudio)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            statusMessage = "Не удалось включить озвучивание."
            return
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = voiceIdentifier.flatMap(AVSpeechSynthesisVoice.init(identifier:))
            ?? preferredRussianVoice()
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
        isSpeaking = true
    }

    func stopSpeaking() {
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        isSpeaking = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    static var russianVoices: [AVSpeechSynthesisVoice] {
        AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language.lowercased().hasPrefix("ru") }
            .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    private func preferredRussianVoice() -> AVSpeechSynthesisVoice? {
        Self.russianVoices.first(where: { $0.gender == .male && $0.quality == .enhanced })
            ?? Self.russianVoices.first(where: { $0.gender == .male })
            ?? AVSpeechSynthesisVoice(language: "ru-RU")
    }

    private func requestSpeechPermission() async -> Bool {
        let current = SFSpeechRecognizer.authorizationStatus()
        if current == .authorized { return true }
        guard current == .notDetermined else { return false }
        return await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { continuation.resume(returning: $0 == .authorized) }
        }
    }

    private func requestMicrophonePermission() async -> Bool {
        let session = AVAudioSession.sharedInstance()
        switch session.recordPermission {
        case .granted: return true
        case .denied: return false
        case .undetermined:
            return await withCheckedContinuation { continuation in
                session.requestRecordPermission { continuation.resume(returning: $0) }
            }
        @unknown default: return false
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in
            isSpeaking = false
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in isSpeaking = false }
    }
}
