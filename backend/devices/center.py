"""Device centre extensions; shared by HUD, local voice and the scenario engine."""

import asyncio
import json
import re
import time

import httpx

from backend.context import request_context, voice_addressed
from backend.devices.adapters import MacDevicesAdapter, WLEDAdapter
from backend.devices.colors import ColorInput, hex_rgb
from backend.security import Risk
from backend.tools.registry import Tool, ToolError

COMMAND_FIELDS = {
    "target": {"type": "string", "maxLength": 64},
    "action": {
        "type": "string",
        "enum": [
            "led",
            "brightness",
            "volume",
            "playback",
            "audio_output",
            "airplay_audio",
            "airplay_video",
            "inspect",
            "save_color",
            "light_preset",
            "light_fade",
            "light_effect",
            "light_stop",
            "light_timer",
        ],
    },
    "power": {"type": "boolean"},
    "color": {"type": "string", "pattern": "#[0-9A-Fa-f]{6}"},
    "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
    "level": {"type": "integer", "minimum": 0, "maximum": 100},
    "mute": {"type": "boolean"},
    "playback": {"type": "string", "enum": ["pause", "resume", "stop", "next", "previous"]},
    "name": {"type": "string", "minLength": 1, "maxLength": 80},
    "preset_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
    "duration_s": {"type": "integer", "minimum": 2, "maximum": 86400},
    "delay_s": {"type": "integer", "minimum": 2, "maximum": 31536000},
    "period_s": {"type": "integer", "minimum": 8, "maximum": 3600},
    "effect": {
        "type": "string",
        "enum": ["cycle", "pulse", "breathing", "rainbow", "twinkle", "transition"],
    },
    "colors": {
        "type": "array",
        "items": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
        "maxItems": 16,
    },
}


class DeviceCenter:
    def init_center(self):
        self.tools.devices = self
        self.native = MacDevicesAdapter(self.s)
        self.network = WLEDAdapter()
        self.outputs = []
        self.command_locks = {}
        self.metrics = {}
        if "system" not in self.s.tools.enabled:
            return
        tool = Tool(
            "device_command",
            "Управление устройством или группой по ID; только подтверждённые возможности.",
            self.device_command,
            COMMAND_FIELDS,
            ["target", "action"],
            risk=Risk.CHANGE,
            intent=self.command_intent,
            risk_resolver=lambda args: (
                Risk.READ if args.get("action") == "inspect" else Risk.CHANGE
            ),
        )
        tool.required_capability = "mac.system"
        tool.agent = self.tools.mac_agent
        tool.settings_store = self.memory
        if hasattr(tool.agent, "register"):
            tool.agent.register(tool.name, tool._handler)
        self.tools.add(tool)
        self.update_catalogue()

    def update_catalogue(self):
        tool = self.tools._tools.get("device_command")
        if tool:
            tool.parameters["target"]["enum"] = [d.id for d in self.store.list()] + [
                g.id for g in self.store.collections("group")
            ]
        if self.tools.automations:
            from backend.automation.capabilities import CapabilityRegistry

            self.tools.automations.capabilities.items = CapabilityRegistry(self.tools, self.s).items

    def command_intent(self, args, text):
        if text == "Управляй выбранным устройством":
            return True  # Generated exclusively for an authenticated typed HUD action.
        plan = self.voice_plan(text)
        return bool(
            plan and not plan.get("error") and any(args == item for item in plan["commands"])
        )

    def center_snapshot(self):
        return {
            "groups": [g.model_dump() for g in self.store.collections("group")],
            "rooms": [r.model_dump() for r in self.store.collections("room")],
            "profiles": [p.model_dump() for p in self.locations.list()],
            "outputs": self.outputs,
            "metrics": self.metrics,
            "user_colors": [c.model_dump() for c in self.colors.list()],
        }

    def device_names(self, target):
        names = [target.name, *getattr(target, "aliases", [])]
        model = getattr(target, "ble_name", None)
        if getattr(target, "kind", None) == "led_strip" and target.endpoint == str(
            self.s.led.address
        ):
            model = model or self.s.led.name
        if model:
            names.append(model)
        return names

    def remember_color_target(self, device):
        """An explicit HUD preview chooses context, but authorizes no hardware action."""
        if not self.conv or device.kind != "led_strip":
            return
        controls = self.conv.controls
        self.preferred = (device.id, (self.conv.device.device_id, self.conv.device.session_id))
        controls.device = "led_strip"
        controls.binding = ((self.conv.device.device_id, self.conv.device.session_id), self.conv.id)
        controls.expires = controls.clock() + controls.ttl_s

    async def center_handle(self, value):
        op = value.get("op")
        request_id = value.get("request_id")
        memo = None
        identity = ""
        if op in {"audio_select", "act_group", "system_audio"}:
            if not self.tools.enabled("device_command"):
                raise ValueError("Управление устройствами отключено в настройках доступа.")
            if not isinstance(request_id, str) or not re.fullmatch(
                r"[a-zA-Z0-9-]{8,80}", request_id
            ):
                raise ValueError("Для команды нужен уникальный идентификатор события.")
            memo = (self.conv.device.session_id, request_id)
            identity = json.dumps(value, sort_keys=True)
            if memo in self.completed:
                if self.completed[memo][0] != identity:
                    raise ValueError("Идентификатор уже относится к другой команде.")
                self.publish(result=self.completed[memo][1], request_id=request_id)
                return True
            self.completed[memo] = (
                identity,
                {"result_status": "unverified", "message": "Запрос уже принят. Повтора нет."},
            )
            while len(self.completed) > 128:
                self.completed.popitem(last=False)
        if op == "collection":
            item = value.get("value", {})
            self.valid_place(item.get("place_id", "general"))
            if value.get("kind") == "room":
                old = next(
                    (r for r in self.store.collections("room") if r.id == item.get("id")), None
                )
                if old and any(
                    (d.place_id, d.room) == (old.place_id, old.name)
                    and (
                        d.id in self.inflight
                        or self.command_locks.get(d.id)
                        and self.command_locks[d.id].locked()
                    )
                    for d in self.store.list()
                ):
                    raise ValueError("Дождись завершения команд устройств в этой комнате.")
            self.store.save_collection(value["kind"], item, value.get("delete") is True)
            self.update_catalogue()
            self.publish(message="Список сохранён.", request_id=request_id)
        elif op == "profile":
            from backend.automation.models import Place

            self.locations.save(Place.model_validate(value["value"]))
            self.publish(message="Местоположение сохранено.", request_id=request_id)
        elif op == "location_mode":
            self.locations.select(value["mode"], value.get("place_id", "general"))
            self.publish(message="Способ выбора места сохранён.", request_id=request_id)
        elif op == "history":
            self.store.get(value["id"])
            self.publish(
                history={"id": value["id"], "items": self.store.history(value["id"])},
                request_id=request_id,
            )
        elif op == "audio_refresh":
            self.require_adapter()
            self.outputs = (await self.native.run("outputs"))["outputs"]
            self.publish(message="Аудиовыходы прочитаны из macOS.", request_id=request_id)
        elif op == "audio_select":
            self.require_adapter()
            # Only a fresh system inventory can authorize a selection, never a client name.
            outputs = (await self.native.run("outputs"))["outputs"]
            if value.get("uid") not in {o["uid"] for o in outputs}:
                raise ValueError("Этот выход сейчас не доступен системе.")
            result = await self.native.run("select", value["uid"])
            self.outputs = result["outputs"]
            result.update(
                result_status="success" if result.get("device_state_confirmed") else "unverified",
                message="macOS подтвердила выход звука."
                if result.get("device_state_confirmed")
                else "Выбор не подтверждён.",
            )
            self.completed[memo] = (identity, result)
            self.publish(result=result, request_id=request_id)
        elif op == "system_audio":
            self.require_adapter()
            result = await self.native.run("displays" if value.get("video") is True else "sound")
            self.completed[memo] = (identity, {**result, "result_status": "unverified"})
            self.publish(result={**result, "result_status": "unverified"}, request_id=request_id)
        elif op == "act_group":
            group = next(
                (g for g in self.store.collections("group") if g.id == value.get("id")), None
            )
            if not group:
                raise ValueError("Группа удалена. Обнови список.")
            allowed = {"led", "brightness", "volume", "playback", "inspect"}
            if value.get("action") not in allowed or not isinstance(
                value.get("parameters", {}), dict
            ):
                raise ValueError("Неподдерживаемая команда группы.")
            from backend.devices.models import Device

            anchor = Device(name=group.name, kind="mac", protocol="local", endpoint="local-mac")
            result = await self.call(
                anchor,
                "device_command",
                {"target": group.id, "action": value["action"], **value.get("parameters", {})},
                "Управляй выбранным устройством",
            )
            self.completed[memo] = (identity, result)
            self.publish(result=result, request_id=request_id)
        elif op == "cancel":
            task = self.operations.get(value.get("operation_id"))
            if task and task is not asyncio.current_task():
                task.cancel()
            self.publish(
                message="Ожидание отменено. Уже отправленную команду отменить нельзя.",
                request_id=request_id,
            )
        else:
            return False
        return True

    async def device_command(self, target, action, **parameters):
        context = request_context.get()
        if context is None or not context.device.authenticated:
            raise ToolError("Нужен авторизованный локальный запрос.")
        self.require_adapter()
        if action.startswith("light_"):
            from datetime import timedelta

            from backend.devices.lighting import now
            from backend.devices.lighting_models import LightAction

            targets = self.lighting.targets([target])
            kind = {
                "light_preset": "preset",
                "light_fade": "fade",
                "light_effect": "effect",
                "light_stop": "stop",
                "light_timer": "set",
            }.get(action)
            if kind is None:
                raise ToolError("Неизвестное действие освещения.")
            delay = parameters.pop("delay_s", None)
            item = LightAction(kind=kind, **parameters)
            if action == "light_timer":
                if type(delay) is not int or not 2 <= delay <= 31536000:
                    raise ToolError("Укажи задержку таймера в секундах.")
                job = self.lighting.save(
                    "job",
                    {
                        "name": "Голосовой таймер / сценарий",
                        "targets": targets,
                        "action": item.model_dump(),
                        "next_at": (now() + timedelta(seconds=delay)).isoformat(),
                    },
                )
                result = {
                    "result_status": "success",
                    "message": "Таймер сохранён. Его выполняет работающий Mac.",
                    "job_id": job.id,
                    "scheduled": True,
                }
            else:
                result = await self.lighting.apply(targets, item)
            self.publish()
            return result
        groups = {g.id: g for g in self.store.collections("group")}
        identifiers = groups[target].device_ids if target in groups else [target]
        if action == "save_color" and target in groups:
            raise ToolError("Для сохранения цвета выбери одну RGB-ленту.")
        if not identifiers:
            raise ToolError("В группе пока нет устройств.")
        if "playback" in parameters:
            parameters["action"] = parameters.pop("playback")
        results = []
        for identifier in identifiers:
            started = time.monotonic()
            device = self.store.get(identifier)
            try:
                self.check_location(device)
                lock = self.command_locks.setdefault(identifier, asyncio.Lock())
                if lock.locked():
                    raise ToolError("Устройство занято другой командой.")
                async with lock, asyncio.timeout(45):
                    if action == "save_color":
                        if device.kind not in {"led_strip", "wled"} or set(parameters) != {"name"}:
                            raise ToolError("Выбери одну RGB-ленту и название цвета.")
                        value = (
                            self.colors.preview(device.id)
                            or device.sent.get("color")
                            or device.state.get("color")
                        )
                        if value is None:
                            raise ToolError(
                                "Сначала выбери цвет в RGB-редакторе или задай его ленте."
                            )
                        saved = self.colors.save(
                            {"name": parameters["name"], "rgb": hex_rgb(value)}
                        )
                        result = {
                            "message": f"Цвет «{saved.name}» сохранён. Ленте ничего не отправлено.",
                            "result_status": "success",
                        }
                    elif action == "inspect":
                        result = await self.refresh(device)
                    elif device.kind == "wled":
                        result = await self.network.execute(device, action, parameters)
                        self.accept_network_state(device, result)
                    elif (
                        device.kind == "led_strip"
                        and action == "led"
                        and set(parameters) == {"color"}
                    ):
                        result = await self.apply_color(device, parameters["color"])
                    elif device.kind == "mac" and action == "audio_output" and not parameters:
                        self.outputs = (await self.native.run("outputs"))["outputs"]
                        builtin = [o for o in self.outputs if o.get("transport") == "builtin"]
                        if len(builtin) != 1:
                            raise ToolError("Выбери встроенные динамики в меню «Вывод звука».")
                        result = await self.native.run("select", builtin[0]["uid"])
                        self.outputs = result["outputs"]
                        result["result_status"] = (
                            "success" if result.get("device_state_confirmed") else "unverified"
                        )
                        result["message"] = (
                            "macOS подтвердила вывод звука на Mac."
                            if result.get("device_state_confirmed")
                            else "Вывод не подтверждён."
                        )
                    elif (
                        device.kind == "audio_output"
                        and action == "audio_output"
                        and not parameters
                    ):
                        result = await self.native.run("select", device.endpoint)
                        self.outputs = result["outputs"]
                        result["result_status"] = (
                            "success" if result.get("device_state_confirmed") else "unverified"
                        )
                        result["message"] = (
                            "macOS подтвердила аудиовыход."
                            if result.get("device_state_confirmed")
                            else "Выбор выхода не подтверждён."
                        )
                    elif (
                        device.kind == "airplay"
                        and action in {"airplay_audio", "airplay_video"}
                        and not parameters
                    ):
                        result = await self.native.run(
                            "sound" if action == "airplay_audio" else "displays"
                        )
                        result["result_status"] = "unverified"
                    else:
                        name, args, text = self.operation(device, action, parameters)
                        result = await self.call(device, name, args, text)
            except (ValueError, ToolError, OSError, TimeoutError, httpx.HTTPError) as exc:
                result = {
                    "result_status": "failed",
                    "error": str(exc)
                    if isinstance(exc, ToolError)
                    and device.kind == "led_strip"
                    and action == "led"
                    and "color" in parameters
                    else "Действие недоступно или не подтверждено. "
                    "Проверь возможности, место и подключение.",
                }
            elapsed = round((time.monotonic() - started) * 1000)
            self.store.log(identifier, action, parameters, result, elapsed)
            self.metrics = {"device_id": identifier, "action": action, "total_ms": elapsed}
            results.append({"id": identifier, "name": device.name, **result})
        statuses = [r.get("result_status", "unverified") for r in results]
        failed = statuses.count("failed")
        status = (
            "failed"
            if failed == len(results)
            else "partial_success"
            if failed
            else "success"
            if all(s == "success" for s in statuses)
            else "unverified"
        )
        message = "\n".join(
            f"{r['name']}: "
            f"{r.get('error') or r.get('message') or r.get('result_status', 'Не подтверждено')}"
            for r in results
        )
        self.publish()
        return {
            "results": results,
            "result_status": status,
            "device_state_confirmed": status == "success",
            "error": message if status == "failed" else None,
            "message": message,
            "delivery_state": "partial" if failed and failed != len(results) else status,
            "written": not failed and any(r.get("written") for r in results),
        }

    def accept_network_state(self, device, result):
        from backend.devices.service import stamp

        device.state = result.get("state", {})
        if device.state.get("color"):
            self.colors.set_preview(device.id, device.state["color"])
        device.state_at = stamp()
        if device.state or result.get("written"):
            device.last_contact = device.state_at
        device.result_status = result.get("result_status", "success")
        self.store.observe(device)
        self.connections[device.id] = "observed" if device.state else "unknown"
        self.update_catalogue()

    def voice_plan(self, text):
        """Exact names/aliases plus conservative light-group matching; never guesses a target."""
        from backend.intents import compile_plan
        from backend.security import command_text

        value = command_text(text).replace("ё", "е").rstrip(".!? ")
        if hasattr(self, "lighting"):
            from backend.devices.lighting_voice import voice_plan

            extended = voice_plan(self, value)
            if extended is not None:
                return extended
        save_name = None
        if match := re.fullmatch(r"сохрани этот цвет как (.{1,80})", value):
            try:
                raw_name = re.search(
                    r"сохрани этот цвет как (.{1,80})$", " ".join(text.split()).rstrip(".!? "), re.I
                )
                save_name = ColorInput(
                    name=raw_name[1] if raw_name else match[1], rgb=[0, 0, 0]
                ).name
            except ValueError:
                return {"error": "Укажи обычное название пользовательского цвета."}
            value = "сделай ленту rgb 0 0 0"
        elif match := re.fullmatch(r"(?:поставь|сделай|установи|включи) мой цвет (.{1,80})", value):
            try:
                color = self.colors.named(match[1])
            except ValueError as exc:
                return {"error": str(exc)}
            value = "сделай ленту rgb " + " ".join(map(str, color.rgb))
        if not re.search(
            r"\b(включи|выключи|сделай|поставь|установи|переключи|верни|подключи|проверь|пауза|продолжи)\b",
            value,
        ):
            return None
        devices = self.store.list()
        groups = self.store.collections("group")
        place = self.locations.current().get("place_id")
        explicit = []
        place_phrases = []
        for profile in self.locations.list():
            phrases = [re.escape(profile.name.casefold().replace("ё", "е"))]
            if profile.id == "home":
                phrases += [r"дома", r"в доме"]
            elif profile.id == "dorm":
                phrases += [r"(?:в )?общаге", r"в общагу"]
            for phrase in phrases:
                if re.search(rf"(?<!\w){phrase}(?!\w)", value):
                    explicit.append(profile.id)
                    place_phrases.append(phrase)

        def without_place(command):
            for phrase in place_phrases:
                command = re.sub(rf"(?<!\w){phrase}(?!\w)", " ", command)
            return " ".join(command.split())

        def light_commands(targets, command):
            plan = compile_plan(
                without_place(command), comfortable_brightness=self.s.led.comfortable_brightness
            )
            if plan and plan.actions and all(a.device == "led_strip" for a in plan.actions):
                if save_name:
                    return {
                        "commands": [
                            {"target": d.id, "action": "save_color", "name": save_name}
                            for d in targets
                        ]
                    }
                return {
                    "commands": [
                        {
                            "target": d.id,
                            "action": "led",
                            **a.parameters.model_dump(exclude_none=True),
                        }
                        for d in targets
                        for a in plan.actions
                    ]
                }
            return None

        local_places = set(explicit) if explicit else {place, "general"}
        if re.search(r"(?:верни|переключи|выведи).*звук.*(?:\bmac\b|\bмак\b)", value):
            macs = [d for d in devices if d.kind == "mac"]
            if len(macs) == 1:
                return {"commands": [{"target": macs[0].id, "action": "audio_output"}]}
        matched = []
        for target in [*groups, *devices]:
            names = [n.casefold().replace("ё", "е") for n in self.device_names(target)]
            # Bounded inflection for names such as «Новая лента» → «новую ленту».
            for name in names[:]:
                if name.endswith("лента"):
                    words = name[:-5].split()
                    names.append(
                        " ".join([re.sub(r"ая$", "ую", word) for word in words] + ["ленту"])
                    )
            for name in sorted(names, key=len, reverse=True):
                normalized = name.casefold().replace("ё", "е")
                if re.search(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", value):
                    matched.append((target, normalized))
                    break
        all_lights = r"\b(?:весь свет|все лампы|всю подсветку|все ленты|обе ленты)\b"
        if not matched and re.search(all_lights, value):
            matches = [
                d for d in devices if d.kind in {"led_strip", "wled"} and d.place_id in local_places
            ]
            rooms = {d.room for d in matches if d.room}
            named_rooms = [room for room in rooms if room.casefold().rstrip("аяеуыи") in value]
            if named_rooms:
                matches = [d for d in matches if d.room in named_rooms]
            normalized = re.sub(all_lights, "ленту", value)
            plan = light_commands(matches, normalized)
            if matches and len(set(explicit)) <= 1 and plan:
                return plan
            return {"error": "Уточни комнату, профиль и поддерживаемое действие для света."}
        if not matched:
            normalized = value
            if re.fullmatch(r"установи яркость (?:\d{1,3}|[а-я ]+) процент(?:ов|а)?", value):
                normalized = value.replace("яркость", "яркость ленты", 1)
            matches = [d for d in devices if d.kind == "led_strip" and d.place_id in local_places]
            plan = light_commands(matches, normalized)
            if matches and plan:
                if not explicit and len(matches) > 1:
                    # Preserve the existing authenticated, expiring selection for follow-ups.
                    try:
                        selected = self.resolve("led_control")
                    except ToolError:
                        selected = None
                    if selected and any(d.id == selected.id for d in matches):
                        return light_commands([selected], normalized)
                if len(matches) == 1 and len(set(explicit)) <= 1:
                    return plan
                return {"error": "Уточни, какую ленту: " + ", ".join(d.name for d in matches) + "."}
            if re.search(r"\bновую ленту\b", value):
                return {
                    "error": "Назови ленту «Новая лента» или добавь голосовой синоним "
                    "в её настройках."
                }
            return None  # Existing bounded context parser handles other follow-ups.
        if explicit:
            matched = [(d, n) for d, n in matched if d.place_id in {*explicit, "general"}]
        else:
            matched = [
                (d, n)
                for d, n in matched
                if d.place_id in {place, "general"} or getattr(d, "protocol", "") == "local"
            ]
        if len(matched) != 1:
            return {
                "error": "Уточни название устройства, группу и текущее место "
                "в разделе «Устройства»."
            }
        target, name = matched[0]
        if getattr(target, "kind", "led_strip") in {"led_strip", "wled"} or hasattr(
            target, "device_ids"
        ):
            normalized = value.replace(name, "ленту")
            plan = light_commands([target], normalized)
            if plan:
                return plan
        if getattr(target, "kind", "") == "audio_output" and re.search(r"звук|вывод", value):
            return {"commands": [{"target": target.id, "action": "audio_output"}]}
        if getattr(target, "kind", "") == "mac":
            return None  # Retain existing display, volume and music command parsing.
        if getattr(target, "kind", "") == "airplay" and re.search(
            r"airplay|эйрплей|звук|вывод|экран|видео|зеркал|подключи", value
        ):
            return {
                "commands": [
                    {
                        "target": target.id,
                        "action": "airplay_video"
                        if re.search(r"экран|видео|зеркал", value)
                        else "airplay_audio",
                    }
                ]
            }
        return {"error": "Назови поддерживаемое действие. Возможности видны в карточке устройства."}

    async def voice_execute(self, plan):
        if plan.get("error"):
            return plan["error"]
        if "lighting_schedule" in plan:
            context = request_context.get()
            if (
                not context
                or not context.device.authenticated
                or context.device.target_agent_id != "local-mac"
                or "mac.system" not in context.device.capabilities
            ):
                return "Расписание света доступно авторизованному приложению на Mac."
            try:
                job = self.lighting.save("job", plan["lighting_schedule"])
                self.publish()
                return f"Расписание «{job.name}» сохранено. Выполняется работающим Mac."
            except (ValueError, ToolError) as exc:
                return str(exc)
        token = voice_addressed.set(True)
        try:
            messages = []
            failed_targets = set()
            for command in plan["commands"]:
                if command["target"] in failed_targets:
                    continue
                result = json.loads(await self.tools.call("device_command", command))
                messages.append(
                    result.get("error")
                    or result.get("message")
                    or "Фактический результат не подтверждён."
                )
                if result.get("result_status") == "failed":
                    failed_targets.add(command["target"])
            return "\n".join(messages)
        finally:
            voice_addressed.reset(token)
