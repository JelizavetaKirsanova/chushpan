// Closed Safari video port: fixed scripts, validated IDs, no supplied JavaScript or shell.
import AppKit
import ApplicationServices
import Carbon
import Foundation

func output(_ value: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}
func fail(_ message: String) -> Never { output(["error": message]) }
func run(_ source: String) -> NSAppleEventDescriptor {
    var error: NSDictionary?
    guard let script = NSAppleScript(source: source) else { fail("youtube_script") }
    let result = script.executeAndReturnError(&error)
    guard error == nil else { fail("youtube_safari_permission_or_state") }
    return result
}
func literal(_ text: String) -> String {
    // JSON string escaping is compatible with these validated ASCII script inputs.
    return "\"" + text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"").replacingOccurrences(of: "\n", with: "\\n") + "\""
}
func validVideoURL(_ value: String) -> String? {
    guard let u = URLComponents(string: value), u.scheme == "https",
          ["youtube.com", "www.youtube.com", "m.youtube.com"].contains(u.host ?? ""),
          u.user == nil, u.password == nil, u.port == nil, u.path == "/watch",
          let ids = u.queryItems?.filter({ $0.name == "v" }), ids.count == 1,
          let id = ids.first?.value,
          id.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil else { return nil }
    return id
}
func tabScript(_ window: Int, _ tab: Int, _ id: String, _ body: String) -> NSAppleEventDescriptor {
    // The identity is checked in the SAME Apple event script as the operation.
    return run("""
    tell application "Safari"
      set t to tab \(tab) of window id \(window)
      set u to URL of t
      if u does not start with "https://www.youtube.com/watch?v=\(id)" and u does not start with "https://youtube.com/watch?v=\(id)" and u does not start with "https://m.youtube.com/watch?v=\(id)" then error "tab changed"
      if (do JavaScript "new URL(location.href).searchParams.get('v')" in t) is not "\(id)" then error "video changed"
      \(body)
    end tell
    """)
}
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      request["bundle"] as? String == "com.apple.Safari",
      let op = request["op"] as? String,
      ["youtube_permissions", "youtube_open", "youtube_state", "youtube_play", "youtube_pause", "youtube_playlist"].contains(op)
else { fail("invalid_request") }
if op == "youtube_permissions" {
    var state = "target_not_running"
    var code: OSStatus = 0
    if !NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari").isEmpty {
        var address = AEAddressDesc()
        let bytes = Array("com.apple.Safari".utf8)
        let created = bytes.withUnsafeBytes {
            AECreateDesc(DescType(typeApplicationBundleID), $0.baseAddress, bytes.count, &address)
        }
        if created == noErr {
            code = AEDeterminePermissionToAutomateTarget(&address, AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
            AEDisposeDesc(&address)
            state = code == noErr ? "granted" : code == -1743 ? "denied" : code == -1744 ? "not_determined" : "unknown"
        } else { state = "unknown"; code = OSStatus(created) }
    }
    var javascript = "not_checked"
    if state == "granted" {
        var error: NSDictionary?
        let script = NSAppleScript(source: """
        tell application "Safari"
          if (count of windows) is 0 then return "no_tab"
          return do JavaScript "'chushpan-read-only'" in current tab of front window
        end tell
        """)
        let value = script?.executeAndReturnError(&error).stringValue
        if let details = error {
            javascript = "unavailable_apple_event_\(details[NSAppleScript.errorNumber] as? Int ?? 0)"
        } else { javascript = value == "chushpan-read-only" ? "granted" : "no_tab" }
    }
    output(["accessibility": AXIsProcessTrusted() ? "granted" : "not_granted",
            "automation": state, "automation_osstatus": code, "safari_javascript": javascript,
            "pid": ProcessInfo.processInfo.processIdentifier, "ppid": getppid(),
            "executable": CommandLine.arguments[0]])
}
guard AXIsProcessTrusted() else { fail("accessibility_permission") }
if op == "youtube_playlist" {
    let url = run("tell application \"Safari\" to return URL of current tab of front window").stringValue ?? ""
    guard let u = URLComponents(string: url), u.scheme == "https",
          ["youtube.com", "www.youtube.com", "m.youtube.com"].contains(u.host ?? ""),
          u.user == nil, u.password == nil, u.port == nil,
          let ids = u.queryItems?.filter({ $0.name == "list" }), ids.count == 1,
          let id = ids.first?.value,
          id.range(of: "^[A-Za-z0-9_-]{10,160}$", options: .regularExpression) != nil
    else { fail("youtube_no_playlist") }
    output(["playlist_id": id])
}
guard let id = request["video_id"] as? String,
      id.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil
else { fail("invalid_request") }
var window = request["window"] as? Int ?? 0
var tab = request["tab"] as? Int ?? 0
var warnings: [String] = []
if op == "youtube_open" {
    let found = run("""
    tell application "Safari"
      set foundTabs to {}
      repeat with w in windows
        set n to 0
        repeat with t in tabs of w
          set n to n + 1
          set u to URL of t
          if u starts with "https://www.youtube.com/watch?" or u starts with "https://youtube.com/watch?" or u starts with "https://m.youtube.com/watch?" or u starts with "https://www.youtube.com/shorts/" or u starts with "https://www.youtube.com/live/" then
            set end of foundTabs to {id of w, n, u, count of tabs of w}
          end if
        end repeat
      end repeat
      return foundTabs
    end tell
    """)
    guard found.numberOfItems <= 1 else { fail("youtube_multiple_tabs") }
    if found.numberOfItems == 1 {
        guard let row = found.atIndex(1), let oldURL = row.atIndex(3)?.stringValue,
              let oldID = validVideoURL(oldURL) else { fail("youtube_tab_changed") }
        window = Int(row.atIndex(1)?.int32Value ?? 0)
        tab = Int(row.atIndex(2)?.int32Value ?? 0)
        _ = tabScript(window, tab, oldID, "do JavaScript \"document.querySelector('video')?.pause()\" in t")
        _ = tabScript(window, tab, oldID, "set URL of t to \"https://www.youtube.com/watch?v=\(id)\"")
    } else {
        let result = run("""
        tell application "Safari"
          make new document with properties {URL:"https://www.youtube.com/watch?v=\(id)"}
          return id of front window
        end tell
        """)
        window = Int(result.int32Value)
        tab = 1
    }
    // Bring only our verified tab forward, so the unchanged Youtube sleep watches it.
    _ = tabScript(window, tab, id, "set current tab of window id \(window) to t\nset index of window id \(window) to 1\nactivate")
    if let uuid = request["external_uuid"] as? String, !uuid.isEmpty {
        let screen = NSScreen.screens.first { screen in
            guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
                  let ref = CGDisplayCreateUUIDFromDisplayID(number.uint32Value)?.takeRetainedValue()
            else { return false }
            return (CFUUIDCreateString(nil, ref)! as String).lowercased() == uuid.lowercased()
        }
        if let screen = screen,
           let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
            let count = tabScript(window, tab, id, "return count of tabs of window id \(window)").int32Value
            if count == 1 {
                let f = CGDisplayBounds(number.uint32Value)
                _ = tabScript(window, tab, id, "set bounds of window id \(window) to {\(Int(f.minX)), \(Int(f.minY)), \(Int(f.maxX)), \(Int(f.maxY))}")
                let observed = tabScript(window, tab, id, "return bounds of window id \(window)")
                let expected = [Int(f.minX), Int(f.minY), Int(f.maxX), Int(f.maxY)]
                if observed.numberOfItems != 4 || (1...4).contains(where: { abs(Int(observed.atIndex($0)?.int32Value ?? -100000) - expected[$0-1]) > 3 }) {
                    warnings.append("Перемещение окна на телевизор не подтверждено; проверь расположение вручную.")
                }

            } else { warnings.append("Во вкладке есть соседние страницы: окно не перемещал, перенеси видео в отдельное окно.") }
        } else { warnings.append("Выбранный телевизор отключён; видео осталось на текущем экране.") }
    }
    if request["fullscreen"] as? Bool == true && tabScript(window, tab, id, "return count of tabs of window id \(window)").int32Value == 1 {
        if let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari").first {
            let root = AXUIElementCreateApplication(app.processIdentifier)
            var result: CFTypeRef?
            AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &result)
            let windows = result as? [AXUIElement] ?? []
            let matches = windows.filter { w in
                var n: CFTypeRef?
                AXUIElementCopyAttributeValue(w, "AXWindowNumber" as CFString, &n)
                return (n as? NSNumber)?.intValue == window
            }
            if matches.count == 1 {
                let err = AXUIElementSetAttributeValue(matches[0], "AXFullScreen" as CFString, kCFBooleanTrue)
                var actual: CFTypeRef?
                AXUIElementCopyAttributeValue(matches[0], "AXFullScreen" as CFString, &actual)
                if err != .success || (actual as? NSNumber)?.boolValue != true { warnings.append("Полноэкранный режим не подтверждён; включи вручную.") }
            } else { warnings.append("Окно Safari не сопоставлено с AX; полноэкранный режим включи вручную.") }
        }
    }
    output(["opened": true, "window": window, "tab": tab, "warnings": warnings])
}
guard window > 0, tab > 0, tab <= 1000 else { fail("invalid_request") }
let change: String
if op == "youtube_pause" { change = "v.pause();" }
else if op == "youtube_play" {
    // Use the ordinary autoplay control; never skip ads or alter media streams.
    change = "const toggle=document.querySelector('.ytp-autonav-toggle-button');if(toggle?.getAttribute('aria-checked')==='true')toggle.click();v.play().catch(()=>{});"
} else { change = "" }
let js = """
(()=>{const v=document.querySelector('video');if(!v)return JSON.stringify({state:'loading'});\(change)
const a=document.querySelector('.ytp-autonav-toggle-button');
return JSON.stringify({id:new URL(location.href).searchParams.get('v'),state:v.ended?'ended':v.paused?'paused':'playing',progress:Number.isFinite(v.currentTime)?v.currentTime:null,duration:Number.isFinite(v.duration)?v.duration:null,ad:!!document.querySelector('.ad-showing'),autoplay_off:a?.getAttribute('aria-checked')==='false',active:document.visibilityState==='visible'&&document.hasFocus()});})()
"""
let raw = tabScript(window, tab, id, "return do JavaScript \(literal(js)) in t").stringValue ?? ""
guard let json = raw.data(using: .utf8), var result = try? JSONSerialization.jsonObject(with: json) as? [String: Any] else { fail("youtube_state_unavailable") }
result["frontmost"] = NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.apple.Safari"
output(result)
