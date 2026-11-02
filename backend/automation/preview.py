"""Non-executing preview over the exact selection, schemas and conditions used by Engine."""

from backend.automation.engine import StepFailed


async def preview(engine, scenario, *, variant="", values=None):
    effective, selection = engine.select(scenario, variant)
    resolved = engine.resolve(effective, values or {})
    errors = []
    try:
        engine.capabilities.validate(effective)
    except ValueError as exc:
        errors.append(str(exc))
    conditions = []
    for condition in effective.conditions:
        try:
            conditions.append(await engine.condition(condition, resolved))
        except (StepFailed, KeyError, ValueError):
            conditions.append(None)
    steps = []

    async def visit(blocks, prefix=""):
        for i, block in enumerate(blocks):
            path = prefix + str(i + 1)
            step = {
                "path": path,
                "kind": block.kind,
                "capability": block.capability,
                "status": "planned",
                "required": block.required,
            }
            if i and effective.inter_block_delay_s:
                step["delay_before_s"] = effective.inter_block_delay_s
            steps.append(step)
            if not block.enabled:
                step.update(status="skipped", reason="Шаг выключен")
                continue
            if block.kind == "action":
                if not block.required and engine.unavailable(block):
                    step.update(
                        status="skipped", reason="Устройство недоступно; шаг необязательный"
                    )
                else:
                    try:
                        cap = engine.capabilities.get(block.capability)
                        step.update(
                            name=cap.name,
                            device=cap.device,
                            parameters=engine.parameters(block, resolved),
                            risk=engine.capabilities.risk(
                                cap, cap.arguments(engine.parameters(block, resolved))
                            ).value,
                            availability="unavailable"
                            if engine.unavailable(block)
                            else "not_checked",
                        )
                    except (ValueError, KeyError) as exc:
                        step.update(status="invalid", reason=str(exc))
            elif block.kind == "delay":
                step["seconds"] = block.seconds
            elif block.kind == "if":
                try:
                    result = await engine.condition(block.condition, resolved)
                except (StepFailed, KeyError, ValueError):
                    result = None
                step["condition_result"] = result
                if result is not False:
                    await visit(block.children, path + ".then.")
                if result is not True:
                    await visit(block.otherwise, path + ".else.")
            else:
                if block.kind == "parallel":
                    resources = [
                        set(cap.device for cap, _ in engine.operations([child], resolved))
                        for child in block.children
                    ]
                    if any(
                        left & right
                        for i, left in enumerate(resources)
                        for right in resources[i + 1 :]
                    ):
                        raise ValueError("Параллельные ветки управляют одним устройством.")
                await visit(block.children, path + ".")

    await visit(effective.actions)
    return {
        "status": "dry_run",
        "name": scenario.name,
        "selection": selection,
        "steps": steps,
        "conditions": conditions,
        "errors": errors,
        "timeout_s": effective.timeout_s,
        "message": "Безопасная проверка: инструменты, устройства и задержки не запускались.",
    }
