"""Install a verified local app; never start recording or modify login items."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.macos_release import BUILD_RECEIPT, install, recover_interrupted, rollback_last


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--app", type=Path, help="проверенная сборка; по умолчанию last-build.json")
    source.add_argument("--rollback", action="store_true", help="явный откат к сохранённой копии")
    source.add_argument("--recover", action="store_true", help="восстановить прерванную установку")
    parser.add_argument("--migrate-from", type=Path)
    parser.add_argument(
        "--trash-build", action="store_true", help="после успеха перенести эту сборку в Корзину"
    )
    args = parser.parse_args()
    if args.rollback or args.recover:
        if args.migrate_from or args.trash_build:
            parser.error("Откат не совмещается с переносом данных или очисткой.")
        result = recover_interrupted() if args.recover else rollback_last()
        print(f"Восстановлено: {result['app']} — {result['version']} ({result['build']})")
        return 0
    app = args.app
    if app is None:
        if not BUILD_RECEIPT.is_file():
            raise SystemExit("Сначала: .venv/bin/python -m scripts.build_macos")
        app = Path(json.loads(BUILD_RECEIPT.read_text())["app"])
    if args.migrate_from:
        from backend.desktop.storage import migrate

        target = Path.home() / "Library/Application Support/Chushpan"
        if (target / "data/jarvis.db").exists():
            raise SystemExit("База приложения уже существует. Перенос не заменяет историю.")
        print(f"Скопировано файлов: {len(migrate(args.migrate_from, target))}")
    result = install(app, cleanup=args.trash_build)
    print(f"Обновлено: {result['app']} — {result['version']} ({result['build']})")
    if result.get("permissions_may_need_refresh"):
        print(
            "Подпись изменилась: старое разрешение Accessibility может не действовать. "
            "В Чушпане: Настройки → Разрешения → Проверить разрешения. "
            "При отказе используйте «Обновить разрешение этой копии»."
        )
    if result["previous"]:
        print(f"Предыдущая рабочая копия сохранена: {result['previous']}")
    if not args.trash_build:
        print(f"Кандидат на очистку (пока сохранён): {app}")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
