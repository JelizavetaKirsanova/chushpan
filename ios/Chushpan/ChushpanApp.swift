import SwiftUI

@main
struct ChushpanApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .environmentObject(model.speech)
                .preferredColorScheme(colorScheme)
                .onChange(of: scenePhase) { _, phase in
                    if phase == .background {
                        model.speech.stopListening()
                        model.cancelGeneration()
                    } else if phase == .active, model.cloudSyncEnabled {
                        model.requestCloudSync()
                    }
                }
        }
    }

    private var colorScheme: ColorScheme? {
        switch model.appearance {
        case "light": .light
        case "dark": .dark
        default: nil
        }
    }
}
