import SwiftUI

struct MediaLibraryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var query = ""
    @State private var status: MediaStatus?
    @State private var favoritesOnly = false
    @State private var editingItem: MobileMediaItem?
    @State private var showingEditor = false
    @State private var showingNew = false
    @State private var deleteID: String?

    private var filtered: [MobileMediaItem] {
        model.visibleMedia.filter { item in
            (status == nil || item.status == status)
                && (!favoritesOnly || item.favorite)
                && (query.isEmpty || item.title.localizedCaseInsensitiveContains(query))
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Поиск в медиатеке", text: $query)
                    .textInputAutocapitalization(.never)
                    .accessibilityLabel("Поиск медиатеки")
            }
            .padding(11)
            .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal)

            Picker("Список", selection: $status) {
                Text("Всё").tag(MediaStatus?.none)
                ForEach(MediaStatus.allCases) { value in Text(value.rawValue).tag(Optional(value)) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)

            Toggle("Нравится", isOn: $favoritesOnly)
                .font(.subheadline)
                .padding(.horizontal, 18)
                .accessibilityHint("Показывает только отмеченные сердцем фильмы и сериалы")

            if filtered.isEmpty {
                ContentUnavailableView(
                    query.isEmpty ? "Медиатека пуста" : "Ничего не найдено",
                    systemImage: "play.rectangle",
                    description: Text("Добавляй фильмы и сериалы вручную. Прогресс можно изменить в любой момент.")
                )
                Spacer()
            } else {
                List {
                    if let current = model.continueWatching,
                       filtered.contains(where: { $0.id == current.id }) {
                        Section("Продолжить просмотр") {
                            MediaItemRow(item: current) { editingItem = current; showingEditor = true }
                        }
                    }
                    Section(status?.rawValue ?? "Медиатека") {
                        ForEach(filtered) { item in
                            MediaItemRow(item: item) { editingItem = item; showingEditor = true }
                        }
                        .onDelete { offsets in
                            if let offset = offsets.first { deleteID = filtered[offset].id }
                        }
                    }

                    if !model.database.macMediaCache.isEmpty {
                        Section("Прогресс, скопированный с Mac") {
                            ForEach(model.database.macMediaCache) { item in
                                VStack(alignment: .leading, spacing: 8) {
                                    Text(item.title).font(.headline)
                                    Text("Сезон \(item.season), серия \(item.episode) · \(Int(item.position) / 60) мин")
                                        .font(.subheadline).foregroundStyle(.secondary)
                                    Button("Скопировать в эту медиатеку") { model.importMacProgress(item) }
                                        .font(.footnote)
                                }
                                .padding(.vertical, 4)
                            }
                        } footer: {
                            Text("Это локальная копия с Mac. Запрос данных выполняется отдельно в настройках и подчиняется разрешениям Mac.")
                        }
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
        .navigationTitle("Медиатека")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("Добавить", systemImage: "plus") {
                    editingItem = nil
                    showingNew = true
                }
                .accessibilityLabel("Добавить фильм или сериал")
            }
        }
        .sheet(isPresented: $showingEditor) {
            if let editingItem {
                MediaItemEditor(item: editingItem)
                    .environmentObject(model)
                    .presentationDetents([.large])
            }
        }
        .sheet(isPresented: $showingNew) {
            MediaItemEditor(item: nil)
                .environmentObject(model)
                .presentationDetents([.large])
        }
        .confirmationDialog("Удалить запись из медиатеки?", isPresented: Binding(
            get: { deleteID != nil }, set: { if !$0 { deleteID = nil } }
        ), titleVisibility: .visible) {
            Button("Удалить", role: .destructive) {
                if let deleteID { model.deleteMedia(deleteID) }
                deleteID = nil
            }
        }
    }
}

private struct MediaItemRow: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.openURL) private var openURL
    let item: MobileMediaItem
    let edit: () -> Void
    @State private var showingPlaybackLimit = false

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(item.title).font(.headline).fixedSize(horizontal: false, vertical: true)
                    Text([item.kind.rawValue, item.year.map(String.init)].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if item.favorite { Image(systemName: "heart.fill").foregroundStyle(.pink).accessibilityLabel("Нравится") }
                Menu {
                    Button("Изменить прогресс и оценку", systemImage: "pencil", action: edit)
                    if item.status != .watched {
                        Button("Отметить просмотренным", systemImage: "checkmark.circle") {
                            var updated = item
                            updated.status = .watched
                            model.updateMedia(updated)
                        }
                    }
                    Button("Включить на Mac", systemImage: "desktopcomputer") { showingPlaybackLimit = true }
                    Button("Удалить", systemImage: "trash", role: .destructive) { model.deleteMedia(item.id) }
                } label: {
                    Image(systemName: "ellipsis.circle").accessibilityLabel("Действия с медиазаписью")
                }
            }
            HStack(spacing: 8) {
                Text(item.status.rawValue).font(.caption.weight(.medium))
                if let rating = item.rating { Text("★ \(rating.formatted(.number.precision(.fractionLength(0...1))))").font(.caption) }
            }
            .foregroundStyle(.secondary)
            if item.kind == .series, item.season > 0 || item.episode > 0 {
                Text("Сезон \(item.season), серия \(item.episode) · \(Int(item.positionSeconds) / 60) мин")
                    .font(.caption).foregroundStyle(.secondary)
            } else if item.positionSeconds > 0 {
                Text("Прогресс: \(Int(item.positionSeconds) / 60) мин")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Открыть на этом устройстве", systemImage: "safari") { openOnDevice() }
                    .disabled(validWebURL == nil)
                    .font(.footnote)
                Spacer()
                Button("Включить на Mac", systemImage: "desktopcomputer") { showingPlaybackLimit = true }
                    .font(.footnote)
            }
            if !item.webURL.isEmpty {
                Text("Откроется сайт. Управление встроенным плеером и восстановление позиции не подтверждены.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
        .confirmationDialog("Воспроизведение недоступно", isPresented: $showingPlaybackLimit, titleVisibility: .visible) {
            Button("Понятно", role: .cancel) { }
        } message: {
            Text("В мобильном клиенте нет проверенного playback endpoint для медиатеки Mac. Прогресс останется ручным; команда не отправлена.")
        }
    }

    private var validWebURL: URL? {
        guard let url = URL(string: item.webURL), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return nil }
        return url
    }

    private func openOnDevice() {
        guard let validWebURL else { return }
        openURL(validWebURL)
    }
}

private struct MediaItemEditor: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let existing: MobileMediaItem?
    @State private var title: String
    @State private var kind: MediaKind
    @State private var year: String
    @State private var status: MediaStatus
    @State private var favorite: Bool
    @State private var rating: String
    @State private var season: Int
    @State private var episode: Int
    @State private var minutes: String
    @State private var durationMinutes: String
    @State private var url: String

    init(item: MobileMediaItem?) {
        existing = item
        _title = State(initialValue: item?.title ?? "")
        _kind = State(initialValue: item?.kind ?? .film)
        _year = State(initialValue: item?.year.map(String.init) ?? "")
        _status = State(initialValue: item?.status ?? .planned)
        _favorite = State(initialValue: item?.favorite ?? false)
        _rating = State(initialValue: item?.rating.map(String.init) ?? "")
        _season = State(initialValue: item?.season ?? 0)
        _episode = State(initialValue: item?.episode ?? 0)
        _minutes = State(initialValue: item.map { String(Int($0.positionSeconds) / 60) } ?? "0")
        _durationMinutes = State(initialValue: item.map { String(Int($0.durationSeconds) / 60) } ?? "0")
        _url = State(initialValue: item?.webURL ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Произведение") {
                    TextField("Название", text: $title)
                    Picker("Тип", selection: $kind) {
                        ForEach(MediaKind.allCases) { Text($0.rawValue).tag($0) }
                    }
                    TextField("Год", text: $year).keyboardType(.numberPad)
                    Picker("Список", selection: $status) {
                        ForEach(MediaStatus.allCases) { Text($0.rawValue).tag($0) }
                    }
                    Toggle("Нравится", isOn: $favorite)
                    TextField("Оценка от 1 до 10", text: $rating).keyboardType(.decimalPad)
                }

                Section("Прогресс просмотра") {
                    if kind == .series {
                        Stepper("Сезон: \(season)", value: $season, in: 0...100)
                        Stepper("Серия: \(episode)", value: $episode, in: 0...1000)
                    }
                    TextField("Позиция, минут", text: $minutes).keyboardType(.decimalPad)
                    TextField("Длительность, минут", text: $durationMinutes).keyboardType(.decimalPad)
                    Text("Время редактируется вручную. Чушпан не управляет мобильным плеером и не считывает его позицию.")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                Section("Сайт (необязательно)") {
                    TextField("https://…", text: $url)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    Text("Кнопка откроет URL во внешнем браузере. Автоматизация Safari macOS здесь не используется.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle(existing == nil ? "Добавить" : "Изменить")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Сохранить", action: save)
                        .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !validURL)
                }
            }
        }
    }

    private var validURL: Bool {
        guard !url.isEmpty else { return true }
        guard let parsed = URL(string: url) else { return false }
        return ["https", "http"].contains(parsed.scheme?.lowercased() ?? "") && parsed.host != nil
    }

    private func save() {
        var item = existing ?? MobileMediaItem(title: title)
        item.title = title
        item.kind = kind
        item.year = Int(year)
        item.status = status
        item.favorite = favorite
        item.rating = Double(rating.replacingOccurrences(of: ",", with: "."))
        item.season = season
        item.episode = episode
        item.positionSeconds = max(0, (Double(minutes.replacingOccurrences(of: ",", with: ".")) ?? 0) * 60)
        item.durationSeconds = max(0, (Double(durationMinutes.replacingOccurrences(of: ",", with: ".")) ?? 0) * 60)
        item.webURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
        if existing == nil { model.addMedia(item) } else { model.updateMedia(item) }
        dismiss()
    }
}
