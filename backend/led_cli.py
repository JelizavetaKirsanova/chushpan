"""Explicit, one-command-at-a-time firmware verification, outside voice and LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from backend.ble_profiles import DDM_PROFILE, BLEProfile, legacy_profile
from backend.config import Settings, get_settings
from backend.led import LEDStrip, parse
from backend.tools.registry import ToolError


def arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="led_action", required=True)
    sub.add_parser("status", help="локальный журнал, без Bluetooth")
    sub.add_parser("inspect", help="все GATT-сервисы, чтения и уведомления, без команд")
    sub.add_parser("discover", help="поиск нового UUID за 4 секунды, без записи и автопривязки")
    command = sub.add_parser("command", help="обычная команда, только проверенные режимы")
    command.add_argument("text", help='например: "сделай подсветку синей"')
    probe = sub.add_parser("probe", help="один эксперимент с ручным подтверждением до и после")
    probe.add_argument("text", help='например: "выключи подсветку"')
    profile = sub.add_parser("profile", help="явный выбор или импорт профиля; допуски сбрасываются")
    profile.add_argument(
        "profile", help="auto, elk-bledob-legacy, elk-bleddm-candidate или путь JSON"
    )
    for parser in sub.choices.values():
        parser.add_argument("--device", help="локальный id выбранной ленты из реестра устройств")


def selected_strip(settings, identifier):
    if not identifier:
        return LEDStrip(settings)
    from backend.devices.store import DeviceStore
    from backend.memory.store import MemoryStore

    memory = MemoryStore(settings.db_path)
    try:
        device = DeviceStore(memory).get(identifier)
        if device.kind != "led_strip":
            raise ToolError("Выбрана не BLE-лента.")
        settings = settings.model_copy(deep=True)
        primary = device.endpoint == str(settings.led.address)
        settings.led.address, settings.led.enabled = UUID(device.endpoint), True
        if not primary:
            settings.led.name, settings.led.firmware = device.ble_name or "unknown", "unknown"
        strip = LEDStrip(settings)
        if not primary:
            strip.path = settings.data_path / "devices" / device.id / "led.json"
        return strip
    finally:
        memory.close()


async def run(settings: Settings, args: argparse.Namespace) -> int:
    try:
        strip = selected_strip(settings, getattr(args, "device", None))
        if args.led_action == "status":
            result = await strip.execute(action="status")
        elif args.led_action == "inspect":
            result = await strip.inspect()
        elif args.led_action == "discover":
            result = await strip.discover()
        elif args.led_action == "command":
            result = await strip.command(args.text)
        elif args.led_action == "profile":
            choices = {
                "auto": None,
                "elk-bledob-legacy": legacy_profile(strip.cfg),
                "elk-bleddm-candidate": DDM_PROFILE,
            }
            if args.profile in choices:
                profile = choices[args.profile]
            else:
                path = Path(args.profile)
                if path.stat().st_size > 32768:
                    raise ToolError("Профиль больше 32 КБ.")
                profile = BLEProfile.model_validate_json(path.read_text())
            await strip.select_profile(profile)
            result = {
                "message": "Профиль сохранён без команд. Допуски сброшены; "
                "нужна диагностика и проверка."
            }
        else:
            request = parse(args.text)
            if request is None or request["action"] not in {"color", "on", "off", "brightness"}:
                raise ToolError(
                    "Для эксперимента укажи цвет, включение, выключение или яркость 0–100."
                )
            state = strip._load()
            data = strip.serialize(state, **request)
            print("Драйвер:", strip.driver(state).name)
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
        return 1 if result.get("result_status") == "failed" else 0
    except ValidationError:
        print(
            "Некорректный BLE-профиль; проверь схему JSON. Команды не отправлял.", file=sys.stderr
        )
        return 1
    except (ToolError, EOFError, OSError, ValueError, KeyError) as exc:
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
