// Public read-only preflight APIs. No requests, app launches, recordings or captures.
import AppKit
import ApplicationServices
import AVFoundation
import Carbon
import Foundation

func automation(_ bundle: String) -> String {
    guard !NSRunningApplication.runningApplications(withBundleIdentifier: bundle).isEmpty
        else { return "target_not_running" }
    var address = AEAddressDesc()
    let bytes = Array(bundle.utf8)
    let created = bytes.withUnsafeBytes {
        AECreateDesc(DescType(typeApplicationBundleID), $0.baseAddress, bytes.count, &address)
    }
    guard created == noErr else { return "unknown" }
    defer { AEDisposeDesc(&address) }
    let status = AEDeterminePermissionToAutomateTarget(
        &address, AEEventClass(typeWildCard), AEEventID(typeWildCard), false
    )
    switch status {
    case noErr: return "granted"
    case -1743: return "denied"
    case -1744: return "not_determined"
    default: return "unknown"
    }
}

let microphone: String
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized: microphone = "granted"
case .denied: microphone = "denied"
case .restricted: microphone = "restricted"
case .notDetermined: microphone = "not_determined"
@unknown default: microphone = "unknown"
}
func safariJavascript() -> String {
    guard CommandLine.arguments.contains("com.apple.Safari"),
          automation("com.apple.Safari") == "granted" else { return "not_checked" }
    // Constant expression only: no page reads, navigation, playback or prompts.
    var error: NSDictionary?
    let script = NSAppleScript(source: """
    tell application "Safari"
      if (count of windows) is 0 then return "no_tab"
      return do JavaScript "'chushpan-read-only'" in current tab of front window
    end tell
    """)
    let value = script?.executeAndReturnError(&error).stringValue
    if let details = error {
        let code = details[NSAppleScript.errorNumber] as? Int ?? 0
        return "unavailable_apple_event_\(code)"
    }
    return value == "chushpan-read-only" ? "granted" : "no_tab"
}
let output: [String: Any] = [
    "helper_process": ["pid": ProcessInfo.processInfo.processIdentifier,
                       "ppid": getppid(), "executable": CommandLine.arguments[0]],
    "safari_javascript": safariJavascript(),

    "accessibility": AXIsProcessTrusted() ? "granted" : "not_granted",
    "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "not_granted",
    "microphone": microphone,
    "automation": Dictionary(uniqueKeysWithValues:
        Set(CommandLine.arguments.dropFirst()).map { ($0, automation($0)) }),
]
let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
