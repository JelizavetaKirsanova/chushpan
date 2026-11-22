import SwiftUI

struct CalendarView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedDate = Date()

    private var dayKey: String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: model.database.macCalendarCache?.timezone ?? "")
            ?? .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: selectedDate)
    }

    private var selectedDay: MacCalendarDay? {
        guard let snapshot = model.database.macCalendarCache else { return nil }
        if snapshot.today == dayKey {
            return MacCalendarDay(
                date: snapshot.today,
                status: snapshot.status,
                message: snapshot.message,
                timezone: snapshot.timezone,
                updatedAt: snapshot.updatedAt,
                events: snapshot.events
            )
        }
        return snapshot.days?.first { $0.date == dayKey }
    }

    private var canReadCalendar: Bool {
        model.macStatus?.categories.contains("calendar") == true
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                MobilePageHeader(
                    eyebrow: "События с Mac",
                    title: "Календарь",
                    subtitle: "Выбери день, чтобы посмотреть события из календаря Mac",
                    symbol: "calendar"
                )
                DatePicker("День", selection: $selectedDate, displayedComponents: .date)
                    .datePickerStyle(.graphical)
                    .tint(Color.accentPurple)
                    .mobileCard(cornerRadius: 20)

                HStack {
                    Label("Календарь Mac", systemImage: "desktopcomputer")
                        .font(.headline)
                    Spacer()
                    Button("Обновить", systemImage: "arrow.triangle.2.circlepath") {
                        model.refreshCalendarDay(dayKey)
                    }
                    .buttonStyle(.bordered)
                    .disabled(!canReadCalendar || model.macStatus == nil)
                }

                if model.macMediaLoading {
                    ProgressView("Синхронизирую данные…")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }

                if let day = selectedDay {
                    if day.status == "ready" {
                        if day.events.isEmpty {
                            ContentUnavailableView(
                                "Событий нет",
                                systemImage: "calendar.badge.checkmark",
                                description: Text("В календаре Mac на этот день нет событий.")
                            )
                            .frame(maxWidth: .infinity)
                        } else {
                            VStack(spacing: 10) {
                                ForEach(day.events) { event in
                                    CalendarEventCard(event: event)
                                }
                            }
                        }
                    } else {
                        StatusCard(
                            title: day.status == "error" ? "Не удалось прочитать календарь" : "День ещё не загружен",
                            detail: day.message,
                            symbol: day.status == "error" ? "exclamationmark.calendar" : "calendar",
                            tint: day.status == "error" ? .orange : Color.accentPurple
                        )
                    }
                    if let updated = day.updatedAt {
                        Text("Обновлено на Mac · \(updated)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    StatusCard(
                        title: canReadCalendar ? "Этот день не загружен" : "Нужно разрешение календаря",
                        detail: canReadCalendar
                            ? "Нажми «Обновить», чтобы Mac прочитал Apple Calendar для выбранного дня."
                            : "Выбери категорию «События календаря Mac» в разрешениях сопряжённого iPhone на Mac.",
                        symbol: "calendar",
                        tint: canReadCalendar ? Color.accentPurple : .orange
                    )
                }

                if let feedback = model.macActionFeedback {
                    StatusCard(
                        title: feedback.state.title,
                        detail: feedback.message,
                        symbol: feedback.state == .failed ? "exclamationmark.circle" : "arrow.triangle.2.circlepath",
                        tint: feedback.state == .failed ? .orange : Color.accentPurple
                    )
                }
            }
            .padding(18)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle("Календарь")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.syncFromMac() }
    }
}

private struct CalendarEventCard: View {
    let event: MacCalendarEvent

    private var color: Color {
        guard let raw = event.color else { return .accentPurple }
        let hex = raw.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard hex.count == 6, let value = UInt32(hex, radix: 16) else { return .accentPurple }
        return Color(
            red: Double((value >> 16) & 0xff) / 255,
            green: Double((value >> 8) & 0xff) / 255,
            blue: Double(value & 0xff) / 255
        )
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            RoundedRectangle(cornerRadius: 3)
                .fill(color)
                .frame(width: 5)
            VStack(alignment: .leading, spacing: 6) {
                Text(event.title).font(.headline).fixedSize(horizontal: false, vertical: true)
                Text(event.allDay ? "Весь день" : "\(time(event.start)) – \(time(event.end))")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(Color.accentPurple)
                if let calendar = event.calendar, !calendar.isEmpty {
                    Text(calendar).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .mobileCard()
    }

    private func time(_ raw: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: raw) ?? ISO8601DateFormatter().date(from: raw)
        return date?.formatted(date: .omitted, time: .shortened) ?? raw
    }
}
