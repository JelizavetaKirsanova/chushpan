import SwiftUI

struct MemoryView: View {
    @EnvironmentObject private var model: AppModel
    @State private var query = ""
    @State private var showingEditor = false
    @State private var editingFact: MobileMemoryFact?
    @State private var source = "На устройстве"

    private var localFacts: [MobileMemoryFact] {
        model.visibleFacts.filter {
            query.isEmpty || $0.content.localizedCaseInsensitiveContains(query)
                || $0.category.localizedCaseInsensitiveContains(query)
        }
    }
    private var macFacts: [MacMemoryCopy] {
        model.database.macMemoryCache.filter {
            query.isEmpty || $0.content.localizedCaseInsensitiveContains(query)
                || $0.category.localizedCaseInsensitiveContains(query)
        }
    }

    var body: some View {
        VStack(spacing: 12) {
            Picker("Источник памяти", selection: $source) {
                Text("На устройстве").tag("На устройстве")
                Text("Копия Mac").tag("Копия Mac")
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)

            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Поиск фактов", text: $query)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("Поиск памяти")
            }
            .padding(11)
            .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal)

            if source == "На устройстве" {
                if localFacts.isEmpty {
                    ContentUnavailableView(
                        query.isEmpty ? "Пока нет фактов" : "Ничего не найдено",
                        systemImage: "brain.head.profile",
                        description: Text(query.isEmpty ? "Добавляй только те сведения, которые хочешь сохранить на этом устройстве." : "Измени поисковый запрос.")
                    )
                    Spacer()
                } else {
                    List {
                        ForEach(localFacts) { fact in
                            LocalFactRow(fact: fact) {
                                editingFact = fact
                                showingEditor = true
                            } delete: {
                                model.deleteFact(fact.id)
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            } else if macFacts.isEmpty {
                ContentUnavailableView(
                    "Копии памяти Mac нет",
                    systemImage: "desktopcomputer",
                    description: Text("Подключи Mac и отдельно запроси разрешённые данные в настройках. Mac не становится единственным хранилищем.")
                )
                Spacer()
            } else {
                List {
                    Section {
                        ForEach(macFacts) { fact in
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text(fact.category).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                                    Spacer()
                                    Text("Копия Mac").font(.caption2).foregroundStyle(.secondary)
                                }
                                Text(fact.content).fixedSize(horizontal: false, vertical: true)
                                    .textSelection(.enabled)
                                HStack {
                                    Text(fact.updatedAt).font(.caption2).foregroundStyle(.secondary)
                                    Spacer()
                                    Button("Скопировать локально") {
                                        model.addFact(content: fact.content, category: fact.category, isPrivate: false)
                                    }
                                    .font(.footnote)
                                }
                            }
                            .padding(.vertical, 4)
                        }
                    } footer: {
                        Text("Копия доступна только для чтения. Локальное копирование — отдельное действие; оно не меняет факт на Mac.")
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
        .navigationTitle("Память")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if source == "На устройстве" {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Добавить факт", systemImage: "plus") {
                        editingFact = nil
                        showingEditor = true
                    }
                    .accessibilityLabel("Добавить факт памяти")
                }
            }
        }
        .sheet(isPresented: $showingEditor) {
            MemoryFactEditor(fact: editingFact)
                .environmentObject(model)
                .presentationDetents([.medium, .large])
        }
    }
}

private struct LocalFactRow: View {
    let fact: MobileMemoryFact
    let edit: () -> Void
    let delete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(fact.category).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                if fact.isPrivate {
                    Label("Приватно", systemImage: "lock.fill")
                        .font(.caption2).foregroundStyle(.orange)
                }
                Spacer()
                Menu {
                    Button("Изменить", systemImage: "pencil", action: edit)
                    Button("Удалить", systemImage: "trash", role: .destructive, action: delete)
                } label: {
                    Image(systemName: "ellipsis.circle").accessibilityLabel("Действия с фактом")
                }
            }
            Text(fact.content).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            Text(fact.updatedAt.formatted(date: .abbreviated, time: .shortened))
                .font(.caption2).foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }
}

private struct MemoryFactEditor: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var content: String
    @State private var category: String
    @State private var isPrivate: Bool
    let fact: MobileMemoryFact?

    init(fact: MobileMemoryFact?) {
        self.fact = fact
        _content = State(initialValue: fact?.content ?? "")
        _category = State(initialValue: fact?.category ?? "Общее")
        _isPrivate = State(initialValue: fact?.isPrivate ?? false)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Факт") {
                    TextField("Текст факта", text: $content, axis: .vertical)
                        .lineLimit(3...7)
                        .accessibilityLabel("Текст факта памяти")
                    TextField("Категория", text: $category)
                        .accessibilityLabel("Категория памяти")
                    Toggle("Приватный факт", isOn: $isPrivate)
                }
                Section {
                    Text("Приватные факты не отправляются GPT как контекст и не попадают в iCloud. Данные общей памяти включаются в запрос только при отдельном разрешении на Mac.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle(fact == nil ? "Новый факт" : "Изменить факт")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Отмена") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Сохранить") {
                        if let fact {
                            var updated = fact
                            updated.content = content
                            updated.category = category
                            updated.isPrivate = isPrivate
                            model.updateFact(updated)
                        } else {
                            model.addFact(content: content, category: category, isPrivate: isPrivate)
                        }
                        dismiss()
                    }
                    .disabled(content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }
}
