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
            MobilePageHeader(
                eyebrow: "Твоя коллекция",
                title: "Медиатека",
                subtitle: model.database.macLibraryHasLoaded
                    ? "\(model.database.macLibraryCache.count) фильмов и сериалов · общие списки Mac"
                    : "Фильмы, избранное и прогресс из общей медиатеки Mac",
                symbol: "play.rectangle.fill"
            )
            .padding(.horizontal, 16)

            if let updated = model.database.macCacheUpdatedAt {
                HStack {
                    Label("Общая медиатека Mac", systemImage: "desktopcomputer")
                    Spacer()
                    Text(updated.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption).foregroundStyle(.secondary)
                }
                .font(.caption.weight(.medium))
                .padding(.horizontal, 18)
            }
            if model.database.macLibraryHasLoaded, let error = model.macMediaError {
                StatusCard(
                    title: "Показан сохранённый кеш",
                    detail: error,
                    symbol: "wifi.slash",
                    tint: .orange
                )
                .padding(.horizontal, 16)
            }
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

            if !model.database.macLibraryHasLoaded {
                if model.macMediaLoading {
                    ProgressView("Загружаю медиатеку Mac…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ContentUnavailableView(
                        "Медиатека Mac ещё не загружена",
                        systemImage: "lock.icloud",
                        description: Text(model.macMediaError ?? "Подключи Mac и разреши синхронизацию медиатеки в настройках сопряжения.")
                    )
                    Button("Повторить синхронизацию", systemImage: "arrow.triangle.2.circlepath") {
                        Task { await model.syncFromMac() }
                    }
                    .buttonStyle(.borderedProminent)
                    Spacer()
                }
            } else if filtered.isEmpty {
                ContentUnavailableView(
                    query.isEmpty ? "Медиатека пуста" : "Ничего не найдено",
                    systemImage: "play.rectangle",
                    description: Text(query.isEmpty ? "В общей медиатеке Mac пока нет фильмов и сериалов." : "Измени поисковый запрос.")
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

                }
                .listStyle(.insetGrouped)
            }
        }
        .navigationTitle("Медиатека")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button("Обновить", systemImage: "arrow.triangle.2.circlepath") {
                    Task { await model.syncFromMac() }
                }
                .disabled(model.macMediaLoading)
                .accessibilityLabel("Синхронизировать медиатеку с Mac")
            }
            ToolbarItem(placement: .topBarTrailing) {
                if model.macStatus?.capabilities.contains("media_library") == true {
                    Button("Добавить", systemImage: "plus") {
                        editingItem = nil
                        showingNew = true
                    }
                    .accessibilityLabel("Добавить в общую медиатеку Mac")
                }
            }
        }
        .task { await model.syncFromMac() }
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
    let item: MobileMediaItem
    let edit: () -> Void

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
        }
        .padding(.vertical, 5)
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

    init(item: MobileMediaItem?) {
        existing = item
        _title = State(initialValue: item?.title ?? "")
        _kind = State(initialValue: item?.kind ?? .film)
        _year = State(initialValue: item?.year.map(String.init) ?? "")
        _status = State(initialValue: item?.status ?? .planned)
        _favorite = State(initialValue: item?.favorite ?? false)
        _rating = State(initialValue: item?.rating.map { String($0) } ?? "")
        _season = State(initialValue: item?.season ?? 0)
        _episode = State(initialValue: item?.episode ?? 0)
        _minutes = State(initialValue: item.map { String(Int($0.positionSeconds) / 60) } ?? "0")
        _durationMinutes = State(initialValue: item.map { String(Int($0.durationSeconds) / 60) } ?? "0")
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Произведение") {
                    if let existing {
                        Text(existing.title).font(.headline)
                        Text([existing.kind.rawValue, existing.year.map(String.init)].compactMap { $0 }.joined(separator: " · "))
                            .font(.footnote).foregroundStyle(.secondary)
                    } else {
                        TextField("Название", text: $title)
                        Picker("Тип", selection: $kind) {
                            ForEach(MediaKind.allCases) { Text($0.rawValue).tag($0) }
                        }
                        TextField("Год", text: $year).keyboardType(.numberPad)
                    }
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
            }
            .navigationTitle(existing == nil ? "Добавить" : "Изменить")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Сохранить", action: save)
                        .disabled(existing == nil && title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }

    private func save() {
        var item = existing ?? MobileMediaItem(title: title)
        if existing == nil {
            item.title = title
            item.kind = kind
            item.year = Int(year)
        }
        item.status = status
        item.favorite = favorite
        item.rating = Double(rating.replacingOccurrences(of: ",", with: "."))
        item.season = season
        item.episode = episode
        item.positionSeconds = max(0, (Double(minutes.replacingOccurrences(of: ",", with: ".")) ?? 0) * 60)
        item.durationSeconds = max(0, (Double(durationMinutes.replacingOccurrences(of: ",", with: ".")) ?? 0) * 60)
        if existing == nil { model.addMedia(item) } else { model.updateMedia(item) }
        dismiss()
    }
}
