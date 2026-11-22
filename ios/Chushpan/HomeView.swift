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
                        .padding(18)
                        .background(Color.accentPurple.opacity(0.11), in: RoundedRectangle(cornerRadius: 20))
                        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Color.accentPurple.opacity(0.18)))
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
        HStack(spacing: 16) {
            Image("ChushpanIcon")
                .resizable()
                .scaledToFit()
                .frame(width: 86, height: 86)
                .padding(8)
                .background(Color.accentPurple.opacity(0.12), in: RoundedRectangle(cornerRadius: 22))
                .accessibilityLabel("Фирменный кот Чушпана")
            VStack(alignment: .leading, spacing: 7) {
                Text("Чушпан")
                    .font(.largeTitle.weight(.bold))
                Text("Твой мобильный клиент Чушпана")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if let chat = model.macChatStatus {
                    Text("\(chat.provider.uppercased()) · \(chat.model)")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(Color.accentPurple)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(Color.accentPurple.opacity(0.18)))
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
                .padding(14)
                .background(.background, in: RoundedRectangle(cornerRadius: 17))
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
