import SwiftUI
import UIKit

struct ChatView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var speech: SpeechService
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var composerFocused: Bool
    @State private var renameTitle = ""
    @State private var showingRename = false
    @State private var showingDelete = false
    @State private var showingPrivateModeInfo = false

    var onExitChat: (() -> Void)? = nil

    private var conversation: MobileConversation? { model.activeConversation }
    private var messages: [ChatMessage] { conversation?.messages ?? [] }

    var body: some View {
        VStack(spacing: 0) {
            if model.macStatus?.capabilities.contains("chat") != true {
                StatusCard(
                    title: "GPT на сервере Mac недоступен",
                    detail: "Нужно защищённое соединение и отдельное разрешение «Текстовый чат» на Mac. Сообщение не отправится через другой провайдер.",
                    symbol: "desktopcomputer.trianglebadge.exclamationmark",
                    tint: .orange
                )
                .padding(.horizontal, 16)
                .padding(.bottom, 8)
            } else if let chat = model.macChatStatus {
                HStack(spacing: 7) {
                    Image(systemName: "lock.shield.fill").foregroundStyle(Color.accentPurple)
                    Text("\(chat.provider.uppercased()) · \(chat.model) · без fallback")
                        .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 8)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 14) {
                        if messages.isEmpty {
                            ContentUnavailableView(
                                "Начни разговор",
                                systemImage: "bubble.left.and.bubble.right",
                                description: Text("Чушпан отвечает через настроенный GPT на сервере Mac. iPhone не хранит API-ключ.")
                            )
                            .padding(.top, 60)
                        }
                        ForEach(messages) { message in
                            messageRow(message)
                                .id(message.id)
                        }
                        if !model.database.macHistoryCache.isEmpty {
                            DisclosureGroup("История этого iPhone на Mac · \(model.database.macHistoryCache.count)") {
                                ForEach(model.database.macHistoryCache.reversed()) { item in
                                    ChatMessageRow(
                                        message: ChatMessage(
                                            id: "mac-\(item.id)",
                                            role: item.role,
                                            text: item.content
                                        ),
                                        isSpeaking: speech.isSpeaking,
                                        voiceIdentifier: model.voiceIdentifier,
                                        retry: {},
                                        onSpeak: speak,
                                        onStopSpeaking: speech.stopSpeaking
                                    )
                                    .equatable()
                                    .id("mac-\(item.id)")
                                }
                            }
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 4)
                            .padding(.top, 8)
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
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ChatComposer(
                focused: $composerFocused,
                isGenerating: model.isGenerating,
                voiceIdentifier: model.voiceIdentifier,
                onSend: model.sendMessage
            )
            .environmentObject(speech)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle(conversation?.title ?? "Чат")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.visible, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                if let onExitChat {
                    Button("Назад", systemImage: "chevron.left") {
                        composerFocused = false
                        onExitChat()
                    }
                    .labelStyle(.titleAndIcon)
                }
            }
            ToolbarItem(placement: .topBarLeading) {
                conversationMenu
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
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Готово", systemImage: "keyboard.chevron.compact.down") {
                    composerFocused = false
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
            Text("Этот диалог не отправляет свои прошлые сообщения в iCloud и не добавляет факты памяти к контексту. При отправке текущий текст и контекст диалога передаются серверу Mac. Для приватного режима Mac хранит историю только во временной базе на время запроса.")
        }
        .task { await model.refreshMac() }
        .onDisappear { speech.stopListening() }
    }

    private var conversationMenu: some View {
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

    private func messageRow(_ message: ChatMessage) -> some View {
        ChatMessageRow(
            message: message,
            isSpeaking: speech.isSpeaking,
            voiceIdentifier: model.voiceIdentifier,
            retry: { model.retryReply(message.id) },
            onSpeak: speak,
            onStopSpeaking: speech.stopSpeaking
        )
        .equatable()
    }

    private func speak(_ text: String, voiceIdentifier: String?) {
        speech.speak(text, voiceIdentifier: voiceIdentifier)
    }
}

private struct ChatComposer: View {
    @EnvironmentObject private var speech: SpeechService
    @FocusState.Binding var focused: Bool
    @State private var draft = ""

    let isGenerating: Bool
    let voiceIdentifier: String
    let onSend: (String) -> Void

    var body: some View {
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
                        if speech.isListening {
                            speech.stopListening()
                        } else {
                            await speech.startListening(onPartial: { draft = $0 })
                        }
                    }
                } label: {
                    Image(systemName: speech.isListening ? "stop.circle.fill" : "mic.circle.fill")
                        .font(.system(size: 32))
                }
                .accessibilityLabel(speech.isListening ? "Остановить голосовой ввод" : "Начать голосовой ввод")

                TextField("Напиши Чушпану…", text: $draft, axis: .vertical)
                    .lineLimit(1...5)
                    .textInputAutocapitalization(.sentences)
                    .submitLabel(.send)
                    .focused($focused)
                    .onSubmit(submit)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 10)
                    .background(
                        Color(uiColor: .secondarySystemGroupedBackground),
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous)
                    )
                    .accessibilityLabel("Сообщение Чушпану")

                Button(action: submit) {
                    if isGenerating {
                        ProgressView().tint(Color.accentPurple)
                    } else {
                        Image(systemName: "arrow.up.circle.fill").font(.system(size: 32))
                    }
                }
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isGenerating)
                .accessibilityLabel("Отправить сообщение")
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 8)
            .background(.bar)
        }
    }

    private func submit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isGenerating else { return }
        draft = ""
        onSend(text)
    }
}

private struct ChatMessageRow: View, Equatable {
    let message: ChatMessage
    let isSpeaking: Bool
    let voiceIdentifier: String
    let retry: () -> Void
    let onSpeak: (String, String?) -> Void
    let onStopSpeaking: () -> Void

    static func == (lhs: ChatMessageRow, rhs: ChatMessageRow) -> Bool {
        lhs.message == rhs.message
            && lhs.isSpeaking == rhs.isSpeaking
            && lhs.voiceIdentifier == rhs.voiceIdentifier
    }

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
                    if let provider = message.provider, let modelName = message.model {
                        Text("\(provider.uppercased()) · \(modelName)")
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                    }
                    HStack(spacing: 14) {
                        Button {
                            UIPasteboard.general.string = message.text
                        } label: {
                            Label("Копировать", systemImage: "doc.on.doc")
                        }
                        if isSpeaking {
                            Button(action: onStopSpeaking) { Label("Остановить", systemImage: "stop.fill") }
                        } else {
                            Button {
                                onSpeak(message.text, voiceIdentifier.isEmpty ? nil : voiceIdentifier)
                            } label: {
                                Label("Озвучить", systemImage: "speaker.wave.2")
                            }
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
