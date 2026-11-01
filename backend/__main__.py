"""Entry point: `uv run python -m backend <command>`."""

from __future__ import annotations

import argparse
import os
import sys
import time

from backend.config import get_settings
from backend.logging_setup import setup_logging

STARTED_AT = time.perf_counter()  # the briefing reports its latency from here

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def _run(coro) -> int:
    import asyncio

    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="чушпан")
    sub = parser.add_subparsers(dest="command", required=True)
    latency_check = sub.add_parser(
        "latency-check", help="замер задержек без записи и воспроизведения"
    )
    latency_check.add_argument(
        "--live", action="store_true", help="нейтральный тестовый запрос модели"
    )
    latency_check.add_argument(
        "--tts", action="store_true", help="синтез в RAM без воспроизведения"
    )
    latency_check.add_argument("--runs", type=int, choices=range(1, 6), default=1)
    latency_check.add_argument(
        "--stream", action="store_true", help="текстовый поток вместо буферизации"
    )
    latency_check.add_argument("--label", choices=["before", "after", "sample"], default="sample")
    latency_check.add_argument("--stt-file", help="явно выбранный mono PCM16 WAV; без микрофона")
    sub.add_parser("doctor", help="проверить Ollama, модель, голос и местоположение")
    mac_check = sub.add_parser(
        "mac-check", help="безопасная диагностика Mac и доступных инструментов"
    )
    mac_check.add_argument("--json", action="store_true", help="вывести структурированный отчёт")
    permissions = sub.add_parser(
        "permissions", help="проверить разрешения macOS без запросов доступа"
    )
    permissions.add_argument(
        "--automation",
        action="append",
        default=[],
        choices=["com.spotify.client", "com.apple.systemevents", "com.apple.Safari"],
        help="проверить Automation для уже работающего приложения",
    )
    night_check = sub.add_parser(
        "night-check", help="проверить ночной сценарий без запуска и записи памяти"
    )
    night_check.add_argument(
        "--json", action="store_true", help="все проверки и причины блокировки"
    )
    night_check.add_argument(
        "--pid", type=int, help="цепочка PID без чтения аргументов и TCC другого процесса"
    )
    music_check = sub.add_parser("music-check", help="проверить YouTube Music без запуска музыки")
    music_check.add_argument("--library", action="store_true", help="также прочитать плейлисты")
    music_check.add_argument("--json", action="store_true", help="безопасный диагностический отчёт")
    music_check.add_argument(
        "--request-access",
        action="store_true",
        help="явно запросить Automation Safari чтением числа окон; без музыки",
    )
    music_check.add_argument(
        "--live", metavar="COMMAND", help="явно выполнить музыкальную команду; может включить звук"
    )
    tts_check = sub.add_parser(
        "tts-check", help="диагностика речевой копии без микрофона и воспроизведения"
    )
    tts_check.add_argument(
        "--sample", choices=["calendar", "abbreviations", "mixed"], default="calendar"
    )
    tts_check.add_argument("--text-file", help="свой текст; по умолчанию содержание не выводится")
    tts_check.add_argument(
        "--show-text", action="store_true", help="явно показать свой текст после удаления секретов"
    )
    tts_check.add_argument(
        "--synthesize", action="store_true", help="синтезировать WAV без воспроизведения"
    )
    tts_check.add_argument("--output-dir", default="docs/audio/tts-regression")
    sub.add_parser("config", help="показать текущие настройки")
    chat = sub.add_parser("chat", help="текстовый чат в терминале")
    chat.add_argument("-r", "--resume", action="store_true", help="продолжить последний разговор")
    chat.add_argument("-s", "--speak", action="store_true", help="озвучивать ответы")
    voice = sub.add_parser("voice", help="говорить с Чушпаном голосом")
    voice.add_argument("-r", "--resume", action="store_true", help="продолжить последний разговор")
    voice.add_argument(
        "--no-wake", action="store_true", help="слушать постоянно, без фразы активации"
    )
    voice.add_argument("--no-briefing", action="store_true", help="пропустить погоду и новости")
    voice.add_argument("--no-hud", action="store_true", help="не открывать интерфейс")
    voice.add_argument("--at-login", action="store_true", help=argparse.SUPPRESS)
    autostart = sub.add_parser("autostart", help="запускать Чушпана при входе в macOS")
    autostart.add_argument(
        "action",
        nargs="?",
        choices=["status", "install", "uninstall", "start", "stop"],
        default="status",
    )
    autostart.add_argument("--no-briefing", action="store_true", help="не читать сводку при входе")
    autostart.add_argument("--no-hud", action="store_true", help="не открывать интерфейс при входе")
    briefing = sub.add_parser("briefing", help="показать сводку и время её подготовки")
    briefing.add_argument("-s", "--speak", action="store_true", help="также озвучить")
    demo = sub.add_parser("hud-demo", help="демонстрация интерфейса без микрофона")
    demo.add_argument("--sample", action="store_true", help="тестовые данные без интернета")
    window = sub.add_parser("hud-window", help="открыть интерфейс в отдельном окне")
    window.add_argument("--url", help="по умолчанию: адрес сервера из настроек")
    window.add_argument("--parent", type=int, help=argparse.SUPPRESS)  # close when it exits
    sub.add_parser("wake-test", help="проверить чувствительность фразы активации")
    sub.add_parser("echo-test", help="проверить эхоподавление в помещении")
    memory = sub.add_parser("memory", help="посмотреть или удалить сохранённые факты")
    memory.add_argument(
        "action", nargs="?", choices=["list", "show", "forget", "clear"], default="list"
    )
    memory.add_argument("id", nargs="?", type=int, help="номер факта для удаления (см. memory)")
    night = sub.add_parser(
        "night", help="посмотреть или сбросить журнал ночного сценария; без запуска"
    )
    night.add_argument("action", nargs="?", choices=["status", "reset"], default="status")
    night.add_argument(
        "--confirm-stopped", action="store_true", help="Youtube sleep уже остановлен вручную"
    )
    sub.add_parser("prompt", help="показать системную инструкцию ассистента")
    sub.add_parser("footprint", help="показать потребление памяти ассистентом и моделью")
    bench = sub.add_parser("bench", help="сравнить время отклика языковых моделей")
    bench.add_argument("models", nargs="*", help="модели Ollama (по умолчанию: из настроек)")
    stt_check = sub.add_parser("stt-check", help="диагностика STT без записи по умолчанию")
    stt_check.add_argument("--record", action="store_true", help="одна запись до 20 с в RAM")
    stt_check.add_argument("--models", nargs="+", help="сравнить модели на одной записи")
    bench_stt = sub.add_parser("bench-stt", help="сравнить модели Whisper на одной записи")
    bench_stt.add_argument(
        "models", nargs="*", help="модели Hugging Face (по умолчанию: три размера)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level)
    if args.command == "latency-check":
        from backend.latency_check import run

        return _run(
            run(
                settings,
                live=args.live,
                tts=args.tts,
                runs=args.runs,
                label=args.label,
                stream=args.stream,
                stt_file=args.stt_file,
            )
        )

    if args.command == "stt-check":
        from backend.stt_check import run

        return _run(run(settings, args.record, args.models))

    if args.command == "permissions":
        from backend.permissions import run

        return _run(run(tuple(args.automation)))

    if args.command == "night-check":
        from backend.mac import MacDesktop
        from backend.night import BetterDisplay
        from backend.night_preflight import NightPreflight
        from backend.shortcuts import ShortcutsService

        report = _run(
            NightPreflight(
                settings, BetterDisplay(MacDesktop(settings)), ShortcutsService(settings)
            ).check()
        )
        if args.pid is not None:
            from backend.permissions import process_chain

            report["requested_process_chain"] = _run(process_chain(args.pid))
            report["requested_process_note"] = (
                "TCC выбранного PID извне не проверяется. Статусы относятся к процессу диагностики."
            )
        if args.json:
            import json

            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(report["message"])
        return 0 if report["ok"] else 1

    if args.command == "mac-check":
        from backend.mac_check import run

        return _run(run(settings, json_output=args.json))

    if args.command == "doctor":
        from backend import doctor

        return doctor.run()
    if args.command == "music-check":
        from backend.media.cli import check_music

        return _run(
            check_music(
                settings,
                args.library,
                json_output=args.json,
                live=args.live,
                request_access=args.request_access,
            )
        )
    if args.command == "tts-check":
        from backend.tts_check import run

        return run(settings, args)
    if args.command == "chat":
        from backend.chat import run_chat

        return _run(run_chat(settings, resume=args.resume, speak=args.speak))
    if args.command == "voice":
        from backend.voice import run_voice

        if args.at_login:
            from backend.autostart import log_path, rotate_log

            rotate_log(log_path(settings))
        wake_word = False if args.no_wake else None
        briefing = False if args.no_briefing else None
        return _run(
            run_voice(
                settings,
                resume=args.resume,
                wake_word=wake_word,
                briefing=briefing,
                started_at=STARTED_AT,
                hud=False if args.no_hud else None,
                at_login=args.at_login,
            )
        )
    if args.command == "autostart":
        from backend.autostart import run_autostart

        voice_args = [
            flag
            for flag, on in (("--no-briefing", args.no_briefing), ("--no-hud", args.no_hud))
            if on
        ]
        return run_autostart(settings, args.action, voice_args)
    if args.command == "briefing":
        from backend.chat import run_briefing

        return _run(run_briefing(settings, speak=args.speak))
    if args.command == "footprint":
        from backend.footprint import run_footprint

        return run_footprint(settings)
    if args.command == "hud-demo":
        from backend.hud.demo import run_demo

        return _run(run_demo(settings, sample=args.sample))
    if args.command == "hud-window":
        from backend.hud.window import run_window

        url = args.url or f"http://{settings.server.host}:{settings.server.port}"
        return run_window(url, settings.assistant_name, args.parent)
    if args.command == "wake-test":
        from backend.wake_test import run_wake_test

        return _run(run_wake_test(settings))
    if args.command == "echo-test":
        from backend.echo_test import run_echo_test

        return _run(run_echo_test(settings))
    if args.command == "bench":
        from backend.bench import run_bench

        return _run(run_bench(settings, args.models))
    if args.command == "bench-stt":
        from backend.bench_stt import run_bench_stt

        return _run(run_bench_stt(settings, args.models))
    if args.command == "memory":
        from backend.memory.cli import run_memory

        return run_memory(settings, args.action, args.id)
    if args.command == "night":
        from backend.night import run_night_cli

        return run_night_cli(settings, args.action, args.confirm_stopped)
    if args.command in {"prompt", "config"}:
        import asyncio

        from backend.tools.geocode import resolve_location

        asyncio.run(resolve_location(settings))
    if args.command == "prompt":
        from backend.brain.prompts import build_context_note, build_system_prompt
        from backend.memory.store import MemoryStore

        facts = None
        if "memory" in settings.tools.enabled:
            store = MemoryStore(settings.db_path)
            facts = store.facts()
            store.close()
        print(build_system_prompt(settings, facts=facts))
        print(build_context_note(settings, first_turn=True))
        return 0
    if args.command == "config":
        print(settings.model_dump_json(indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
