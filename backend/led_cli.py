"""Explicit, one-command-at-a-time firmware verification, outside voice and LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.config import Settings, get_settings
from backend.led import LEDStrip, packet, parse
from backend.tools.registry import ToolError


def arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="led_action", required=True)
    sub.add_parser("status", help="локальный журнал, без Bluetooth")
    sub.add_parser("inspect", help="GATT и сырые FFF4 notifications, без команд")
    sub.add_parser("discover", help="поиск нового UUID за 4 секунды, без записи и автопривязки")
    command = sub.add_parser("command", help="обычная команда, только проверенные режимы")
    command.add_argument("text", help='например: "сделай подсветку синей"')
    probe = sub.add_parser("probe", help="один эксперимент с ручным подтверждением до и после")
    probe.add_argument("text", help='например: "выключи подсветку"')


async def run(settings: Settings, args: argparse.Namespace) -> int:
    strip = LEDStrip(settings)
    try:
        if args.led_action == "status":
            result = await strip.execute(action="status")
        elif args.led_action == "inspect":
            result = await strip.inspect()
        elif args.led_action == "discover":
            result = await strip.discover()
        elif args.led_action == "command":
            result = await strip.command(args.text)
        else:
            request = parse(args.text)
            if request is None or request["action"] not in {"color", "on", "off", "brightness"}:
                raise ToolError(
                    "Для эксперимента укажи цвет, включение, выключение или яркость 0–100."
                )
            data = packet(**request)
            print("Устройство:", strip.identity)
            print("Экспериментальный пакет:", data.hex(" ").upper())
            print("Наблюдай ленту. Будет отправлена ровно одна команда, без перебора пакетов.")
            if not sys.stdin.isatty():
                raise ToolError("Эксперимент требует интерактивного терминала и наблюдения ленты.")
            if input("Для отправки введи «ОТПРАВИТЬ»: ").strip() != "ОТПРАВИТЬ":
                print("Отменено. Пакет не отправлял.")
                return 0
            result = await strip.execute(**request, experiment=True)
            print(result["message"])
            observed = input("Лента выполнила именно эту команду? Введи «ДА»: ").strip() == "ДА"
            await strip.confirm_probe(result["probe_id"], observed)
            print("Режим разрешён." if observed else "Режим не подтверждён и заблокирован.")
            return 0 if observed else 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ToolError, EOFError) as exc:
        print(str(exc) or "Проверка прервана; режим не подтверждён.", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.led_cli")
    arguments(parser)
    try:
        return asyncio.run(run(get_settings(), parser.parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
