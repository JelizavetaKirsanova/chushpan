import SwiftUI

enum AppSection: String, CaseIterable, Hashable, Identifiable {
    case home, chat, devices, lighting, scenarios, library, calendar, statistics, memory, settings

    var id: String { rawValue }
    var title: String {
        switch self {
        case .home: "Главная"
        case .chat: "Чат"
        case .devices: "Устройства"
        case .lighting: "Освещение"
        case .scenarios: "Сценарии"
        case .library: "Медиатека"
        case .calendar: "Календарь"
        case .statistics: "Статистика"
        case .memory: "Память"
        case .settings: "Настройки"
        }
    }
    var symbol: String {
        switch self {
        case .home: "house"
        case .chat: "bubble.left.and.bubble.right"
        case .devices: "lightbulb.2"
        case .lighting: "lightbulb.led.wide"
        case .scenarios: "sparkles"
        case .library: "play.rectangle"
        case .calendar: "calendar"
        case .statistics: "chart.bar.xaxis"
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
            NavigationStack { ChatView(onExitChat: { selection = .home }) }
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
            List {
                ForEach(AppSection.allCases) { section in
                    Button {
                        selection = section
                    } label: {
                        Label(section.title, systemImage: section.symbol)
                    }
                    .buttonStyle(.plain)
                    .listRowBackground(selection == section ? Color.accentPurple.opacity(0.16) : Color.clear)
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
        case .lighting: LightingView()
        case .scenarios: ScenariosView()
        case .library: MediaLibraryView()
        case .calendar: CalendarView()
        case .statistics: StatisticsView()
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
                ForEach([
                    AppSection.lighting,
                    .scenarios,
                    .calendar,
                    .statistics,
                    .memory,
                    .settings,
                ]) { section in
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
    static let accentPurple = Color(uiColor: UIColor { traits in
        if traits.userInterfaceStyle == .dark {
            UIColor(red: 180.0 / 255, green: 135.0 / 255, blue: 199.0 / 255, alpha: 1)
        } else {
            UIColor(red: 145.0 / 255, green: 54.0 / 255, blue: 194.0 / 255, alpha: 1)
        }
    })
}

struct MobilePageHeader: View {
    @Environment(\.colorScheme) private var colorScheme

    let eyebrow: String
    let title: String
    let subtitle: String
    let symbol: String
    var showsMascot = false

    var body: some View {
        HStack(spacing: 14) {
            if showsMascot {
                Image("ChushpanIcon")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 56, height: 56)
                    .padding(7)
                    .background(
                        Color.accentPurple.opacity(0.13),
                        in: RoundedRectangle(cornerRadius: 19, style: .continuous)
                    )
                    .accessibilityLabel("Фирменный кот Чушпана")
            } else {
                Image(systemName: symbol)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(Color.accentPurple)
                    .frame(width: 48, height: 48)
                    .background(
                        Color.accentPurple.opacity(0.13),
                        in: RoundedRectangle(cornerRadius: 16, style: .continuous)
                    )
                    .accessibilityHidden(true)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(eyebrow.uppercased())
                    .font(.caption2.weight(.bold))
                    .tracking(1.2)
                    .foregroundStyle(Color.accentPurple)
                Text(title)
                    .font(.title2.weight(.bold))
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)
                Text(subtitle)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .fill(
                    LinearGradient(
                        colors: [
                            Color.accentPurple.opacity(colorScheme == .dark ? 0.24 : 0.15),
                            Color(uiColor: .secondarySystemGroupedBackground),
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ))
        }
        .overlay {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .strokeBorder(Color.accentPurple.opacity(0.20))
        }
        .accessibilityElement(children: .combine)
    }
}

private struct MobileCardModifier: ViewModifier {
    var cornerRadius: CGFloat = 18

    func body(content: Content) -> some View {
        content
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                Color(uiColor: .secondarySystemGroupedBackground),
                in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous).strokeBorder(
                    Color.accentPurple.opacity(0.13)))
    }
}

extension View {
    func mobileCard(cornerRadius: CGFloat = 18) -> some View {
        modifier(MobileCardModifier(cornerRadius: cornerRadius))
    }
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
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(tint.opacity(0.20)))
    }
}
