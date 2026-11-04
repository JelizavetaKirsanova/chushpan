// Public CoreAudio and Bonjour only. Runs once per explicit GUI/tool operation.
import AppKit
import CoreAudio
import Foundation
import Network

func address(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}
func number(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32 {
    var a = address(selector), value: UInt32 = 0, size = UInt32(4)
    _ = AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value)
    return value
}
func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String {
    var a = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value) == noErr else { return "" }
    return value?.takeRetainedValue() as String? ?? ""
}
func outputs() -> [[String: Any]] {
    var a = address(kAudioHardwarePropertyDevices), size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size) == noErr else { return [] }
    var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = devices.withUnsafeMutableBytes { AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, $0.baseAddress!) }
    guard status == noErr else { return [] }
    let active = number(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice)
    return devices.compactMap { id in
        var streams = address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput), bytes: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &bytes) == noErr, bytes > 0,
              number(id, kAudioDevicePropertyDeviceIsAlive) == 1 else { return nil }
        let uid = string(id, kAudioDevicePropertyDeviceUID)
        guard !uid.isEmpty else { return nil }
        let transport = number(id, kAudioDevicePropertyTransportType)
        let kind = transport == kAudioDeviceTransportTypeAirPlay ? "airplay" : transport == kAudioDeviceTransportTypeBuiltIn ? "builtin" : (transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE) ? "bluetooth" : "other"
        return ["id": Int(id), "uid": uid, "name": string(id, kAudioObjectPropertyName), "active": id == active, "transport": kind]
    }
}
func finish(_ value: [String: Any]) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) { FileHandle.standardOutput.write(data) }
    exit(0)
}
let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "outputs": finish(["outputs": outputs()])
case "select":
    let list = outputs()
    guard args.count == 2, let item = list.first(where: { $0["uid"] as? String == args[1] }), let raw = item["id"] as? Int else { finish(["error": "Выход больше не доступен системе."]) }
    var target = AudioDeviceID(raw), a = address(kAudioHardwarePropertyDefaultOutputDevice)
    let status = AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, UInt32(MemoryLayout<AudioDeviceID>.size), &target)
    let confirmed = status == noErr && number(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice) == target
    finish(["outputs": outputs(), "device_state_confirmed": confirmed, "error": confirmed ? "" : "Система не подтвердила выбор выхода."])
case "browse":
    let queue = DispatchQueue(label: "local.chushpan.devices.bonjour")
    let types = ["_airplay._tcp", "_raop._tcp", "_wled._tcp"]
    var browsers: [NWBrowser] = [], found: [String: [String: String]] = [:], failures: [String] = []
    for type in types {
        let browser = NWBrowser(for: .bonjour(type: type, domain: "local."), using: .tcp)
        browser.browseResultsChangedHandler = { results, _ in
            // Replace this type's results so disappeared services are never retained.
            found = found.filter { $0.value["service"] != type }
            for result in results {
                if case let .service(name, service, domain, _) = result.endpoint {
                    let endpoint = "\(name)|\(service)|\(domain)"
                    found[endpoint] = ["name": name, "endpoint": endpoint, "service": type]
                }
            }
        }
        browser.stateUpdateHandler = { state in
            if case .failed = state { failures.append(type) }
            if case .waiting = state { failures.append(type) }
        }
        browsers.append(browser); browser.start(queue: queue)
    }
    queue.asyncAfter(deadline: .now() + 6) {
        for browser in browsers { browser.cancel() }
        finish(["services": Array(found.values), "warning": failures.isEmpty ? "" : "Часть поиска недоступна. Проверь разрешение Локальная сеть."])
    }
    dispatchMain()
case "sound", "displays":
    let suffix = args[0] == "sound" ? "com.apple.Sound-Settings.extension" : "com.apple.Displays-Settings.extension"
    let opened = NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:\(suffix)")!)
    finish(["submitted": opened, "message": args[0] == "sound" ? "В macOS открой Вывод и выбери приёмник. Затем обнови список в Чушпане. Подключение пока не подтверждено." : "В macOS выбери Добавить дисплей и приёмник AirPlay. Зеркалирование подтверждается в системе."])
default: finish(["error": "Неизвестная операция адаптера."])
}
