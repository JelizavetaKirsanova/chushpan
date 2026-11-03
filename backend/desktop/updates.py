"""Explicit release check; no downloads, installation or background network polling."""

import json
import urllib.error
import urllib.request

from backend.desktop import VERSION


def check():
    request = urllib.request.Request(
        "https://api.github.com/repos/JelizavetaKirsanova/chushpan/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Chushpan/" + VERSION},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": True,
                "message": "Опубликованных выпусков пока нет. Текущая версия: " + VERSION,
            }
        raise
    tag = release.get("tag_name", "")
    return {
        "ok": True,
        "message": f"Текущая версия: {VERSION}. Последний выпуск: {tag}. "
        "Открой историю изменений для установки.",
    }
