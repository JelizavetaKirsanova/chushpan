"""Fixed Mac Agent operations added to the existing system group."""

import re

from backend.config import Settings
from backend.mac import MacDesktop
from backend.security import Risk
from backend.tools.registry import Tool, ToolError


def mac_tools(s: Settings, desktop: MacDesktop | None = None) -> list[Tool]:
    mac = desktop or MacDesktop(s)
    cap = {
        "ru": "управлять приложениями и окнами Mac, искать разрешённые файлы и делать снимки экрана"
    }

    def reply(result: dict) -> str:
        return result.get("message", "Результат операции не подтверждён.")

    def window_arguments(args: dict) -> dict:
        needed = {"move": {"x", "y"}, "resize": {"width", "height"}}.get(args["action"], set())
        given = set(args) & {"x", "y", "width", "height"}
        if needed != given:
            raise ToolError(
                "Для move нужны только x/y, для resize — width/height; "
                "другие операции не принимают геометрию."
            )
        return args

    tools = [
        Tool(
            "get_apps",
            "Список запущенных приложений или активное приложение Mac. Только чтение.",
            mac.apps,
            {"scope": {"type": "string", "enum": ["running", "active"]}},
            capability=cap,
        ),
        Tool(
            "manage_app",
            "Переключиться на запущенное приложение (switch) или завершить его (quit). "
            "Quit ждёт подтверждения; force quit отсутствует.",
            mac.app,
            {"name": {"type": "string"}, "action": {"type": "string", "enum": ["switch", "quit"]}},
            ["name", "action"],
            summary=lambda a: (
                f"закрыть приложение «{a['name']}» обычным способом; "
                "несохранённый документ может остановить закрытие"
            ),
            reply=reply,
        ),
        Tool(
            "get_windows",
            "Получить окна указанного запущенного приложения; без name — активного. "
            "Затем manage_window с выданным window_id. ID живёт 120 секунд и одну операцию.",
            mac.list_windows,
            {"name": {"type": "string"}},
        ),
        Tool(
            "manage_window",
            "Управление окном из get_windows. focus переключает окно; minimize/restore "
            "сворачивают/восстанавливают; left/right/maximize задают положение; "
            "move/resize используют координаты в пунктах из screens. "
            "Fullscreen зависит от приложения. move_space не поддерживается.",
            mac.window,
            {
                "window_id": {"type": "string", "pattern": r"[0-9a-f]{32}", "maxLength": 32},
                "action": {
                    "type": "string",
                    "enum": [
                        "focus",
                        "close",
                        "minimize",
                        "restore",
                        "maximize",
                        "left",
                        "right",
                        "move",
                        "resize",
                        "fullscreen",
                        "exit_fullscreen",
                        "move_space",
                    ],
                },
                "x": {"type": "integer", "minimum": -20000, "maximum": 20000},
                "y": {"type": "integer", "minimum": -20000, "maximum": 20000},
                "width": {"type": "integer", "minimum": 200, "maximum": 16000},
                "height": {"type": "integer", "minimum": 80, "maximum": 16000},
            },
            ["window_id", "action"],
            prepare=window_arguments,
            reply=reply,
        ),
        Tool(
            "get_brightness",
            "Прочитать яркость через публичный IOKit API, если он поддерживается дисплеем.",
            mac.brightness,
            reply=reply,
        ),
        Tool(
            "set_brightness",
            "Изменить яркость от 0 до 100 на поддерживаемом дисплее. Результат перечитывается.",
            mac.brightness,
            {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            ["level"],
            reply=reply,
        ),
        Tool(
            "mac_power",
            "Блокировка экрана, сон Mac или выключение. Всегда нужен отдельный свежий код. "
            "Это не go_to_sleep ассистента. Слово «спи» без Mac-запроса не разрешает действие.",
            mac.power,
            {"action": {"type": "string", "enum": ["lock", "sleep", "shutdown"]}},
            ["action"],
            risk=Risk.DANGEROUS,
            summary=lambda a: {
                "lock": "заблокировать экран Mac; сессия станет недоступна до разблокировки",
                "sleep": "перевести Mac в сон; текущая работа ассистента и соединения прервутся",
                "shutdown": "выключить Mac; приложения могут запросить сохранение документов",
            }[a["action"]],
            reply=reply,
        ),
        Tool(
            "take_screenshot",
            "По явной просьбе сохранить PNG основного экрана локально и вернуть путь. "
            "Не анализирует изображение и никуда его не отправляет. Нужен Screen Recording.",
            mac.screenshot,
            {"mode": {"type": "string", "enum": ["screen", "window", "region"]}},
            reply=reply,
        ),
        Tool(
            "find_files",
            "Поиск по части имени в разрешённой папке, без чтения содержимого. "
            "Скрытые файлы, симлинки и пакеты пропускаются. Максимум 20 результатов; "
            "truncated означает неполный поиск.",
            mac.find_files,
            {
                "root": {"type": "string", "enum": list(s.mac.file_roots)},
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            ["root", "name"],
        ),
        Tool(
            "open_path",
            "Открыть файл/папку из разрешённого root; path относителен root, точка — папка. "
            "finder=true показывает объект в Finder. Скрипты и приложения запрещены.",
            mac.open_path,
            {
                "root": {"type": "string", "enum": list(s.mac.file_roots)},
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "format": "safe-path",
                },
                "finder": {"type": "boolean"},
            },
            ["root"],
            intent=mac.path_intent,
            reply=reply,
        ),
    ]
    tools.append(
        Tool(
            "set_external_brightness",
            "Яркость выбранного внешнего дисплея через BetterDisplay с перечитыванием.",
            mac.external_brightness,
            {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            ["level"],
            risk=Risk.CHANGE,
            intent=lambda a, text: bool(
                re.search(
                    r"яркост.*(?:телевизор|внешн)|(?:телевизор|внешн).*яркост", text.casefold()
                )
            ),
            reply=reply,
        )
    )
    for tool in tools:
        tool.required_capability = (
            "mac.apps"
            if tool.name in {"get_apps", "manage_app"}
            else "mac.windows"
            if tool.name in {"get_windows", "manage_window"}
            else "mac.screen"
            if tool.name == "take_screenshot"
            else "mac.files"
            if tool.name in {"find_files", "open_path"}
            else "mac.system"
        )
    return tools
