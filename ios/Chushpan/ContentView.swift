import SwiftUI

enum AppSection: String, CaseIterable, Hashable, Identifiable {
    case home, chat, devices, scenarios, library, memory, settings

    var id: String { rawValue }
    var title: String {
        switch self {
        case .home: "Главная"
        case .chat: "Чат"
        case .devices: "Устройства"
        case .scenarios: "Сценарии"
        case .library: "Медиатека"
        case .memory: "Память"
        case .settings: "Настройки"
        }
    }
    var symbol: String {
        switch self {
        case .home: "house"
        case .chat: "bubble.left.and.bubble.right"
        case .devices: "lightbulb.2"
        case .scenarios: "sparkles"
        case .library: "play.rectangle"
        case .memory: "brain.head.profile"
        case .settings: "gearshape"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @State private var selection: AppSection = .home

    var body: some View {
        Group {
            if horizontalSizeClass == .compact {
                phoneNavigation
            } else {
                tabletNavigation
            }
        }
        .tint(Color.accentPurple)
        .onOpenURL { url in
            guard url.scheme == "chushpan", url.host == "chat" else { return }
            selection = .chat
            model.createConversation()
        }
        .alert("Не удалось сохранить данные", isPresented: storageAlert) {
            Button("Понятно", role: .cancel) { }
        } message: {
            Text(model.storageError ?? "Локальное хранилище недоступно.")
        }
    }

    private var phoneNavigation: some View {
        TabView(selection: $selection) {
            NavigationStack { HomeView() }
                .tabItem { Label("Главная", systemImage: "house") }
                .tag(AppSection.home)
            NavigationStack { ChatView() }
                .tabItem { Label("Чат", systemImage: "bubble.left.and.bubble.right") }
                .tag(AppSection.chat)
            NavigationStack { DevicesView() }
                .tabItem { Label("Устройства", systemImage: "lightbulb.2") }
                .tag(AppSection.devices)
            NavigationStack { MediaLibraryView() }
                .tabItem { Label("Медиатека", systemImage: "play.rectangle") }
                .tag(AppSection.library)
            NavigationStack {
                MoreView()
                    .navigationDestination(for: AppSection.self) { section in
                        sectionContent(section)
                    }
            }
            .tabItem { Label("Ещё", systemImage: "ellipsis.circle") }
            .tag(AppSection.memory)
        }
    }

    private var tabletNavigation: some View {
        NavigationSplitView {
            List(selection: $selection) {
                ForEach(AppSection.allCases) { section in
                    Label(section.title, systemImage: section.symbol).tag(section)
                }
            }
            .listStyle(.sidebar)
            .navigationTitle("Чушпан")
        } detail: {
            NavigationStack { sectionContent(selection) }
        }
    }

    @ViewBuilder
    private func sectionContent(_ section: AppSection) -> some View {
        switch section {
        case .home: HomeView()
        case .chat: ChatView()
        case .devices: DevicesView()
        case .scenarios: ScenariosView()
        case .library: MediaLibraryView()
        case .memory: MemoryView()
        case .settings: SettingsView()
        }
    }

    private var storageAlert: Binding<Bool> {
        Binding(get: { model.storageError != nil }, set: { if !$0 { model.storageError = nil } })
    }
}

struct MoreView: View {
    var body: some View {
        List {
            Section("Чушпан") {
                ForEach([AppSection.scenarios, .memory, .settings]) { section in
                    NavigationLink(value: section) {
                        Label(section.title, systemImage: section.symbol)
                    }
                }
            }
        }
        .navigationTitle("Ещё")
    }
}

extension Color {
    static let accentPurple = Color(red: 0.48, green: 0.31, blue: 0.69)
}

struct StatusCard: View {
    let title: String
    let detail: String
    var symbol = "info.circle"
    var tint = Color.accentPurple

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol)
                .font(.title3)
                .foregroundStyle(tint)
                .frame(width: 26)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(detail).font(.subheadline).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(tint.opacity(0.16)))
    }
}
