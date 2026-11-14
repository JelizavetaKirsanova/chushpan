import SwiftUI
import UIKit

struct ChatView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var speech: SpeechService
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var renameTitle = ""
    @State private var showingRename = false
    @State private var showingDelete = false
    @State private var showingPrivateModeInfo = false

    private var conversation: MobileConversation? { model.activeConversation }
    private var messages: [ChatMessage] { conversation?.messages ?? [] }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Где отвечает Чушпан", selection: $model.chatTarget) {
                ForEach(ChatExecutionTarget.allCases) { target in Text(target.rawValue).tag(target) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            if model.chatTarget == .mac && model.macStatus?.capabilities.contains("chat") != true {
                StatusCard(
                    title: "Чат Mac недоступен",
                    detail: "Нужны сопряжение, доступ Mac к сети и выданное на Mac разрешение «Чат». Сообщение не переключится на iPhone автоматически.",
                    symbol: "desktopcomputer.trianglebadge.exclamationmark",
                    tint: .orange
                )
                .padding(.horizontal, 16)
                .padding(.bottom, 8)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if messages.isEmpty {
                            ContentUnavailableView(
                                "Начни разговор",
                                systemImage: "bubble.left.and.bubble.right",
                                description: Text("Выбери устройство выполнения. Ответ с iPhone/iPad требует интернет и личный API-ключ.")
                            )
                            .padding(.top, 60)
                        }
                        ForEach(messages) { message in
                            ChatMessageRow(message: message) {
                                model.retryReply(message.id)
                            }
                            .id(message.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 10)
                    .padding(.bottom, 16)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: messages.count) { _, _ in
                    guard let last = messages.last else { return }
                    if reduceMotion {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    } else {
                        withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
                .onChange(of: messages.last?.text) { _, _ in
                    guard let last = messages.last else { return }
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { composerBar }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle(conversation?.title ?? "Чат")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Menu {
                    Button("Новый диалог", systemImage: "plus") { model.createConversation() }
                    if let current = conversation {
                        Button("Переименовать", systemImage: "pencil") {
                            renameTitle = current.title
                            showingRename = true
                        }
                        Button(
                            current.isPrivate ? "Выключить приватный режим" : "Включить приватный режим",
                            systemImage: current.isPrivate ? "lock.open" : "lock"
                        ) { model.setConversationPrivate(current.id, isPrivate: !current.isPrivate) }
                        Button("Удалить диалог", systemImage: "trash", role: .destructive) {
                            showingDelete = true
                        }
                    }
                    if !model.visibleConversations.isEmpty {
                        Section("Диалоги") {
                            ForEach(model.visibleConversations) { item in
                                Button(item.title) { model.selectConversation(item.id) }
                            }
                        }
                    }
                } label: {
                    Image(systemName: "text.bubble")
                        .accessibilityLabel("Управление диалогами")
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                if model.isGenerating {
                    Button("Остановить", systemImage: "stop.fill") { model.cancelGeneration() }
                        .labelStyle(.iconOnly)
                        .accessibilityLabel("Остановить генерацию")
                } else if conversation?.isPrivate == true {
                    Button {
                        showingPrivateModeInfo = true
                    } label: {
                        Label("Приватный", systemImage: "lock.fill").labelStyle(.iconOnly)
                    }
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Информация о приватном диалоге")
                }
            }
        }
        .alert("Переименовать диалог", isPresented: $showingRename) {
            TextField("Название", text: $renameTitle)
            Button("Сохранить") { if let id = conversation?.id { model.renameConversation(id, title: renameTitle) } }
            Button("Отмена", role: .cancel) { }
        }
        .confirmationDialog("Удалить этот диалог?", isPresented: $showingDelete, titleVisibility: .visible) {
            Button("Удалить диалог", role: .destructive) { if let id = conversation?.id { model.deleteConversation(id) } }
        } message: {
            Text("На других устройствах удаление распространится при включённой синхронизации iCloud.")
        }
        .alert("Приватный диалог", isPresented: $showingPrivateModeInfo) {
            Button("Понятно", role: .cancel) { }
        } message: {
            Text("Этот диалог исключён из iCloud и не добавляет факты памяти к контексту. Нажатие «Отправить» всё равно передаст его текущий текст и историю выбранному ИИ: Gemini при выборе этого устройства или настроенному провайдеру Mac.")
        }
        .task { await model.refreshMac() }
        .onDisappear { speech.stopListening() }
    }

    private var composerBar: some View {
        VStack(spacing: 6) {
            if speech.isListening {
                HStack(spacing: 8) {
                    Image(systemName: "waveform")
                    Text("Слушаю по кнопке. Скажи фразу и нажми микрофон ещё раз.")
                    Spacer()
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
            }
            if let notice = speech.statusMessage {
                Text(notice).font(.footnote).foregroundStyle(.secondary).padding(.horizontal, 16)
            }
            HStack(alignment: .bottom, spacing: 10) {
                Button {
                    Task {
                        if speech.isListening { speech.stopListening() }
                        else { await speech.startListening(onPartial: model.setComposerFromSpeech) }
                    }
                } label: {
                    Image(systemName: speech.isListening ? "stop.circle.fill" : "mic.circle.fill")
                        .font(.system(size: 32))
                }
                .accessibilityLabel(speech.isListening ? "Остановить голосовой ввод" : "Начать голосовой ввод")

                TextField("Напиши Чушпану…", text: $model.composer, axis: .vertical)
                    .lineLimit(1...5)
                    .textInputAutocapitalization(.sentences)
                    .submitLabel(.send)
                    .onSubmit(model.sendMessage)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 10)
                    .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 20))
                    .accessibilityLabel("Сообщение Чушпану")

                Button(action: model.sendMessage) {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 32))
                }
                .disabled(model.composer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.isGenerating)
                .accessibilityLabel("Отправить сообщение")
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 8)
            .background(.bar)
        }
    }
}

private struct ChatMessageRow: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var speech: SpeechService
    let message: ChatMessage
    let retry: () -> Void

    private var isUser: Bool { message.role == .user }

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 42) }
            VStack(alignment: .leading, spacing: 8) {
                if message.text.isEmpty && message.delivery == .streaming {
                    ProgressView().controlSize(.small).accessibilityLabel("Ответ загружается")
                } else if !message.text.isEmpty {
                    markdown(message.text)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let error = message.errorMessage, !error.isEmpty {
                    Text(error).font(.footnote).foregroundStyle(isUser ? .white.opacity(0.85) : .secondary)
                    if message.delivery == .failed || message.delivery == .cancelled {
                        Button("Повторить запрос", action: retry)
                            .font(.footnote.weight(.semibold))
                            .accessibilityHint("Повторит запрос, используя сохранённое сообщение")
                    }
                }
                if !message.text.isEmpty && !isUser {
                    HStack(spacing: 14) {
                        Button {
                            UIPasteboard.general.string = message.text
                        } label: { Label("Копировать", systemImage: "doc.on.doc") }
                        if speech.isSpeaking {
                            Button { speech.stopSpeaking() } label: { Label("Остановить", systemImage: "stop.fill") }
                        } else {
                            Button {
                                speech.speak(message.text, voiceIdentifier: model.voiceIdentifier.isEmpty ? nil : model.voiceIdentifier)
                            } label: { Label("Озвучить", systemImage: "speaker.wave.2") }
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.plain)
                    .foregroundStyle(isUser ? .white.opacity(0.8) : .secondary)
                }
            }
            .padding(13)
            .background(isUser ? Color.accentPurple : Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .foregroundStyle(isUser ? Color.white : Color.primary)
            .frame(maxWidth: 720, alignment: isUser ? .trailing : .leading)
            if !isUser { Spacer(minLength: 42) }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(isUser ? "Ты" : "Чушпан")
    }

    private func markdown(_ value: String) -> Text {
        if let formatted = try? AttributedString(markdown: value) { return Text(formatted) }
        return Text(value)
    }
}
