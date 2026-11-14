import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Чушпан")
                        .font(.largeTitle.weight(.bold))
                    Text("Локальная история, облачный ответ по твоей команде и отдельное управление Mac.")
                        .font(.body)
                        .foregroundStyle(.secondary)
                }

                NavigationLink(destination: ChatView()) {
                    Label("Голосовой разговор", systemImage: "mic.fill")
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 15)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Откроет чат, где можно говорить по кнопке или вводить текст")

                StatusCard(
                    title: model.macStatus == nil ? "Mac недоступен" : "Mac на связи",
                    detail: model.macStatus == nil
                        ? "Чат через Gemini и локально сохранённые данные работают отдельно от Mac. Подключение Mac настраивается явно."
                        : "Подключение защищено TLS. Сценарии и устройства запускаются только после выбора команды на этом устройстве.",
                    symbol: model.macStatus == nil ? "desktopcomputer.trianglebadge.exclamationmark" : "desktopcomputer",
                    tint: model.macStatus == nil ? .orange : .green
                )

                if let item = model.continueWatching {
                    NavigationLink(destination: MediaLibraryView()) {
                        VStack(alignment: .leading, spacing: 10) {
                            Label("Продолжить просмотр", systemImage: "play.circle.fill")
                                .font(.headline)
                            Text(item.title).font(.title3.weight(.semibold))
                            Text(progressText(item))
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                            Text("Время сохранено вручную. Открытие сайта не означает управление плеером или восстановление позиции.")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(18)
                        .background(Color.accentPurple.opacity(0.10), in: RoundedRectangle(cornerRadius: 18))
                    }
                    .buttonStyle(.plain)
                } else {
                    StatusCard(
                        title: "Продолжить просмотр",
                        detail: "Добавь фильм или сериал в медиатеку и укажи прогресс — карточка появится здесь.",
                        symbol: "play.rectangle"
                    )
                }

                VStack(alignment: .leading, spacing: 12) {
                    Text("Быстрый доступ").font(.headline)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 145), spacing: 12)], spacing: 12) {
                        quickLink("Чат", symbol: "bubble.left", destination: ChatView())
                        quickLink("Устройства", symbol: "lightbulb.2", destination: DevicesView())
                        quickLink("Сценарии", symbol: "sparkles", destination: ScenariosView())
                        quickLink("Память", symbol: "brain.head.profile", destination: MemoryView())
                    }
                }

                StatusCard(
                    title: "Данные дня",
                    detail: "Погода и календарь не подключены к мобильному приложению. Оно не выдаёт пустые карточки за полученную сводку.",
                    symbol: "sun.max"
                )
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 22)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle("Главная")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func quickLink<Destination: View>(_ title: String, symbol: String, destination: Destination) -> some View {
        NavigationLink(destination: destination) {
            Label(title, systemImage: symbol)
                .font(.subheadline.weight(.medium))
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(.background, in: RoundedRectangle(cornerRadius: 15))
        }
        .buttonStyle(.plain)
    }

    private func progressText(_ item: MobileMediaItem) -> String {
        let seconds = Int(item.positionSeconds)
        if item.kind == .series { return "Сезон \(item.season), серия \(item.episode) · \(seconds / 60) мин" }
        return "\(seconds / 60) мин сохранено"
    }
}
