import AppKit
import CoreLocation
import EventKit
import Foundation

// A small, stable app identity lets macOS ask for these two permissions explicitly.
// No calendar writes or continuous tracking. City lookup uses the macOS geocoder.
final class LocationReader: NSObject, CLLocationManagerDelegate {
    let manager = CLLocationManager()
    var completion: (([String: Any]) -> Void)?
    var timer: Timer?
    var started = false
    var cityOnly = false
    var resolving = false
    let geocoder = CLGeocoder()

    func read(requestAccess: Bool, cityOnly: Bool = false, completion: @escaping ([String: Any]) -> Void) {
        self.completion = completion
        self.cityOnly = cityOnly
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyKilometer
        timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: false) { [weak self] _ in
            self?.finish(["error": "location_unavailable"])
        }
        if !CLLocationManager.locationServicesEnabled() {
            finish(["error": "location_disabled"])
        } else if manager.authorizationStatus == .notDetermined && !requestAccess {
            finish(["error": "location_permission"])
        } else if manager.authorizationStatus == .notDetermined {
            manager.requestWhenInUseAuthorization()
        } else {
            checkAuthorization()
        }
    }

    func checkAuthorization() {
        switch manager.authorizationStatus {
        case .authorized, .authorizedAlways:
            if !started {
                started = true
                manager.startUpdatingLocation()
            }
        case .denied, .restricted:
            finish(["error": "location_permission"])
        default: break
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        checkAuthorization()
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last,
              location.horizontalAccuracy >= 0,
              location.horizontalAccuracy <= (cityOnly ? 3000 : 25000),
              location.timestamp.timeIntervalSinceNow <= 0,
              location.timestamp.timeIntervalSinceNow > -300 else { return }
        if cityOnly {
            guard !resolving else { return }
            resolving = true
            manager.stopUpdatingLocation()
            geocoder.reverseGeocodeLocation(location, preferredLocale: Locale(identifier: "ru_RU")) { [weak self] places, _ in
                guard let city = places?.first?.locality, !city.isEmpty else {
                    self?.finish(["error": "location_city_unavailable"])
                    return
                }
                self?.finish(["city": city, "country": places?.first?.country ?? "",
                    "latitude": (location.coordinate.latitude * 10).rounded() / 10,
                    "longitude": (location.coordinate.longitude * 10).rounded() / 10,
                    "timezone": TimeZone.current.identifier,
                    "country_code": places?.first?.isoCountryCode ?? "",
                    "accuracy_m": location.horizontalAccuracy,
                    "measured_at": ISO8601DateFormatter().string(from: location.timestamp)])
            }
            return
        }
        finish([
            "latitude": location.coordinate.latitude,
            "longitude": location.coordinate.longitude,
            "timezone": TimeZone.current.identifier,
            "accuracy_m": location.horizontalAccuracy,
        ])
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if (error as? CLError)?.code == .denied {
            finish(["error": "location_permission"])
        }
    }

    func finish(_ result: [String: Any]) {
        guard let done = completion else { return }
        completion = nil
        manager.stopUpdatingLocation()
        timer?.invalidate()
        done(result)
    }
}

func calendarEvents(requestAccess: Bool, day: String? = nil) async -> [String: Any] {
    let store = EKEventStore()
    var status = EKEventStore.authorizationStatus(for: .event)
    if status == .notDetermined && requestAccess {
        do {
            _ = try await store.requestFullAccessToEvents()
        } catch {
            return ["error": "calendar_permission"]
        }
        status = EKEventStore.authorizationStatus(for: .event)
    }
    guard status == .fullAccess else { return ["error": "calendar_permission"] }
    let calendar = Calendar.current
    let input = DateFormatter()
    input.locale = Locale(identifier: "en_US_POSIX")
    input.timeZone = TimeZone.current
    input.dateFormat = "yyyy-MM-dd"
    input.isLenient = false
    if let day, input.date(from: day) == nil { return ["error": "invalid_date"] }
    let start = calendar.startOfDay(for: day.flatMap { input.date(from: $0) } ?? Date())
    guard let end = calendar.date(byAdding: .day, value: day == nil ? 2 : 1, to: start) else {
        return ["error": "calendar_unavailable"]
    }
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let formatter = ISO8601DateFormatter()
    let events = store.events(matching: predicate)
        .filter { $0.status != .canceled && $0.startDate < end && $0.endDate > start }
        .sorted { $0.startDate < $1.startDate }
        .map { event -> [String: Any] in
            let color = NSColor(cgColor: event.calendar.cgColor)?.usingColorSpace(.deviceRGB)
            let hex = color.map { String(format: "#%02x%02x%02x", Int($0.redComponent * 255), Int($0.greenComponent * 255), Int($0.blueComponent * 255)) } ?? "#9881bd"
            return ["id": (event.eventIdentifier ?? "") + ":" + formatter.string(from: event.startDate),
             "external_id": event.calendarItemExternalIdentifier ?? "",
             "title": event.title ?? "Без названия", "color": hex,
             "start": formatter.string(from: event.startDate),
             "end": formatter.string(from: event.endDate), "all_day": event.isAllDay,
             "calendar": event.calendar.title]
        }
    return ["events": events, "timezone": TimeZone.current.identifier,
            "start": formatter.string(from: start), "end": formatter.string(from: end)]
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let args = CommandLine.arguments
guard args.count == 3,
      let data = try? Data(contentsOf: URL(fileURLWithPath: args[1])),
      let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { exit(1) }
let requestAccess = request["request_access"] as? Bool ?? false
let parts = Set(request["parts"] as? [String] ?? ["calendar", "location"])
guard !parts.isEmpty, parts.isSubset(of: ["calendar", "location"]) else { exit(1) }
let location = LocationReader()
var output: [String: Any] = [:]
var finished = 0
func save(_ key: String, _ value: [String: Any]) {
    output[key] = value
    finished += 1
    if finished == parts.count {
        if let data = try? JSONSerialization.data(withJSONObject: output) {
            try? data.write(to: URL(fileURLWithPath: args[2]), options: .atomic)
        }
        app.terminate(nil)
    }
}
DispatchQueue.main.async {
    // Request sequentially, so two privacy dialogs never compete for attention.
    Task { @MainActor in
        if parts.contains("calendar") {
            let calendar = await calendarEvents(requestAccess: requestAccess, day: request["date"] as? String)
            save("calendar", calendar)
        }
        if parts.contains("location") {
            location.read(requestAccess: requestAccess, cityOnly: request["city_only"] as? Bool ?? false) { save("location", $0) }
        }
    }
}
// Bounded even when a permission dialog is left unanswered.
Timer.scheduledTimer(withTimeInterval: 55, repeats: false) { _ in
    if let data = try? JSONSerialization.data(withJSONObject: output) {
        try? data.write(to: URL(fileURLWithPath: args[2]), options: .atomic)
    }
    app.terminate(nil)
}
app.run()
