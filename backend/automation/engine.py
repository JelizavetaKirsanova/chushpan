"""Async block execution, scoped authority, per-device exclusion and durable step receipts."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.automation.models import Variable
from backend.context import request_context, scenario_operation
from backend.latency import current, measure
from backend.security import ActionDenied, ConfirmationRequired, Risk, approved_action, fingerprint


class StepFailed(RuntimeError):
    pass


class StepUnverified(StepFailed):
    pass


class Engine:
    def __init__(self, store, capabilities):
        self.store, self.capabilities = store, capabilities
        self.locks = capabilities.tools.device_locks
        self.device_state = {}
        self.location = lambda: {"place_id": None, "name": None}

    def select(self, scenario, variant=""):
        if not scenario.variants:
            if variant:
                raise ValueError("У команды нет такой версии.")
            return scenario, None
        place = self.location()
        if variant:
            selected = next((v for v in scenario.variants if v.id == variant), None)
        else:
            if not place.get("place_id"):
                raise ValueError("Место не определено. Выбери место в разделе «Мои команды».")
            selected = next((v for v in scenario.variants if v.place_id == place["place_id"]), None)
        if selected is None or not selected.enabled:
            raise ValueError(
                "Для выбранного места нет включённой версии команды. "
                "Добавь или включи её в разделе «Мои команды»."
            )
        effective = scenario.model_copy(
            update={
                "actions": [*scenario.actions, *selected.actions],
                "conditions": [*scenario.conditions, *selected.conditions],
                "variants": [],
            }
        )
        return effective, {
            "variant_id": selected.id,
            "variant_name": selected.name,
            "place_id": selected.place_id,
            "current_place_id": place.get("place_id"),
            "place_name": place.get("name"),
            "selection": "manual" if variant else "auto",
        }

    def unavailable(self, block):
        if block.capability not in self.capabilities.items:
            return True
        cap = self.capabilities.items[block.capability]
        return (
            cap.tool not in self.capabilities.tools
            or self.device_state.get(cap.device, {}).get("connected") is False
        )

    def resolve(self, scenario, values):
        unknown = set(values) - {v.name for v in scenario.variables}
        if unknown:
            raise ValueError("Переданы неизвестные переменные.")
        result = {}
        for variable in scenario.variables:
            value = values.get(variable.name, variable.default)
            Variable(name=variable.name, kind=variable.kind, default=value)
            result[variable.name] = value
        return result

    @staticmethod
    def parameters(block, values):
        return {
            key: values[value["variable"]] if isinstance(value, dict) else value
            for key, value in block.parameters.items()
        }

    def operations(self, blocks, values):
        for block in blocks:
            if not block.enabled:
                continue
            if block.kind == "action":
                if not block.required and self.unavailable(block):
                    continue
                cap = self.capabilities.get(block.capability)
                yield cap, cap.arguments(self.parameters(block, values))
            yield from self.operations(block.children, values)
            yield from self.operations(block.otherwise, values)

    def identity(self, scenario, values, selection=None, variant=""):
        # Bind approval to the complete revision and current external Shortcut configuration.
        shortcuts = self.capabilities.s.shortcuts.model_dump(mode="json")
        summary = f"сценарий «{scenario.name}», версия {scenario.revision}; " + json.dumps(
            [scenario.model_dump(mode="json"), values, shortcuts, selection],
            ensure_ascii=False,
            sort_keys=True,
        )
        args = {
            "id": scenario.id,
            "revision": scenario.revision,
            "values": json.dumps(values, sort_keys=True),
        }
        if variant:
            args["variant"] = variant
        return args, summary, fingerprint("run_scenario", args, summary)

    async def condition(self, condition, values):
        if condition.source == "variable":
            actual = values[condition.key]
        elif condition.source == "time":
            actual = datetime.now(ZoneInfo(self.capabilities.s.location.tz_name)).hour
        else:
            # No guessed connection status. Only an explicitly observed probe/operation counts.
            cap = self.capabilities.get(condition.key)
            actual = self.device_state.get(cap.device, {}).get("connected")
            if actual is None:
                raise StepFailed("Доступность устройства не проверена. Обнови раздел «Устройства».")
        try:
            return {
                "eq": lambda: actual == condition.value,
                "ne": lambda: actual != condition.value,
                "gt": lambda: actual > condition.value,
                "lt": lambda: actual < condition.value,
            }[condition.operator]()
        except TypeError:
            raise StepFailed("Типы значений условия несовместимы.") from None

    async def run(self, scenario, *, values=None, event_id=None, variant=""):
        context = request_context.get()
        if (
            context is None
            or not context.device.authenticated
            or context.device.target_agent_id != "local-mac"
        ):
            raise ActionDenied("Сценарии выполняются только из авторизованной локальной сессии.")
        if not scenario.enabled:
            raise ActionDenied("Сценарий отключён.")
        with measure("action_planning"):
            effective, selection = self.select(scenario, variant)
            self.capabilities.validate(effective)
            values = self.resolve(effective, values or {})
            operations = list(self.operations(effective.actions, values))
            args, summary, approval = self.identity(scenario, values, selection, variant)
            dangerous = any(
                self.capabilities.risk(cap, a) == Risk.DANGEROUS for cap, a in operations
            )
            if (
                dangerous or scenario.confirmation == "always"
            ) and approved_action.get() != approval:
                raise ConfirmationRequired(
                    f"выполнить сценарий «{scenario.name}», версия {scenario.revision}. "
                    + "; ".join(
                        f"{cap.name}: {json.dumps(a, ensure_ascii=False)}" for cap, a in operations
                    ),
                    approval,
                    args,
                )
        event_id = (
            event_id
            or f"{context.device.device_id}:{context.device.session_id}:{context.request_id}"
        )
        run, fresh = self.store.begin_run(scenario, event_id)
        if not fresh:
            return {
                **run,
                "replayed": True,
                "message": "Событие уже зарегистрировано; повторного запуска нет.",
            }
        started = time.perf_counter()
        if selection:
            run.update(selection)
        acquired = []
        try:
            # Reserve all devices atomically before awaiting any operation. A conflicting
            # scenario is rejected, never queued for an unexpected delayed execution.
            for device in sorted({cap.device for cap, _ in operations}):
                lock = self.locks.setdefault(device, asyncio.Lock())
                if lock.locked():
                    raise StepFailed(f"Устройство {device} уже выполняет другой сценарий.")
                await lock.acquire()
                acquired.append(lock)
            if not all(
                [await self.condition(condition, values) for condition in effective.conditions]
            ):
                run["status"] = "skipped"
            else:
                with measure("scenario_execution"):
                    await self.blocks(effective.actions, values, run, approval=approval)
                statuses = {step["status"] for step in run["steps"]}
                run["status"] = (
                    "failed"
                    if statuses & {"failed", "blocked"} or run.get("errors")
                    else "unverified"
                    if "unverified" in statuses
                    else "sent"
                    if "sent" in statuses
                    else "skipped"
                    if not statuses or statuses == {"skipped"}
                    else "confirmed"
                )
        except asyncio.CancelledError:
            run["status"] = "interrupted"
            run["error"] = (
                "Ожидание прервано; фактическое состояние неизвестно. Автоповтор отключён."
            )
            raise
        except StepUnverified as exc:
            run.update(status="unverified", error=str(exc))
        except (StepFailed, ValueError) as exc:
            run.update(status="failed", error=str(exc))
        except Exception:
            run.update(
                status="failed",
                error="Ошибка интеграции. Часть действий могла выполниться; автоповтора нет.",
            )
        finally:
            for lock in acquired:
                lock.release()
            run["duration_s"] = time.perf_counter() - started
            self.store.update_run(run)
        profile = current.get()
        if profile and run["status"] in {"failed", "unverified", "interrupted"}:
            profile.result_status = run["status"]
        labels = {
            "confirmed": "выполнен и проверен",
            "sent": "команды отправлены; физический результат не подтверждён",
            "failed": "остановлен с ошибкой",
            "unverified": "результат не подтверждён",
            "skipped": "нет шагов для выполнения или условия не выполнены",
        }
        run["message"] = f"Сценарий «{scenario.name}»: {labels.get(run['status'], run['status'])}."
        if run.get("error"):
            run["message"] += " " + run["error"]
        return run

    async def blocks(self, blocks, values, run, prefix="", approval=None):
        for index, block in enumerate(blocks):
            path = prefix + str(index + 1)
            try:
                await self.block(block, values, run, path, approval)
            except StepFailed as exc:
                run.setdefault("errors", []).append(str(exc))
                if block.on_error == "stop":
                    raise

    async def block(self, block, values, run, path, approval=None):
        if (
            not block.enabled
            or block.kind == "action"
            and not block.required
            and self.unavailable(block)
        ):
            run["steps"].append(
                {
                    "path": path,
                    "kind": block.kind,
                    "capability": block.capability,
                    "status": "skipped",
                    "reason": "disabled" if not block.enabled else "unavailable",
                    "duration_s": 0.0,
                }
            )
            self.store.update_run(run)
            return
        if block.kind == "sequence":
            await self.blocks(block.children, values, run, path + ".", approval)
            return
        if block.kind == "parallel":
            resources = []
            for child in block.children:
                devices = {cap.device for cap, _ in self.operations([child], values)}
                if any(devices & previous for previous in resources):
                    raise StepFailed(
                        "Параллельные ветки управляют одним устройством. "
                        "Используй последовательность."
                    )
                resources.append(devices)
            # Wait for every started branch even after a sibling fails; never orphan a write.
            results = await asyncio.gather(
                *(
                    self.block(child, values, run, path + f".{i + 1}", approval)
                    for i, child in enumerate(block.children)
                ),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                raise StepFailed(
                    "Ошибка параллельной ветки; результаты всех начатых веток сохранены."
                )
            return
        if block.kind == "if":
            selected = (
                block.children if await self.condition(block.condition, values) else block.otherwise
            )
            await self.blocks(selected, values, run, path + ".", approval)
            return
        step = {
            "path": path,
            "kind": block.kind,
            "capability": block.capability,
            "status": "running",
        }
        run["steps"].append(step)
        self.store.update_run(run)  # Journal before the effect, not after it.
        start = time.perf_counter()
        try:
            if block.kind == "delay":
                await asyncio.sleep(block.seconds)
                step["status"] = "confirmed"
                return
            capability = self.capabilities.get(block.capability)
            args = capability.arguments(self.parameters(block, values))
            context = request_context.get()
            approved = None
            tool = self.capabilities.tools._tools[capability.tool]
            # Only the outer, complete scenario approval may authorize a dangerous child.
            if self.capabilities.risk(capability, args) == Risk.DANGEROUS:
                if approval is None or approved_action.get() != approval:
                    raise StepFailed("Риск действия изменился. Нужно новое подтверждение сценария.")
                prepared = tool.prepare(args) if tool.prepare else args
                summary = (
                    tool.summary(prepared)
                    if tool.summary
                    else f"{tool.name}: {json.dumps(prepared, ensure_ascii=False)}"
                )
                identity = summary + ("\n" + tool.version(prepared) if tool.version else "")
                approved = approved_action.set(fingerprint(tool.name, prepared, identity))
            grant = scenario_operation.set(
                (context.request_id, capability.tool, json.dumps(args, sort_keys=True))
            )
            try:
                with measure("scenario_action", tool=capability.tool):
                    result = json.loads(await self.capabilities.tools.call(capability.tool, args))
            finally:
                scenario_operation.reset(grant)
                if approved is not None:
                    approved_action.reset(approved)
            with measure("result_verification", tool=capability.tool):
                step.update(
                    result=result, status=self.capabilities.verify(capability.tool, args, result)
                )
            if (
                not block.required
                and result.get("error_code") == "device_unavailable"
                and result.get("effect_started") is False
            ):
                step.update(status="skipped", reason="unavailable")
                self.device_state.setdefault(capability.device, {}).update(
                    connected=False, status="unavailable", error=result["error"]
                )
                return
            state = self.device_state.setdefault(capability.device, {})
            state.update(last_operation=capability.name, status=step["status"])
            if step["status"] in {"confirmed", "sent"}:
                state.update(connected=True, last_success=datetime.now().isoformat(), error=None)
            else:
                state.update(connected=None, error=result.get("error", "Результат не подтверждён."))
            if step["status"] in {"failed", "blocked", "unverified"}:
                failure = StepUnverified if step["status"] == "unverified" else StepFailed
                raise failure(f"Шаг {path} ({capability.name}): " + str(state["error"]))
        except asyncio.CancelledError:
            step["status"] = "interrupted"
            raise
        except StepFailed:
            raise
        except Exception as exc:
            if step["status"] == "running":
                step.update(status="failed", error=str(exc))
            raise StepFailed(f"Шаг {path}: {exc}") from exc
        finally:
            step["duration_s"] = time.perf_counter() - start
            self.store.update_run(run)
