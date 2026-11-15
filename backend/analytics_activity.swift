// Opt-in foreground-app timing. Emits identifiers and names only; no window titles,
// URLs, screenshots, keyboard events, or periodic sampling.
import AppKit
import Foundation

func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func emitApplication(_ app: NSRunningApplication?) {
    guard let app, let bundle = app.bundleIdentifier, !bundle.isEmpty else { return }
    emit([
        "type": "activated",
        "timestamp": Date().timeIntervalSince1970,
        "bundle": bundle,
        "app": app.localizedName ?? bundle,
    ])
}

final class ActivityObserver: NSObject {
    @objc func activated(_ notification: Notification) {
        emitApplication(notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
    }

    @objc func paused(_ notification: Notification) {
        emit(["type": "paused", "timestamp": Date().timeIntervalSince1970])
    }

    @objc func resumed(_ notification: Notification) {
        emitApplication(NSWorkspace.shared.frontmostApplication)
    }
}

let observer = ActivityObserver()
let center = NSWorkspace.shared.notificationCenter
center.addObserver(observer, selector: #selector(ActivityObserver.activated(_:)),
                   name: NSWorkspace.didActivateApplicationNotification, object: NSWorkspace.shared)
center.addObserver(observer, selector: #selector(ActivityObserver.paused(_:)),
                   name: NSWorkspace.sessionDidResignActiveNotification, object: NSWorkspace.shared)
center.addObserver(observer, selector: #selector(ActivityObserver.resumed(_:)),
                   name: NSWorkspace.sessionDidBecomeActiveNotification, object: NSWorkspace.shared)
center.addObserver(observer, selector: #selector(ActivityObserver.paused(_:)),
                   name: NSWorkspace.screensDidSleepNotification, object: NSWorkspace.shared)
center.addObserver(observer, selector: #selector(ActivityObserver.resumed(_:)),
                   name: NSWorkspace.screensDidWakeNotification, object: NSWorkspace.shared)
emitApplication(NSWorkspace.shared.frontmostApplication)
RunLoop.main.run()
