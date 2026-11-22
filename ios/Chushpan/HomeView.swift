import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                hero

                NavigationLink(destination: ChatView()) {
                    Label("Поговорить с Чушпаном", systemImage: "bubble.left.and.bubble.right.fill")
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Откроет чат с ответами GPT через защищённое соединение с Mac")

                StatusCard(
                    title: model.macStatus == nil ? "Mac недоступен" : "Mac на связи",
                    detail: model.macStatus == nil
                        ? model.macConnectionMessage
                        : "Защищённое сопряжение · \(model.macConnectionKind). Данные и команды доступны только по разрешениям, выданным на Mac.",
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
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .mobileCard(cornerRadius: 20)
                    }
                    .buttonStyle(.plain)
                } else if model.database.macLibraryHasLoaded {
                    StatusCard(
                        title: "Медиатека синхронизирована",
                        detail: "Нет записей со статусом «Смотрю». Открой медиатеку, чтобы посмотреть списки и избранное.",
                        symbol: "play.rectangle"
                    )
                } else {
                    StatusCard(
                        title: "Медиатека Mac",
                        detail: model.macMediaError ?? "Сохраняет фильмы, избранное и прогресс в общей базе Чушпана.",
                        symbol: "play.rectangle"
                    )
                }

                VStack(alignment: .leading, spacing: 12) {
                    Text("Разделы Чушпана").font(.headline)
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 145), spacing: 12)], spacing: 12) {
                        quickLink("Медиатека", symbol: "play.rectangle", destination: MediaLibraryView())
                        quickLink("Устройства", symbol: "lightbulb.2", destination: DevicesView())
                        quickLink("Освещение", symbol: "lightbulb.led.wide", destination: LightingView())
                        quickLink("Сценарии", symbol: "sparkles", destination: ScenariosView())
                        quickLink("Календарь", symbol: "calendar", destination: CalendarView())
                        quickLink("Статистика", symbol: "chart.bar.xaxis", destination: StatisticsView())
                    }
                }

                if let calendar = model.database.macCalendarCache {
                    StatusCard(
                        title: calendar.status == "ready" ? "Календарь Mac обновлён" : "Календарь Mac",
                        detail: calendar.message,
                        symbol: "calendar",
                        tint: calendar.status == "ready" ? Color.accentPurple : .secondary
                    )
                }
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

    private var hero: some View {
        MobilePageHeader(
            eyebrow: "Личный помощник",
            title: "Чушпан",
            subtitle: model.macChatStatus.map { "\($0.provider.uppercased()) · \($0.model) · через Mac" }
                ?? "Твой голосовой помощник на iPhone",
            symbol: "cat.fill",
            showsMascot: true
        )
    }

    private func quickLink<Destination: View>(
        _ title: String,
        symbol: String,
        destination: Destination
    ) -> some View {
        NavigationLink(destination: destination) {
            Label(title, systemImage: symbol)
                .font(.subheadline.weight(.medium))
                .frame(maxWidth: .infinity, alignment: .leading)
                .mobileCard()
        }
        .buttonStyle(.plain)
    }

    private func progressText(_ item: MobileMediaItem) -> String {
        let minutes = Int(item.positionSeconds) / 60
        if item.kind == .series {
            return "Сезон \(item.season), серия \(item.episode) · \(minutes) мин"
        }
        return "\(minutes) мин сохранено"
    }
}
