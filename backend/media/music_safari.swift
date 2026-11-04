// Same executable checks permission and executes the server-generated AppleScript.
import AppKit
import ApplicationServices
import Carbon
import Foundation

func output(_ value: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let input = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let operation = input["op"] as? String, ["probe", "request", "permission_request", "script"].contains(operation)
else { output(["error": "invalid_request"]) }
if operation == "permission_request" {
    if input["permission"] as? String == "accessibility" {
        _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    } else if input["permission"] as? String == "screen_recording" { _ = CGRequestScreenCaptureAccess() }
    else { output(["error": "invalid_request"]) }
    output(["accessibility": AXIsProcessTrusted() ? "granted" : "check_needed",
            "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "check_needed"])
}
let target = input["target"] as? String ?? "com.apple.Safari"
guard ["com.apple.Safari", "com.apple.finder", "com.apple.Music", "com.apple.systemevents", "com.spotify.client"].contains(target)
else { output(["error": "invalid_request"]) }
if operation == "request", input["launch_target"] as? Bool == true,
   NSRunningApplication.runningApplications(withBundleIdentifier: target).isEmpty,
   let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: target) {
    _ = NSWorkspace.shared.open(url)
    for _ in 0..<50 {
        if !NSRunningApplication.runningApplications(withBundleIdentifier: target).isEmpty { break }
        Thread.sleep(forTimeInterval: 0.1)
    }
}
var address = AEAddressDesc()
let bundle = Array(target.utf8)
var code = OSStatus(bundle.withUnsafeBytes {
    AECreateDesc(DescType(typeApplicationBundleID), $0.baseAddress, bundle.count, &address)
})
if code == noErr {
    code = AEDeterminePermissionToAutomateTarget(&address,
        AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
    if code == -1744 && operation == "request" && input["request_access"] as? Bool == true {
        _ = AEDeterminePermissionToAutomateTarget(&address,
            AEEventClass(typeWildCard), AEEventID(typeWildCard), true)
        code = AEDeterminePermissionToAutomateTarget(&address,
            AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
    }
    AEDisposeDesc(&address)
}
let state: String = code == noErr ? "granted" : code == -1743 ? "denied" :
    code == -1744 ? "not_determined" : code == -600 ? "target_not_running" : "unknown"
if operation == "probe" || operation == "request" {
    output(["automation": state, "osstatus": code, "pid": ProcessInfo.processInfo.processIdentifier,
            "ppid": getppid(), "executable": CommandLine.arguments[0],
            "bundle_id": Bundle.main.bundleIdentifier ?? "local.chushpan.music-safari", "target": target,
            "accessibility": AXIsProcessTrusted() ? "granted" : "check_needed",
            "screen_recording": CGPreflightScreenCaptureAccess() ? "granted" : "check_needed",
            "scope": "same_executor_for_probe_and_applescript"])
}
if code != noErr {
    output(["error": state == "denied" ? "automation_denied" :
            state == "target_not_running" ? "safari_closed" : "automation_unknown"])
}
guard let source = input["source"] as? String, source.utf8.count < 200_000,
      let script = NSAppleScript(source: source)
else { output(["error": "script_failed"]) }
var error: NSDictionary?
let result = script.executeAndReturnError(&error)
if let error = error {
    let number = error[NSAppleScript.errorNumber] as? Int ?? 0
    let message = (error[NSAppleScript.errorMessage] as? String ?? "").lowercased()
    // Never return the raw script/error: it may include a private search/title/URL.
    let reason = number == -1743 ? "automation_denied" : number == -600 ? "safari_closed" :
        message.contains("javascript") || message.contains("javascript из") ? "javascript_disabled" :
        number == -1728 || message.contains("tab changed") ? "tab_changed" : "service_unavailable"
    output(["error": reason, "osstatus": number])
}
output(["result": result.stringValue ?? String(result.int32Value)])
