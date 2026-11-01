// Same executable checks permission and executes the server-generated AppleScript.
import AppKit
import Carbon
import Foundation

func output(_ value: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}
let data = FileHandle.standardInput.readDataToEndOfFile()
guard let input = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let operation = input["op"] as? String, ["probe", "script"].contains(operation)
else { output(["error": "invalid_request"]) }
var address = AEAddressDesc()
let bundle = Array("com.apple.Safari".utf8)
var code = OSStatus(bundle.withUnsafeBytes {
    AECreateDesc(DescType(typeApplicationBundleID), $0.baseAddress, bundle.count, &address)
})
if code == noErr {
    code = AEDeterminePermissionToAutomateTarget(&address,
        AEEventClass(typeWildCard), AEEventID(typeWildCard), false)
    AEDisposeDesc(&address)
}
let state: String = code == noErr ? "granted" : code == -1743 ? "denied" :
    code == -1744 ? "not_determined" : code == -600 ? "target_not_running" : "unknown"
if operation == "probe" {
    output(["automation": state, "osstatus": code, "pid": ProcessInfo.processInfo.processIdentifier,
            "ppid": getppid(), "executable": CommandLine.arguments[0],
            "scope": "same_executor_for_probe_and_applescript"])
}
if code != noErr && input["request_access"] as? Bool != true {
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
