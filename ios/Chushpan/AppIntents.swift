import AppIntents
import Foundation

struct OpenChushpanChatIntent: AppIntent {
    static var title: LocalizedStringResource = "Открыть чат Чушпана"
    static var description = IntentDescription("Открывает мобильный чат Чушпана.")
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult & OpensIntent {
        let url = URL(string: "chushpan://chat")!
        return .result(opensIntent: OpenURLIntent(url))
    }
}

struct ChushpanShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenChushpanChatIntent(),
            phrases: ["Открой чат с \(.applicationName)", "Начни разговор с \(.applicationName)"],
            shortTitle: "Новый разговор",
            systemImageName: "bubble.left.and.bubble.right"
        )
    }
}
