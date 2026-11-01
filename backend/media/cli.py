"""Safe music diagnostics: no playback, volume changes or credential output."""

import json

from backend.config import Settings
from backend.media.youtube import YouTubeMusic
from backend.tools.registry import ToolError


async def check_music(
    s: Settings, library: bool = False, *, json_output=False, live=None, request_access=False
) -> int:
    if s.media.youtube_music.transport == "safari":
        from backend.media.music_safari import SafariMusicTab, SafariYouTubeMusic
        from backend.music import MusicCommands
        from backend.tools.media import MediaService

        app = SafariMusicTab(s)
        if request_access:
            try:
                await app.request_access()
            except ToolError as exc:
                print(json.dumps({"error_code": exc.code, "message": str(exc)}, ensure_ascii=False))
                return 1
        report = await app.diagnose()
        if library and not report.get("error_code") and report.get("tab_found"):
            items = await app.call("items", kind="playlist")
            report["visible_playlist_count"] = len(items.get("items", []))
        if live:
            # Only this explicitly supplied CLI flag can navigate/search/start playback.
            media = MediaService(s)
            media.youtube = SafariYouTubeMusic(s, app=app)
            result = await MusicCommands(s, media).execute(live)
            report.update(await app.diagnose())
            report.update(app.stages)
            report["live_ok"] = bool(result.get("ok"))
            report["error_code"] = result.get("code") or (
                None if result.get("ok") else result.get("status", "playback_unconfirmed")
            )
            if not result.get("ok"):
                report["message"] = result.get("error", "Музыкальная команда не подтверждена.")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("error_code") else 0
    print(f"Музыкальный провайдер: {s.media.provider}")
    if s.media.provider != "youtube_music" or not s.media.youtube_music.enabled:
        print("YouTube Music не выбран или отключён.")
        return 0
    music = YouTubeMusic(s)
    try:
        path, bundle = music.app.discover()
        print(f"Приложение: {path}\nBundle ID: {bundle}")
        status = await music.app.request("status")
        print(f"Универсальный доступ: {'разрешён' if status['trusted'] else 'не разрешён'}")
        print(f"Основной плейлист: {music.preference('default') or 'ещё не назначен'}")
        print(f"Плеер: {await music.app.now_playing()}")
        if library:
            data = await music.library()
            print("Плейлисты:")
            for item in data["playlists"]:
                print(f"  {item['title']} ({item['id']})")
            if not data.get("complete", True):
                print("Список из окна приложения может быть загружен частично.")
    except ToolError as exc:
        print(f"Не удалось проверить: {exc}")
        return 1
    return 0
