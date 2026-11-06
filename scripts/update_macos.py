"""The single developer command: temporary build, verification, canonical update."""

from __future__ import annotations

import argparse
import os

from scripts.build_macos import build
from scripts.macos_release import assert_not_running, install


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trash-build",
        action="store_true",
        help="перенести только новую сборку в Корзину после успеха",
    )
    args = parser.parse_args()
    assert_not_running()
    result = install(build(), cleanup=args.trash_build)
    print(f"Обновлено единственное приложение: {result['app']}")
    print(f"Резерв сохранён: {result['previous'] or 'первая установка'}")
    if not args.trash_build:
        print(f"Проверенная сборка сохранена вне поиска: {result['source']}")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
