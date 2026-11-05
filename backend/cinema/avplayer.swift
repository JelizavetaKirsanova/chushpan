// Owned AVKit window. JSON-lines stdin/stdout only; no website scripts, cookies or DRM loaders.
import AppKit
import AVKit
import AVFoundation

final class CinemaPlayer: NSObject, NSWindowDelegate {
    var player: AVPlayer?
    var window: NSWindow?
    var view: AVPlayerView?
    var closed = false
    var seeking = false
    var loading = false
    var failure = ""
    var loadGeneration = 0
    var audioGroup: AVMediaSelectionGroup?

    func reply(_ id: String, _ extra: [String: Any] = [:]) {
        var value = snapshot()
        value["id"] = id
        for (key, entry) in extra { value[key] = entry }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data + Data([10]))
        }
    }

    func snapshot() -> [String: Any] {
        let duration = player?.currentItem?.duration.seconds ?? .nan
        let position = player?.currentTime().seconds ?? .nan
        let ready = player?.currentItem?.status == .readyToPlay
        let tracks = audioGroup
        let selected = tracks.flatMap { player?.currentItem?.currentMediaSelection.selectedMediaOption(in: $0) }
        var value: [String: Any] = [
            "position": position.isFinite ? max(0, position) : 0,
            "duration": duration.isFinite ? max(0, duration) : 0,
            "paused": player?.rate == 0,
            "seeking": seeking,
            "ready": ready ? 4 : 0,
            "ended": duration.isFinite && duration > 0 && position >= duration - 0.1,
            "closed": closed,
            "loading": loading,
            "observed_at": Date().timeIntervalSince1970 * 1000,
            "fullscreen": window?.styleMask.contains(.fullScreen) == true,
            "voice": selected?.displayName ?? "",
            "capabilities": [
                "play": ready, "pause": ready,
                "seek": ready && !(player?.currentItem?.seekableTimeRanges.isEmpty ?? true),
                "position": ready && duration.isFinite,
                "fullscreen": window?.styleMask.contains(.fullScreen) == true,
                "fullscreen_available": window != nil,
                "audio_tracks": tracks?.options.map { $0.displayName } ?? []
            ]
        ]
        if !failure.isEmpty { value["error"] = failure }
        else if player?.currentItem?.status == .failed { value["error"] = "media_error" }
        return value
    }

    func open(_ request: [String: Any], id: String) {
        guard let text = request["url"] as? String else { reply(id, ["error": "invalid_url"]); return }
        let url: URL?
        if text.hasPrefix("/") { url = URL(fileURLWithPath: text) }
        else { url = URL(string: text) }
        guard let url, ["https", "http", "file"].contains(url.scheme ?? ""),
              url.user == nil, url.password == nil,
              ["mp4", "m4v", "mov", "m3u8"].contains(url.pathExtension.lowercased()) else {
            reply(id, ["error": "unsupported_source"]); return
        }
        player?.pause()
        player = nil
        audioGroup = nil
        view?.player = nil
        failure = ""
        closed = false
        seeking = false
        loading = true
        loadGeneration += 1
        let generation = loadGeneration
        let asset = AVURLAsset(url: url)
        Task { @MainActor in
            do {
                let protected = try await asset.load(.hasProtectedContent)
                guard generation == self.loadGeneration else { return }
                guard !protected else {
                    self.failure = "protected_player"; self.loading = false; return
                }
                let playable = try await asset.load(.isPlayable)
                guard generation == self.loadGeneration else { return }
                guard playable else {
                    self.failure = "unsupported_source"; self.loading = false; return
                }
                let group = try await asset.loadMediaSelectionGroup(for: .audible)
                guard generation == self.loadGeneration else { return }
                self.audioGroup = group
                let item = AVPlayerItem(asset: asset)
                self.player = AVPlayer(playerItem: item)
                self.player?.isMuted = request["muted"] as? Bool ?? false
                let frame = NSRect(x: 0, y: 0, width: 960, height: 600)
                let window = NSWindow(contentRect: frame,
                    styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
                window.title = "Чушпан · " + (request["title"] as? String ?? "Просмотр")
                window.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.05, blue: 0.13, alpha: 1)
                window.isReleasedWhenClosed = false
                window.delegate = self
                window.collectionBehavior.insert(.fullScreenPrimary)
                let view = AVPlayerView(frame: frame)
                view.autoresizingMask = [.width, .height]
                view.controlsStyle = .floating
                view.showsFullScreenToggleButton = true
                view.player = self.player
                window.contentView = view
                self.window?.close()
                self.window = window
                self.view = view
                self.closed = false
                self.loading = false
                window.center()
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            } catch {
                if generation == self.loadGeneration { self.failure = "media_error"; self.loading = false }
            }
        }
        reply(id)
    }

    func command(_ request: [String: Any]) {
        let id = request["id"] as? String ?? ""
        switch request["action"] as? String ?? "" {
        case "open": open(request, id: id)
        case "status": reply(id)
        case "play": player?.play(); reply(id)
        case "pause": player?.pause(); reply(id)
        case "seek":
            guard let position = request["position"] as? Double,
                  position.isFinite, position >= 0, let player,
                  player.currentItem?.status == .readyToPlay else {
                reply(id, ["error": "invalid_position"]); return
            }
            seeking = true
            player.seek(to: CMTime(seconds: position, preferredTimescale: 600),
                        toleranceBefore: .zero, toleranceAfter: .zero) { finished in
                DispatchQueue.main.async {
                    self.seeking = false
                    self.reply(id, finished ? [:] : ["error": "seek_interrupted"])
                }
            }
        case "fullscreen":
            if window?.styleMask.contains(.fullScreen) != true { window?.toggleFullScreen(nil) }
            reply(id)
        case "voice":
            guard let voice = request["voice"] as? String,
                  let item = player?.currentItem,
                  let group = audioGroup,
                  let option = group.options.first(where: { $0.displayName == voice }) else {
                reply(id, ["error": "voice_unavailable"]); return
            }
            item.select(option, in: group); reply(id)
        case "close":
            player?.pause(); closed = true; reply(id); NSApp.terminate(nil)
        default: reply(id, ["error": "unsupported_command"])
        }
    }

    func windowWillClose(_ notification: Notification) {
        player?.pause()
        closed = true
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let cinema = CinemaPlayer()
DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine() {
        guard let data = line.data(using: .utf8), data.count < 16384,
              let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
        DispatchQueue.main.async { cinema.command(request) }
    }
    DispatchQueue.main.async { cinema.player?.pause(); NSApp.terminate(nil) }
}
app.run()
