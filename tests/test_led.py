import argparse
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from bleak.exc import BleakBluetoothNotAvailableError, BleakBluetoothNotAvailableReason, BleakError

from backend import led, led_cli
from backend.brain.conversation import Conversation
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.led import LEDStrip, packet, parse
from backend.local_commands import route
from backend.mac_agent import LocalMacAgent
from backend.memory.store import MemoryStore
from backend.tools import build_registry
from backend.tools.registry import ToolError, ToolRegistry


@pytest.fixture
def rig(monkeypatch):
    settings = Settings(language="ru-RU", led={"enabled": True})
    strip = LEDStrip(settings)
    strip.cfg = strip.cfg.model_copy(update={"settle_s": 0})
    char = SimpleNamespace(
        uuid=str(settings.led.write_characteristic), properties=["write-without-response"]
    )
    service = SimpleNamespace(
        uuid=str(settings.led.service),
        get_characteristic=Mock(return_value=char),
        characteristics=[char],
    )
    clients = []

    def create(*args, **kwargs):
        services = MagicMock()
        services.get_service = Mock(return_value=service)
        services.__iter__.side_effect = lambda: iter(
            [services.get_service.return_value] if services.get_service.return_value else []
        )
        client = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            write_gatt_char=AsyncMock(),
            services=services,
            read_gatt_char=AsyncMock(return_value=b"mock-private-value"),
            stop_notify=AsyncMock(),
        )
        clients.append(client)
        return client

    factory = Mock(side_effect=create)
    monkeypatch.setattr(led, "BleakClient", factory)
    return SimpleNamespace(
        strip=strip, settings=settings, factory=factory, clients=clients, char=char
    )


@pytest.mark.parametrize(
    "text,args",
    [
        ("Чушпан, сделай подсветку красной!", {"action": "color", "rgb": [255, 0, 0]}),
        ("сделай ленту синей", {"action": "color", "rgb": [0, 0, 255]}),
        ("включи фиолетовую подсветку", {"action": "color", "rgb": [255, 0, 200]}),
        ("поставь подсветку розовый", {"action": "color", "rgb": [255, 0, 32]}),
        ("сделай цвет подсветки тёплый белый", {"action": "color", "rgb": [255, 48, 32]}),
        ("сделай подсветку светло-розовой", {"action": "color", "rgb": [255, 16, 32]}),
        ("сделай подсветку светло розовой", {"action": "color", "rgb": [255, 16, 32]}),
        ("сделай подсветку RGB 12 34 56", {"action": "color", "rgb": [12, 34, 56]}),
        ("выключи ленту", {"action": "off"}),
        ("включи LED-ленту", {"action": "on"}),
        ("погаси подсветку", {"action": "off"}),
        ("установи яркость подсветки на 50%", {"action": "brightness", "level": 50}),
        ("сделай яркость ленты двадцать пять процентов", {"action": "brightness", "level": 25}),
        ("поставь яркость подсветки сто", {"action": "brightness", "level": 100}),
        ("сделай подсветку тусклее", {"action": "тусклее"}),
        ("сделай подсветку ярче", {"action": "ярче"}),
        ("проверь состояние ленты", {"action": "status"}),
    ],
)
def test_russian_routes(text, args):
    assert parse(text) == args
    assert route(text) == ("led_control", {"command": text})


@pytest.mark.parametrize(
    "text",
    [
        "не выключай подсветку",
        "он сказал: выключи ленту",
        "«включи подсветку»",
        "включи подсветку и выключи Mac",
        "сделай яркость подсветки 101%",
        "сделай яркость подсветки -1",
        "сделай подсветку RGB 256 0 0",
        "выключи Bluetooth",
        "установи яркость 25%",
        "сделай экран синим",
    ],
)
def test_no_ambiguous_or_unrelated_actions(text):
    assert parse(text) is None


def test_red_regression_frame_and_strict_validation():
    assert packet("color", rgb=[255, 0, 0]).hex(" ").upper() == "7E 07 05 03 FF 00 00 10 EF"
    for rgb in ([0, 0], [0, 0, 256], [True, 0, 0], [-1, 0, 0]):
        with pytest.raises(ToolError):
            packet("color", rgb=rgb)
    for value in (-1, 101, True, 50.5):
        with pytest.raises(ToolError):
            packet("brightness", level=value)


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"action": "on"}, "7e 04 04 01 00 01 ff 00 ef"),
        ({"action": "off"}, "7e 04 04 00 00 00 ff 00 ef"),
        ({"action": "brightness", "level": 0}, "7e 04 01 00 ff ff ff 00 ef"),
        ({"action": "brightness", "level": 25}, "7e 04 01 19 ff ff ff 00 ef"),
        ({"action": "brightness", "level": 100}, "7e 04 01 64 ff ff ff 00 ef"),
    ],
)
def test_power_and_percent_brightness_protocol_regressions(args, expected):
    assert packet(**args).hex(" ") == expected


@pytest.mark.parametrize(
    "led_request",
    [
        {"action": "on"},
        {"action": "off"},
        {"action": "brightness", "level": 50},
        {"action": "color", "rgb": [0, 0, 255]},
    ],
)
def test_unconfirmed_modes_never_connect(rig, led_request):
    with pytest.raises(ToolError, match="не проверен"):
        asyncio.run(rig.strip.execute(**led_request))
    rig.factory.assert_not_called()


def test_persistence_human_verification_and_firmware_scope(rig):
    async def go():
        for index, rgb in enumerate(([0, 255, 0], [0, 0, 255])):
            result = await rig.strip.execute(action="color", rgb=rgb, experiment=True)
            assert ("rgb" in rig.strip._load().confirmed) is bool(index)
            await rig.strip.confirm_probe(result["probe_id"], True)
        restarted = LEDStrip(rig.settings)
        restarted.cfg = rig.strip.cfg
        assert "rgb" in restarted._load().confirmed
        await restarted.command("сделай подсветку розовой")
        restarted.cfg = restarted.cfg.model_copy(update={"firmware": "other-firmware"})
        with pytest.raises(ToolError, match="не проверен"):
            await restarted.command("сделай подсветку красной")

    asyncio.run(go())
    assert rig.factory.call_count == 3
    assert all(c.disconnect.await_count == 1 for c in rig.clients)
    assert rig.clients[-1].write_gatt_char.await_args.kwargs == {"response": False}


def test_rejected_probe_remains_blocked_and_cannot_be_reused(rig):
    async def go():
        result = await rig.strip.execute(action="off", experiment=True)
        await rig.strip.confirm_probe(result["probe_id"], False)
        with pytest.raises(ToolError, match="устарела"):
            await rig.strip.confirm_probe(result["probe_id"], True)
        with pytest.raises(ToolError, match="не выполнила"):
            await rig.strip.execute(action="off")

    asyncio.run(go())
    assert rig.factory.call_count == 1


def test_write_failure_is_not_retried_and_next_request_reconnects(rig):
    create = rig.factory.side_effect

    def first_bad(*args, **kwargs):
        client = create(*args, **kwargs)
        if len(rig.clients) == 1:
            client.write_gatt_char.side_effect = BleakError("disconnected during write")
        return client

    rig.factory.side_effect = first_bad

    async def go():
        with pytest.raises(ToolError, match="автоповтора"):
            await rig.strip.command("сделай подсветку красной")
        assert rig.strip._load().last_write["status"] == "unknown"
        assert rig.factory.call_count == 1
        result = await rig.strip.command("сделай подсветку красной")
        assert result["written"] and not result["physical_confirmed"]

    asyncio.run(go())
    assert rig.factory.call_count == 2
    assert all(c.write_gatt_char.await_count == 1 for c in rig.clients)


def test_only_connection_is_retried(rig):
    create = rig.factory.side_effect

    def transient(*args, **kwargs):
        client = create(*args, **kwargs)
        if len(rig.clients) == 1:
            client.connect.side_effect = TimeoutError()
        return client

    rig.factory.side_effect = transient
    asyncio.run(rig.strip.command("сделай подсветку красной"))
    rig.clients[0].write_gatt_char.assert_not_awaited()
    assert rig.clients[1].write_gatt_char.await_count == 1


def test_bluetooth_disabled_reports_without_retry(rig):
    rig.factory.side_effect = None
    client = SimpleNamespace(
        connect=AsyncMock(
            side_effect=BleakBluetoothNotAvailableError(
                "off", BleakBluetoothNotAvailableReason.POWERED_OFF
            )
        ),
        disconnect=AsyncMock(),
    )
    rig.factory.return_value = client
    with pytest.raises(ToolError, match="Bluetooth недоступен"):
        asyncio.run(rig.strip.command("сделай подсветку красной"))
    assert rig.factory.call_count == 1
    assert client.disconnect.await_count == 1


def test_receipt_waits_for_write_and_cancellation_never_promotes(rig):
    create = rig.factory.side_effect

    async def go():
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(*args, **kwargs):
            entered.set()
            await release.wait()

        def blocked(*args, **kwargs):
            client = create(*args, **kwargs)
            client.write_gatt_char.side_effect = write
            return client

        rig.factory.side_effect = blocked
        task = asyncio.create_task(rig.strip.execute(action="off", experiment=True))
        await entered.wait()
        assert not task.done()
        assert rig.strip._load().last_write["status"] == "unknown"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "off" not in rig.strip._load().confirmed
        assert rig.strip._load().pending_probe is None
        rig.clients[0].disconnect.assert_awaited_once()
        release.set()
        result = await rig.strip.command("сделай подсветку красной")
        assert result["written"] is True

    asyncio.run(go())


def test_write_timeout_has_no_automatic_replay(rig):
    rig.strip.cfg = rig.strip.cfg.model_copy(update={"write_timeout_s": 0.01})
    create = rig.factory.side_effect

    def hanging(*args, **kwargs):
        client = create(*args, **kwargs)

        async def wait(*args, **kwargs):
            await asyncio.Event().wait()

        client.write_gatt_char.side_effect = wait
        return client

    rig.factory.side_effect = hanging
    with pytest.raises(ToolError, match="тайм-аут"):
        asyncio.run(rig.strip.command("сделай подсветку красной"))
    assert rig.factory.call_count == 1


def test_corrupt_settings_and_wrong_gatt_never_write(rig):
    rig.strip.path.parent.mkdir(parents=True)
    rig.strip.path.write_text('{"confirmed": ["off"]}')
    with pytest.raises(ToolError, match="повреждены"):
        asyncio.run(rig.strip.execute(action="off"))
    rig.factory.assert_not_called()
    rig.strip.path.unlink()
    rig.char.properties = ["read"]
    with pytest.raises(ToolError, match="GATT"):
        asyncio.run(rig.strip.command("сделай подсветку красной"))
    rig.clients[0].write_gatt_char.assert_not_awaited()


def test_relative_brightness_requires_baseline_and_rejection_clears_it(rig):
    async def go():
        with pytest.raises(ToolError, match="абсолютную"):
            await rig.strip.command("сделай подсветку ярче")
        result = await rig.strip.execute(action="brightness", level=50, experiment=True)
        await rig.strip.confirm_probe(result["probe_id"], True)
        await rig.strip.command("сделай подсветку тусклее")
        assert rig.strip._load().brightness == 40
        result = await rig.strip.execute(action="brightness", level=25, experiment=True)
        await rig.strip.confirm_probe(result["probe_id"], False)
        assert rig.strip._load().brightness is None
        assert "brightness" not in rig.strip._load().confirmed

    asyncio.run(go())


def registry(strip):
    tool = strip.tool()
    agent = LocalMacAgent()
    agent.register(tool.name, tool._handler)
    tool.agent = agent
    return ToolRegistry([tool])


def test_conversation_and_voice_bypass_llm_even_on_failure(rig):
    from backend.voice import reply_aloud

    async def go():
        model = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("LLM forbidden")))
        store = MemoryStore(":memory:")
        speaker = SimpleNamespace()
        said = []

        async def speak(stream, on_start=None):
            async for piece in stream:
                said.append(piece)

        speaker.speak = speak
        try:
            conv = Conversation(rig.settings, model, store, tools=registry(rig.strip))
            await reply_aloud(conv, speaker, "Чушпан, сделай подсветку красной", 20)
            assert "отправлена" in " ".join(said)
            assert "Физическое выполнение не подтверждено" in " ".join(said)
            said.clear()
            await reply_aloud(conv, speaker, "Чушпан, выключи подсветку", 20)
            assert "отправлена" in " ".join(said)
            assert "не подтверждено" in " ".join(said)
            said.clear()
            await reply_aloud(conv, speaker, "Чушпан, установи яркость подсветки 50%", 20)
            assert "не проверен" in " ".join(said)
            assert "отправлена" not in " ".join(said)
            model.chat_stream.assert_not_called()
        finally:
            store.close()

    asyncio.run(go())
    assert rig.factory.call_count == 2


def test_disabled_led_still_routes_locally(rig):
    async def go():
        model = SimpleNamespace(chat_stream=AsyncMock(side_effect=AssertionError("LLM forbidden")))
        store = MemoryStore(":memory:")
        try:
            conv = Conversation(rig.settings, model, store, tools=ToolRegistry())
            answer = "".join([s async for s in conv.reply("включи подсветку")])
            assert "отключено" in answer
            model.chat_stream.assert_not_called()
        finally:
            store.close()

    asyncio.run(go())
    rig.factory.assert_not_called()


def test_registry_preserves_authentication_intent_and_local_agent(rig):
    async def go():
        reg = registry(rig.strip)
        current = RequestContext(DeviceContext.local_mac(), "сделай подсветку красной")
        with request_scope(current):
            result = json.loads(await reg.call("led_control", {"command": "выключи подсветку"}))
            assert "явная просьба" in result["error"]
        remote = replace(current.device, capabilities=frozenset(), authenticated=False)
        with request_scope(RequestContext(remote, "сделай подсветку красной")):
            result = json.loads(
                await reg.call("led_control", {"command": "сделай подсветку красной"})
            )
            assert "Mac Agent" in result["error"]

    asyncio.run(go())
    rig.factory.assert_not_called()
    assert "led_control" in build_registry(rig.settings)
    rig.settings.led.enabled = False
    assert "led_control" not in build_registry(rig.settings)


@pytest.mark.parametrize("failure", [False, True])
def test_night_led_failure_preserves_displays_volume_and_single_shortcut(rig, tmp_path, failure):
    from tests.test_night import routine

    service = routine(tmp_path)
    service.led = SimpleNamespace(
        execute=AsyncMock(
            return_value={
                "written": True,
                "message": "Команда LED отправлена; физическое выполнение не подтверждено",
            }
        )
    )
    if failure:
        service.led.execute.side_effect = ToolError("Bluetooth недоступен")

    async def go():
        result = await service.run()
        assert result["shortcut_started"]
        assert result["ok"] is not failure
        assert result["steps"]["led"]["status"] == ("unknown" if failure else "done")
        assert result["steps"]["builtin"]["status"] == "done"
        assert result["steps"]["external"]["status"] == "done"
        await service.run()

    asyncio.run(go())
    assert service.display.set.await_count == 2
    service.volume.assert_awaited_once_with(level=30)
    service.led.execute.assert_awaited_once_with(action="off")
    service.shortcuts.start_sleep.assert_awaited_once()


def test_cli_probe_requires_send_and_visual_confirmation(rig, monkeypatch):
    monkeypatch.setattr(led_cli, "LEDStrip", lambda s: rig.strip)
    monkeypatch.setattr(led_cli.sys.stdin, "isatty", lambda: True)
    replies = iter(["нет"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    args = argparse.Namespace(led_action="probe", text="выключи подсветку")
    assert asyncio.run(led_cli.run(rig.settings, args)) == 0
    rig.factory.assert_not_called()
    replies = iter(["ОТПРАВИТЬ", "ДА"])
    assert asyncio.run(led_cli.run(rig.settings, args)) == 0
    assert "off" in rig.strip._load().confirmed
    assert rig.factory.call_count == 1


def test_status_and_notification_diagnostics_never_write_or_claim_ack(rig):
    async def go():
        status = await rig.strip.execute(action="status")
        assert status["last_write"] is None
        assert "не считывал" in status["message"]
        rig.factory.assert_not_called()
        create = rig.factory.side_effect
        notify = SimpleNamespace(
            uuid=str(rig.strip.cfg.notify_characteristic), properties=["notify"]
        )

        def diagnostic(*args, **kwargs):
            client = create(*args, **kwargs)
            service = client.services.get_service.return_value
            service.characteristics = [rig.char, notify]
            service.get_characteristic.side_effect = lambda uuid: (
                rig.char if uuid == rig.char.uuid else notify
            )

            async def start(char, callback):
                callback(char, bytearray.fromhex("7e 01 ef"))

            client.start_notify = AsyncMock(side_effect=start)
            return client

        rig.factory.side_effect = diagnostic
        result = await rig.strip.inspect(0.001)
        assert result["notifications"] == [
            {"uuid": notify.uuid, "byte_count": 3, "value": "redacted"}
        ]
        assert "не являются ACK" in result["message"]
        rig.clients[0].write_gatt_char.assert_not_awaited()

    asyncio.run(go())


def test_another_process_or_instance_cannot_overlap_write(rig):
    async def go():
        other = LEDStrip(rig.settings)
        async with rig.strip._exclusive():
            with pytest.raises(ToolError, match="занята"):
                await other.command("сделай подсветку красной")
        assert (await other.execute(action="status"))["last_write"] is None

    asyncio.run(go())
    rig.factory.assert_not_called()


def test_custom_device_requires_new_evidence_and_keeps_it(rig):
    async def go():
        rig.strip.cfg = rig.strip.cfg.model_copy(update={"firmware": "new-firmware"})
        with pytest.raises(ToolError, match="не проверен"):
            await rig.strip.command("сделай подсветку красной")
        result = await rig.strip.execute(action="color", rgb=[255, 0, 0], experiment=True)
        await rig.strip.confirm_probe(result["probe_id"], True)
        assert (await rig.strip.command("сделай подсветку красной"))["written"]

    asyncio.run(go())
    assert rig.factory.call_count == 2


def test_unknown_native_bluetooth_error_is_a_tool_error_without_retry(rig):
    create = rig.factory.side_effect

    def native_error(*args, **kwargs):
        client = create(*args, **kwargs)
        client.write_gatt_char.side_effect = RuntimeError("native backend error")
        return client

    rig.factory.side_effect = native_error
    with pytest.raises(ToolError, match="выполнение неизвестно"):
        asyncio.run(rig.strip.command("сделай подсветку красной"))
    assert rig.factory.call_count == 1


def test_unconfirmed_night_off_never_connects_but_preserves_shortcut(rig, tmp_path):
    from tests.test_night import routine

    service = routine(tmp_path)
    service.led = rig.strip
    result = asyncio.run(service.run())
    assert not result["ok"] and result["shortcut_started"]
    assert "не проверен" in result["message"]
    rig.factory.assert_not_called()
    service.shortcuts.start_sleep.assert_awaited_once()


def test_existing_sleep_watcher_never_gets_late_led_change(rig, tmp_path):
    from tests.test_night import routine

    service = routine(tmp_path)
    assert asyncio.run(service.run())["shortcut_started"]
    service.led = SimpleNamespace(execute=AsyncMock())
    asyncio.run(service.run())
    service.led.execute.assert_not_awaited()
    service.shortcuts.start_sleep.assert_awaited_once()


def test_bundle_declares_bluetooth_permission(rig):
    from backend.app_bundle import info_plist

    assert "Bluetooth" in info_plist(rig.settings)["NSBluetoothAlwaysUsageDescription"]
