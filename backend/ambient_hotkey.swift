// Registers Control+Option+Command+F12 without reading or intercepting other keys.
import AppKit
import Carbon
import Foundation

func emit(_ message: String) {
    FileHandle.standardOutput.write(Data((message + "\n").utf8))
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)

var handler: EventHandlerRef?
var hotKey: EventHotKeyRef?
var event = EventTypeSpec(
    eventClass: OSType(kEventClassKeyboard),
    eventKind: UInt32(kEventHotKeyPressed)
)
let handlerStatus = InstallEventHandler(
    GetApplicationEventTarget(),
    { _, _, _ in
        emit("TOGGLE")
        return noErr
    },
    1,
    &event,
    nil,
    &handler
)
guard handlerStatus == noErr else {
    emit("ERROR \(handlerStatus)")
    exit(1)
}

let identifier = EventHotKeyID(signature: OSType(0x43485553), id: 1)
let modifiers = UInt32(cmdKey | optionKey | controlKey)
let keyCodes: [String: UInt32] = [
    "f9": UInt32(kVK_F9),
    "f10": UInt32(kVK_F10),
    "f11": UInt32(kVK_F11),
    "f12": UInt32(kVK_F12),
]
let keyName = CommandLine.arguments.dropFirst().first?.lowercased() ?? "f12"
guard let keyCode = keyCodes[keyName] else {
    emit("ERROR invalid-key")
    exit(2)
}
let registrationStatus = RegisterEventHotKey(
    keyCode,
    modifiers,
    identifier,
    GetApplicationEventTarget(),
    0,
    &hotKey
)
guard registrationStatus == noErr else {
    if let handler { RemoveEventHandler(handler) }
    emit("ERROR \(registrationStatus)")
    exit(1)
}

emit("READY")
application.run()

if let hotKey { UnregisterEventHotKey(hotKey) }
if let handler { RemoveEventHandler(handler) }
