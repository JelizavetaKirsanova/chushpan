// A small, local Accessibility bridge. No browser scripting, cookies or coordinates.
import AppKit
import ApplicationServices
import Foundation
import IOKit.graphics
import Vision

func output(_ value: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}
func fail(_ message: String) -> Never { output(["error": message]) }
func attr(_ element: AXUIElement, _ key: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, key as CFString, &value) == .success ? value : nil
}
func children(_ element: AXUIElement) -> [AXUIElement] {
    var items = attr(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
    if text(element, kAXRoleAttribute) == "AXApplication" {
        for window in attr(element, kAXWindowsAttribute) as? [AXUIElement] ?? [] {
            if !items.contains(where: { CFEqual($0, window) }) { items.append(window) }
        }
    }
    return items
}
func text(_ element: AXUIElement, _ key: String) -> String {
    guard let value = attr(element, key) else { return "" }
    if let url = value as? URL { return url.absoluteString }
    return String(describing: value)
}
func fields(_ element: AXUIElement) -> [String: String] {
    var out: [String: String] = [:]
    for (key, ax) in [("role", "AXRole"), ("title", "AXTitle"),
                      ("description", "AXDescription"), ("value", "AXValue"),
                      ("url", "AXURL"), ("help", "AXHelp"), ("identifier", "AXIdentifier"),
                      ("main", "AXMain"), ("focused", "AXFocused")] {
        let value = text(element, ax)
        if !value.isEmpty { out[key] = value }
    }
    out["enabled"] = text(element, "AXEnabled")
    return out
}

// Desktop operations reuse the same AX access and compiler as the media bridge.
func appInfo(_ app: NSRunningApplication) -> [String: Any] {
    return ["name": app.localizedName ?? "", "bundle": app.bundleIdentifier ?? "",
            "pid": app.processIdentifier, "active": app.isActive]
}
func windowFrame(_ window: AXUIElement) -> CGRect? {
    guard let p = attr(window, kAXPositionAttribute), let s = attr(window, kAXSizeAttribute),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
func frameArray(_ frame: CGRect) -> [Double] {
    return [Double(frame.minX), Double(frame.minY), Double(frame.width), Double(frame.height)]
}
func visibleScreens() -> [CGRect] {
    let top = NSScreen.screens.first?.frame.maxY ?? 0
    return NSScreen.screens.map { screen in
        let f = screen.visibleFrame
        return CGRect(x: f.minX, y: top - f.maxY, width: f.width, height: f.height)
    }
}
func windowInfo(_ window: AXUIElement, _ index: Int, _ root: AXUIElement) -> [String: Any] {
    let focus = attr(root, kAXFocusedWindowAttribute)
    return ["index": index, "title": text(window, kAXTitleAttribute),
            "identifier": text(window, "AXIdentifier"),
            "frame": windowFrame(window).map(frameArray) ?? [],
            "focused": focus.map { CFEqual($0, window) } ?? false,
            "minimized": (attr(window, kAXMinimizedAttribute) as? NSNumber)?.boolValue ?? false,
            "fullscreen": (attr(window, "AXFullScreen") as? NSNumber)?.boolValue ?? false]
}
func waitFor(_ test: () -> Bool) -> Bool {
    for _ in 0..<30 {
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))
        if test() { return true }
    }
    return false
}
func settable(_ element: AXUIElement, _ attribute: String) -> Bool {
    var value = DarwinBoolean(false)
    return AXUIElementIsAttributeSettable(element, attribute as CFString, &value) == .success && value.boolValue
}
func desktop(_ request: [String: Any], _ op: String) -> Never {
    if op == "desktop_activity_status" {
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: .null)
        output(["idle_s": idle.isFinite ? idle : 86400])
    }
    if op == "desktop_ocr" {
        guard let path = request["path"] as? String,
              path.hasSuffix(".png"), FileManager.default.fileExists(atPath: path) else { fail("invalid_image") }
        let recognition = VNRecognizeTextRequest()
        recognition.recognitionLevel = .accurate
        recognition.recognitionLanguages = ["ru-RU", "en-US"]
        recognition.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(url: URL(fileURLWithPath: path)).perform([recognition])
            let observations = recognition.results ?? []
            let lines: [[String: Any]] = observations.prefix(200).compactMap { observation in
                guard let candidate = observation.topCandidates(1).first else { return nil }
                let box = observation.boundingBox
                return ["text": String(candidate.string.prefix(500)), "confidence": candidate.confidence,
                        "box": [box.minX, box.minY, box.width, box.height]]
            }
            output(["ok": true, "lines": lines, "mode": "local_ocr"])
        } catch { fail("ocr_unavailable") }
    }
    if op == "desktop_capture_window" {
        guard AXIsProcessTrusted(), let app = NSWorkspace.shared.frontmostApplication else { fail("accessibility_permission") }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        guard let focused = attr(root, kAXFocusedWindowAttribute), CFGetTypeID(focused) == AXUIElementGetTypeID() else { fail("window_unavailable") }
        let title = text(focused as! AXUIElement, kAXTitleAttribute)
        let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let candidates = windows.filter { ($0[kCGWindowOwnerPID as String] as? Int32) == app.processIdentifier
            && ($0[kCGWindowName as String] as? String ?? "") == title
            && ($0[kCGWindowLayer as String] as? Int) == 0 }
        guard candidates.count == 1, let number = candidates[0][kCGWindowNumber as String] as? Int else { fail("window_ambiguous") }
        output(["window_id": number])
    }
    if op == "desktop_displays" {
        var ids = [CGDirectDisplayID](repeating: 0, count: 32)
        var count: UInt32 = 0
        guard CGGetOnlineDisplayList(32, &ids, &count) == .success else { fail("display_inventory") }
        let items: [[String: Any]] = ids.prefix(Int(count)).compactMap { id in
            guard let ref = CGDisplayCreateUUIDFromDisplayID(id)?.takeRetainedValue() else { return nil }
            let uuid = CFUUIDCreateString(nil, ref)! as String
            return ["uuid": uuid, "display_id": Int(id), "builtin": CGDisplayIsBuiltin(id) != 0]
        }
        output(["displays": items])
    }
    if op == "desktop_capture_status" {
        output(["granted": CGPreflightScreenCaptureAccess()])
    }
    if op == "desktop_apps" {
        let all = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
        let apps = request["scope"] as? String == "active" ? all.filter { $0.isActive } : all
        output(["apps": apps.map(appInfo)])
    }
    if op == "desktop_brightness" {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IODisplayConnect"), &iterator) == KERN_SUCCESS
            else { fail("unsupported_brightness") }
        defer { IOObjectRelease(iterator) }
        var displays: [(io_service_t, Float)] = []
        while true {
            let service = IOIteratorNext(iterator)
            if service == 0 { break }
            var value: Float = 0
            if IODisplayGetFloatParameter(service, 0, "brightness" as CFString, &value) == KERN_SUCCESS {
                displays.append((service, value))
            } else { IOObjectRelease(service) }
        }
        defer { for display in displays { IOObjectRelease(display.0) } }
        guard displays.count == 1 else { fail("unsupported_brightness") }
        let display = displays[0]
        var value = display.1
        if let level = request["level"] as? Int {
            guard (0...100).contains(level),
                  IODisplaySetFloatParameter(display.0, 0, "brightness" as CFString, Float(level) / 100) == KERN_SUCCESS,
                  IODisplayGetFloatParameter(display.0, 0, "brightness" as CFString, &value) == KERN_SUCCESS,
                  abs(value * 100 - Float(level)) < 2 else { fail("brightness_not_verified") }
        }
        output(["ok": true, "level": Int((value * 100).rounded())])
    }
    guard AXIsProcessTrusted() || op == "desktop_app" else { fail("accessibility_permission") }
    if op == "desktop_lock" {
        let source = CGEventSource(stateID: .combinedSessionState)
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 12, keyDown: false) else { fail("lock_unavailable") }
        // Exactly the documented Control-Command-Q, never client-provided keys.
        down.flags = [.maskControl, .maskCommand]
        up.flags = [.maskControl, .maskCommand]
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
        output(["submitted": true])
    }
    guard let bundle = request["target_bundle"] as? String,
          let pid = request["pid"] as? Int32,
          let app = NSRunningApplication(processIdentifier: pid),
          app.bundleIdentifier == bundle else { fail("app_changed") }
    if op == "desktop_app" {
        if request["action"] as? String == "switch" {
            guard app.activate(options: [.activateAllWindows]), waitFor({ app.isActive }) else { fail("activation_not_verified") }
        } else if request["action"] as? String == "quit" {
            guard app.terminate(), waitFor({ app.isTerminated }) else { fail("quit_not_verified") }
        } else { fail("invalid_request") }
        output(["ok": true])
    }
    let root = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(root, 2)
    let windows = attr(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
    if op == "desktop_windows" {
        output(["windows": windows.enumerated().map { windowInfo($0.element, $0.offset, root) },
                "screens": visibleScreens().map(frameArray)])
    }
    guard op == "desktop_window", let expected = request["expected"] as? [String: Any],
          let index = expected["index"] as? Int, index >= 0, index < windows.count,
          let action = request["action"] as? String else { fail("invalid_request") }
    let window = windows[index]
    let current = windowInfo(window, index, root)
    guard NSDictionary(dictionary: current).isEqual(to: expected) else { fail("window_changed") }
    if request["implicit"] as? Bool == true && (!app.isActive || current["focused"] as? Bool != true) {
        fail("window_changed")
    }
    // Refuse identical ambiguous AX windows; an ordinal is not a stable system ID.
    let identical = windows.filter { text($0, kAXTitleAttribute) == text(window, kAXTitleAttribute)
        && windowFrame($0) == windowFrame(window) }
    guard identical.count == 1 else { fail("window_ambiguous") }
    func observedWindow() -> AXUIElement {
        let freshRoot = AXUIElementCreateApplication(pid)
        let candidates = (attr(freshRoot, kAXWindowsAttribute) as? [AXUIElement] ?? []).filter {
            text($0, kAXTitleAttribute) == current["title"] as? String
            && text($0, "AXIdentifier") == current["identifier"] as? String
        }
        return candidates.count == 1 ? candidates[0] : window
    }
    if action == "close" {
        guard let button = attr(window, kAXCloseButtonAttribute), CFGetTypeID(button) == AXUIElementGetTypeID(),
              AXUIElementPerformAction(button as! AXUIElement, kAXPressAction as CFString) == .success else { fail("window_close_unavailable") }
        let closed = waitFor {
            let fresh = attr(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
            return !fresh.contains { CFEqual($0, window) }
        }
        guard closed else { fail("window_close_not_verified") }
    } else if action == "focus" {
        guard AXUIElementPerformAction(window, kAXRaiseAction as CFString) == .success,
              app.activate(options: []), waitFor({
                  app.isActive && attr(root, kAXFocusedWindowAttribute).map { CFEqual($0, window) } == true
              }) else { fail("window_not_verified") }
    } else if ["minimize", "restore", "fullscreen", "exit_fullscreen"].contains(action) {
        let attribute = ["minimize", "restore"].contains(action) ? kAXMinimizedAttribute : "AXFullScreen"
        let desired = ["minimize", "fullscreen"].contains(action)
        guard settable(window, attribute) else { fail("unsupported_window_attribute") }
        let write: AXError
        if action == "minimize", let button = attr(window, kAXMinimizeButtonAttribute),
           CFGetTypeID(button) == AXUIElementGetTypeID() {
            write = AXUIElementPerformAction(button as! AXUIElement, kAXPressAction as CFString)
        } else {
            write = AXUIElementSetAttributeValue(window, attribute as CFString, desired ? kCFBooleanTrue : kCFBooleanFalse)
        }
        guard write == .success else { fail("window_write_\(write.rawValue)") }
        let verified = waitFor({ (attr(observedWindow(), attribute) as? NSNumber)?.boolValue == desired })
        guard verified else {
            output(["error": "window_not_verified", "observed": windowInfo(observedWindow(), index, root),
                    "raw_attribute": text(observedWindow(), attribute)])
        }
        // Fullscreen updates its state before the Space/AX tree transition has finished.
        if attribute == "AXFullScreen" { RunLoop.current.run(until: Date(timeIntervalSinceNow: 1)) }
    } else if ["move", "resize", "left", "right", "maximize"].contains(action) {
        guard var frame = windowFrame(window), current["fullscreen"] as? Bool != true else { fail("window_geometry_unavailable") }
        let screens = visibleScreens()
        guard let screen = screens.max(by: { $0.intersection(frame).area < $1.intersection(frame).area }) else { fail("window_geometry_unavailable") }
        if action == "left" || action == "right" {
            frame = CGRect(x: screen.minX + (action == "right" ? screen.width / 2 : 0),
                           y: screen.minY, width: screen.width / 2, height: screen.height)
        } else if action == "maximize" { frame = screen }
        else if action == "move" {
            guard let x = request["x"] as? Double, let y = request["y"] as? Double else { fail("invalid_geometry") }
            frame.origin = CGPoint(x: x, y: y)
        } else {
            guard let width = request["width"] as? Double, let height = request["height"] as? Double else { fail("invalid_geometry") }
            frame.size = CGSize(width: width, height: height)
        }
        guard frame.width >= 200, frame.height >= 80,
              screens.contains(where: { $0.insetBy(dx: -2, dy: -2).contains(frame) }),
              (action == "resize" || settable(window, kAXPositionAttribute)),
              (action == "move" || settable(window, kAXSizeAttribute)) else { fail("invalid_geometry") }
        var point = frame.origin, size = frame.size
        guard let position = AXValueCreate(.cgPoint, &point), let dimensions = AXValueCreate(.cgSize, &size)
            else { fail("invalid_geometry") }
        if action != "move" && action != "resize" {
            // AppKit constrains a large resize at the old origin before the move occurs.
            // Shrink to fit both origins, move, then apply the requested final size.
            let old = windowFrame(window) ?? frame
            var interim = CGSize(width: min(old.width, size.width), height: min(old.height, size.height))
            guard let small = AXValueCreate(.cgSize, &interim),
                  AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, small) == .success
                else { fail("window_not_verified") }
        }
        if action != "resize" && AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, position) != .success {
            fail("window_not_verified")
        }
        if action != "move" && AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, dimensions) != .success {
            fail("window_not_verified")
        }
        guard waitFor({
                  guard let actual = windowFrame(observedWindow()) else { return false }
                  return zip(frameArray(actual), frameArray(frame)).allSatisfy { abs($0 - $1) <= 3 }
              }) else {
            output(["error": "window_not_verified", "requested_frame": frameArray(frame),
                    "observed": windowInfo(window, index, root)])
        }
    } else { fail("unsupported_window_action") }
    output(["ok": true, "window": windowInfo(window, index, root)])
}
extension CGRect {
    var area: CGFloat { isNull ? 0 : width * height }
}

let data = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let bundle = request["bundle"] as? String,
      let op = request["op"] as? String else { fail("invalid_request") }
if ["permission_status", "permission_request"].contains(op) {
    let key = request["permission"] as? String ?? "accessibility"
    if op == "permission_request" {
        if key == "accessibility" {
            _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        } else if key == "screen_recording" { _ = CGRequestScreenCaptureAccess() }
        else { fail("invalid_request") }
    }
    output(["accessibility": AXIsProcessTrusted() ? "granted" : "check_needed",
            "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "check_needed",
            "identity": ["executable": CommandLine.arguments[0], "pid": getpid(), "ppid": getppid()]])
}
if op.hasPrefix("desktop_") { desktop(request, op) }
let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
if op == "status" {
    output(["trusted": AXIsProcessTrusted(), "running": !running.isEmpty,
            "frontmost": NSWorkspace.shared.frontmostApplication?.bundleIdentifier == bundle,
            "pids": running.map { $0.processIdentifier },
            "path": NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundle)?.path ?? ""])
}
guard AXIsProcessTrusted() else { fail("accessibility_permission") }
guard let app = running.first else { fail("app_closed") }
let root = AXUIElementCreateApplication(app.processIdentifier)
AXUIElementSetMessagingTimeout(root, 2)

if op == "snapshot" {
    var nodes: [[String: Any]] = []
    var truncated = false
    func visit(_ element: AXUIElement, _ path: [Int], _ depth: Int) {
        if nodes.count >= 14000 || depth > 55 { truncated = true; return }
        var node: [String: Any] = fields(element)
        node["path"] = path
        nodes.append(node)
        for (index, child) in children(element).enumerated() {
            visit(child, path + [index], depth + 1)
        }
    }
    visit(root, [], 0)
    output(["nodes": nodes, "truncated": truncated, "pid": app.processIdentifier])
}
guard ["press", "set", "search"].contains(op), let path = request["path"] as? [Int],
      let expected = request["expected"] as? [String: String] else { fail("invalid_request") }
// Recheck the owning web page as well as the button: a tab can change between calls.
if let scope = request["scope"] as? [String: Any] {
    guard let scopePath = scope["path"] as? [Int],
          let scopeExpected = scope["expected"] as? [String: String],
          path.starts(with: scopePath) else { fail("invalid_request") }
    var parent = root
    for index in scopePath {
        let items = children(parent)
        guard index >= 0 && index < items.count else { fail("element_changed") }
        parent = items[index]
    }
    let scopeActual = fields(parent)
    for (key, value) in scopeExpected where scopeActual[key] != value { fail("element_changed") }
}
var target = root
for index in path {
    let items = children(target)
    guard index >= 0 && index < items.count else { fail("element_changed") }
    target = items[index]
}
let actual = fields(target)
for (key, value) in expected where actual[key] != value { fail("element_changed") }
var error: AXError
if op == "search" {
    guard let query = request["query"] as? String, !query.isEmpty, query.count <= 400,
          text(target, "AXRole") == "AXComboBox" else { fail("invalid_search") }
    app.activate(options: [])
    _ = AXUIElementPerformAction(target, kAXPressAction as CFString)
    _ = AXUIElementSetAttributeValue(target, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    error = AXUIElementSetAttributeValue(target, kAXValueAttribute as CFString, "" as CFString)
    guard error == .success else { fail("search_field_unavailable") }
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.1))
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
        else { fail("search_focus_unavailable") }
    guard let focus = attr(root, kAXFocusedUIElementAttribute),
          CFGetTypeID(focus) == AXUIElementGetTypeID() else { fail("search_focus_unavailable") }
    var focused = focus as! AXUIElement
    var inSearchField = false
    for _ in 0..<12 {
        if CFEqual(focused, target) { inSearchField = true; break }
        guard let parent = attr(focused, kAXParentAttribute),
              CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
        focused = parent as! AXUIElement
    }
    guard inSearchField else { fail("search_focus_unavailable") }
    // One Return in the verified foreground app; no global hotkeys or key interception.
    let source = CGEventSource(stateID: .combinedSessionState)
    // Real text input updates the web component's query state; AXSetValue alone doesn't.
    for character in query {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
            else { fail("search_focus_unavailable") }
        let units = Array(String(character).utf16)
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)
            event?.flags = []
            units.withUnsafeBufferPointer { buffer in
                event?.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
            }
            event?.post(tap: .cghidEventTap)
        }
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.005))
    }
    // Let the web component commit its debounced query before submitting it.
    RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.5))
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
        else { fail("search_focus_unavailable") }
    guard let finalFocus = attr(root, kAXFocusedUIElementAttribute),
          CFGetTypeID(finalFocus) == AXUIElementGetTypeID(),
          text(finalFocus as! AXUIElement, "AXValue") == query
        else { fail("search_focus_unavailable") }
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: source, virtualKey: 36, keyDown: down)
        event?.flags = []
        event?.post(tap: .cghidEventTap)
    }
} else if op == "press" {
    error = AXUIElementPerformAction(target, kAXPressAction as CFString)
} else if let number = request["value"] as? NSNumber {
    error = AXUIElementSetAttributeValue(target, kAXValueAttribute as CFString, number)
} else if let string = request["value"] as? String {
    error = AXUIElementSetAttributeValue(target, kAXValueAttribute as CFString, string as CFString)
} else { fail("invalid_value") }
guard error == .success else { fail("ax_action_\(error.rawValue)") }
output(["ok": true])
