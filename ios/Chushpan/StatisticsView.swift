import SwiftUI

struct StatisticsView: View {
    @EnvironmentObject private var model: AppModel

    private var statistics: MacStatisticsSnapshot? { model.database.macStatisticsCache }
    private var hasPermission: Bool {
        model.macStatus?.categories.contains("statistics") == true
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Статистика Чушпана")
                            .font(.title2.weight(.bold))
                        Text("Сводка за последние семь дней с Mac")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Обновить", systemImage: "arrow.triangle.2.circlepath") {
                        Task { await model.syncFromMac() }
                    }
                    .labelStyle(.iconOnly)
                    .accessibilityLabel("Обновить статистику с Mac")
                }

                if let statistics {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 145), spacing: 12)], spacing: 12) {
                        metric("Сообщения", value: "\(statistics.messages)", symbol: "bubble.left.and.bubble.right")
                        metric("Диалоги", value: "\(statistics.conversations)", symbol: "text.bubble")
                        metric("Команды", value: "\(statistics.commands)", symbol: "command")
                        metric("Ошибки", value: "\(statistics.errors)", symbol: "exclamationmark.triangle")
                        metric("Ответ", value: duration(statistics.response), symbol: "clock")
                        metric("Модель", value: statistics.model ?? "—", symbol: "cpu")
                    }

                    VStack(alignment: .leading, spacing: 12) {
                        Text("Запросы по дням").font(.headline)
                        if statistics.buckets.isEmpty {
                            Text("За этот период запросов нет.")
                                .font(.subheadline).foregroundStyle(.secondary)
                        } else {
                            let maximum = max(statistics.buckets.map(\.requests).max() ?? 1, 1)
                            ForEach(statistics.buckets) { bucket in
                                HStack(spacing: 10) {
                                    Text(bucket.label)
                                        .font(.caption.monospacedDigit())
                                        .frame(width: 55, alignment: .leading)
                                    GeometryReader { proxy in
                                        ZStack(alignment: .leading) {
                                            Capsule().fill(Color.accentPurple.opacity(0.12))
                                            Capsule()
                                                .fill(Color.accentPurple)
                                                .frame(width: proxy.size.width * CGFloat(bucket.requests) / CGFloat(maximum))
                                        }
                                    }
                                    .frame(height: 9)
                                    Text("\(bucket.requests)")
                                        .font(.caption.monospacedDigit())
                                        .frame(width: 28, alignment: .trailing)
                                }
                            }
                        }
                    }
                    .padding(18)
                    .background(.background, in: RoundedRectangle(cornerRadius: 19))

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Среднее время по этапам").font(.headline)
                        stage("Распознавание речи", value: statistics.stages.stt)
                        stage("Модель", value: statistics.stages.modelTotal)
                        stage("Создание речи", value: statistics.stages.ttsGeneration)
                    }
                    .padding(18)
                    .background(.background, in: RoundedRectangle(cornerRadius: 19))
                } else {
                    ContentUnavailableView(
                        hasPermission ? "Статистика не загружена" : "Нужно разрешение статистики",
                        systemImage: "chart.bar.xaxis",
                        description: Text(hasPermission
                            ? "Подключи Mac и обнови сводку. На iPhone показывается только кешированная статистика Mac."
                            : "Выбери категорию «Сводка статистики Чушпана» в разрешениях сопряжённого iPhone на Mac.")
                    )
                    .padding(.top, 50)
                }
            }
            .padding(18)
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .navigationTitle("Статистика")
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.syncFromMac() }
    }

    private func metric(_ title: String, value: String, symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(Color.accentPurple)
            Text(value)
                .font(.title3.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
        .padding(15)
        .background(.background, in: RoundedRectangle(cornerRadius: 17))
    }

    private func stage(_ title: String, value: Double?) -> some View {
        LabeledContent(title, value: duration(value))
            .font(.subheadline)
    }

    private func duration(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(value.formatted(.number.precision(.fractionLength(1)))) с"
    }
}
