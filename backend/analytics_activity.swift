// Opt-in foreground-app timing. Emits identifiers and names only; no window titles,
// URLs, screenshots, keyboard events, or periodic sampling.
import AppKit
import CoreGraphics
import Foundation

let idleThreshold: TimeInterval = 60

func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func emitApplication(_ app: NSRunningApplication?) {
    guard let app, let bundle = app.bundleIdentifier, !bundle.isEmpty else { return }
    let systemBundleIDs: Set<String> = [
        "com.apple.loginwindow",
        "com.apple.dock",
        "com.apple.controlcenter",
        "com.apple.notificationcenterui",
        "com.apple.windowserver",
        "com.apple.windowmanager",
    ]
    let isSystem = systemBundleIDs.contains(bundle.lowercased())
        || app.activationPolicy != .regular
        || (app.bundleURL?.path.hasPrefix("/System/Library/CoreServices/") ?? false)
    emit([
        "type": "activated",
        "timestamp": Date().timeIntervalSince1970,
        "bundle": bundle,
        "app": app.localizedName ?? bundle,
        "system": isSystem,
    ])
}

func secondsSinceInput() -> TimeInterval {
    CGEventSource.secondsSinceLastEventType(
        .combinedSessionState,
        eventType: CGEventType(rawValue: UInt32.max)!
    )
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
var lastIdleState: Bool?
func updateIdleState() {
    let seconds = secondsSinceInput()
    guard seconds.isFinite else { return }
    let idle = seconds >= idleThreshold
    guard lastIdleState != idle else { return }
    lastIdleState = idle
    emit(["type": "idle", "idle": idle, "timestamp": Date().timeIntervalSince1970])
}

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
updateIdleState()
emitApplication(NSWorkspace.shared.frontmostApplication)
Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in updateIdleState() }
RunLoop.main.run()
