"""Install a verified local app; never start recording or modify login items."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--applications", type=Path, default=Path("/Applications"))
    parser.add_argument("--migrate-from", type=Path)
    args = parser.parse_args()
    app = ROOT / "dist/Чушпан.app"
    if not (app / "Contents/MacOS/Chushpan").exists():
        raise SystemExit("Сначала собери приложение: .venv/bin/python -m scripts.build_macos")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    destination = args.applications / app.name
    backup = args.applications / "Чушпан.previous.app"
    staging = args.applications / ".Чушпан.installing.app"
    if staging.exists() or backup.exists():
        raise SystemExit(
            "Осталась предыдущая установка. Проверь .Чушпан.installing.app / Чушпан.previous.app."
        )
    if args.migrate_from:
        from backend.desktop.storage import migrate

        target = Path.home() / "Library/Application Support/Chushpan"
        if (target / "data/jarvis.db").exists():
            raise SystemExit("База приложения уже существует. Перенос не заменяет историю.")
        print(f"Скопировано файлов: {len(migrate(args.migrate_from, target))}")
    # ditto preserves framework symlinks and bundle signatures.
    subprocess.run(["/usr/bin/ditto", str(app), str(staging)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staging)], check=True)
    try:
        if destination.exists():
            destination.rename(backup)
        staging.rename(destination)
    except OSError:
        if backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    register = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    subprocess.run([register, "-f", str(destination)], check=True)
    print(
        f"Установлено: {destination}. Открой двойным нажатием. Автозапуск включается в настройках."
    )
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
