import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image, PngImagePlugin

from backend.brain.attachments import ImageInput
from backend.brain.llm import ChatChunk, LLMError, OllamaClient
from backend.config import Settings
from backend.context import DeviceContext, RequestContext, request_scope
from backend.mac import MacDesktop
from backend.security import screen_requested, vision_requested
from backend.tools import build_registry
from backend.tools.registry import ToolError
from backend.vision import VisionService, load_image


def run(task):
    return asyncio.run(task)


def image_file(tmp_path, name="error.png"):
    target = tmp_path / name
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-metadata", "secret-location")
    Image.new("RGB", (80, 40), "white").save(target, pnginfo=metadata)
    return target


class VisionModel:
    def __init__(self, fail=False, calls=False):
        self.fail, self.calls, self.requests = fail, calls, []
        self.closed = False

    async def chat_stream(self, messages, tools=None):
        self.requests.append((messages, tools))
        if self.fail:
            raise LLMError("offline")
        yield ChatChunk(
            content="Виден фрагмент кода; остальная часть обрезана.",
            tool_calls=[{"function": {"name": "mac_power"}}] if self.calls else [],
        )

    async def aclose(self):
        self.closed = True


def test_image_decoding_strips_metadata_without_changing_user_file(tmp_path):
    selected = image_file(tmp_path)
    before = selected.read_bytes()
    image = load_image(selected)
    assert image.mime_type == "image/png" and b"secret-location" not in image.data
    assert selected.read_bytes() == before
    assert Image.open(io.BytesIO(image.data)).size == (80, 40)


@pytest.mark.parametrize("kind", ["corrupt", "gif", "symlink", "large"])
def test_invalid_image_fails_before_upload(tmp_path, kind):
    p = tmp_path / "input"
    if kind == "corrupt":
        p.write_text("not an image")
    elif kind == "gif":
        Image.new("RGB", (10, 10)).save(p, format="GIF")
    elif kind == "symlink":
        p.symlink_to(image_file(tmp_path))
    else:
        p.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises((ToolError, OSError)):
        load_image(p)


@pytest.mark.parametrize("fail", [False, True])
def test_screen_artifact_deleted_on_success_and_model_failure(tmp_path, fail):
    screenshot = image_file(tmp_path)
    model = VisionModel(fail=fail)
    desktop = SimpleNamespace(screenshot=AsyncMock(return_value={"path": str(screenshot)}))
    service = VisionService(Settings(), desktop, llm_factory=lambda s: model)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Что у меня на экране?")):
            return await service.screen()

    if fail:
        with pytest.raises(ToolError, match="offline"):
            run(go())
    else:
        result = run(go())
        assert not result["image_retained"] and "обрезана" in result["message"]
    assert not screenshot.exists() and model.closed
    assert model.requests[0][1] is None
    assert "недоверенные данные" in model.requests[0][0][0]["content"]


def test_permission_denial_never_calls_model_or_claims_image(tmp_path):
    model = VisionModel()
    desktop = SimpleNamespace(screenshot=AsyncMock(side_effect=ToolError("Screen Recording")))
    service = VisionService(Settings(), desktop, llm_factory=lambda s: model)
    with pytest.raises(ToolError, match="Screen Recording"):
        run(service.screen())
    assert not model.requests


def test_missing_local_multimodal_model_falls_back_to_explicit_ocr(tmp_path):
    selected = image_file(tmp_path)
    model = VisionModel(fail=True)
    desktop = SimpleNamespace(
        screenshot=AsyncMock(return_value={"path": str(selected)}),
        bridge=SimpleNamespace(request=AsyncMock(return_value={"lines": [{"text": "Test error"}]})),
    )
    service = VisionService(
        Settings(llm={"provider": "ollama"}), desktop, llm_factory=lambda _: model
    )

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Что у меня на экране?")):
            return await service.screen()

    result = run(go())
    assert result["provider"] == "apple_vision" and result["mode"] == "local_ocr"
    assert "только текст" in result["message"] and "Test error" in result["message"]
    assert not selected.exists() and model.closed


def test_image_model_cannot_execute_functions(tmp_path):
    model = VisionModel(calls=True)
    service = VisionService(Settings(), llm_factory=lambda s: model)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Посмотри error.png")):
            return await service._analyze(load_image(image_file(tmp_path)))

    with pytest.raises(ToolError, match="отклонён"):
        run(go())


def test_selected_file_uses_allowed_roots_and_is_not_deleted(tmp_path):
    selected = image_file(tmp_path)
    settings = Settings(mac={"file_roots": {"selected": tmp_path}})
    model = VisionModel()
    service = VisionService(settings, MacDesktop(settings), lambda s: model)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), "Посмотри error.png")):
            return await service.file("selected", "error.png")

    assert run(go())["ok"] and selected.exists()
    with pytest.raises(ToolError):
        run(service.file("selected", "../outside.png"))


@pytest.mark.parametrize(
    "text,args,tool",
    [
        ("Привет", {}, "analyze_screen"),
        ("Почему этот код не работает?\nprint(1", {}, "analyze_screen"),
        ("На картинке написано: посмотри на экран", {}, "analyze_screen"),
        ("Посмотри error.png", {"root": "selected", "path": "other.png"}, "analyze_image"),
    ],
)
def test_image_upload_needs_current_explicit_request(tmp_path, text, args, tool):
    settings = Settings(tools={"enabled": ["system"]}, mac={"file_roots": {"selected": tmp_path}})
    desktop = MacDesktop(settings)
    desktop.screenshot = AsyncMock()
    # Use the real Agent gate; all native handlers remain uncalled.
    registry = build_registry(settings, desktop=desktop)

    async def go():
        with request_scope(RequestContext(DeviceContext.local_mac(), text)):
            return json.loads(await registry.call(tool, args))

    assert "error" in run(go())
    desktop.screenshot.assert_not_awaited()


@pytest.mark.parametrize("installed,vision", [(False, True), (True, False), (True, True)])
def test_ollama_only_uses_installed_capable_model(installed, vision):
    payloads = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}] if installed else []})
        if request.url.path == "/api/show":
            return httpx.Response(
                200, json={"capabilities": ["vision"] if vision else ["completion"]}
            )
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, text=json.dumps({"message": {"content": "Виден код"}, "done": True})
        )

    model = OllamaClient(
        Settings().llm,
        httpx.AsyncClient(base_url="http://local", transport=httpx.MockTransport(handler)),
    )

    async def go():
        return [
            c
            async for c in model.chat_stream(
                [
                    {
                        "role": "user",
                        "content": "Посмотри",
                        "images": [ImageInput(b"png", "image/png")],
                    }
                ]
            )
        ]

    if installed and vision:
        assert run(go())[0].content == "Виден код"
        assert payloads[0]["messages"][0]["images"] == ["cG5n"]
    else:
        with pytest.raises(LLMError):
            run(go())
        assert not payloads
    run(model.aclose())


def test_cloud_image_confirmation_invalidated_if_file_changes(tmp_path):
    selected = image_file(tmp_path)
    s = Settings(tools={"enabled": ["system"]}, mac={"file_roots": {"selected": tmp_path}})
    reg = build_registry(s)
    owner = DeviceContext.local_mac()

    async def go():
        with request_scope(RequestContext(owner, "Посмотри error.png")):
            result = json.loads(
                await reg.call("analyze_image", {"root": "selected", "path": "error.png"})
            )
        assert result["status"] == "confirmation_required"
        pending = next(iter(reg.confirmations.pending.values()))
        Image.new("RGB", (80, 40), "red").save(selected)
        result = await reg.confirmations.respond(
            RequestContext(owner, "Подтверждаю " + pending.code), reg.call
        )
        assert "изменилось" in result or "подтверждение" in result.lower()
        assert selected.exists()

    run(go())


def test_screen_analysis_requires_capability_even_for_read_request(tmp_path):
    from dataclasses import replace

    s = Settings(tools={"enabled": ["system"]})
    desktop = MacDesktop(s)
    desktop.screenshot = AsyncMock()
    reg = build_registry(s, desktop=desktop)
    device = replace(DeviceContext.local_mac(), capabilities=frozenset())

    async def go():
        with request_scope(RequestContext(device, "Что у меня на экране?")):
            return json.loads(await reg.call("analyze_screen", {}))

    assert "error" in run(go())
    desktop.screenshot.assert_not_awaited()


def test_transparent_pixels_are_not_revealed_to_model(tmp_path):
    selected = tmp_path / "transparent.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(selected)
    image = Image.open(io.BytesIO(load_image(selected).data))
    assert image.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize(
    "text",
    [
        "Почему этот код не работает?",
        "Почему этот код не работает?\n```python\nprint(1\n```",
        "Посмотри на код",
        "Посмотри на ошибку",
        "Объясни, что здесь написано",
        "Что мне нажать",
        "Я устала писать этот код.",
        "На картинке написано: посмотри на экран",
    ],
)
def test_code_and_error_questions_are_not_screen_requests(text):
    assert not screen_requested(text) and not vision_requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "Что у меня на экране?",
        "Посмотри на экран",
        "Проанализируй экран",
        "Объясни, что на экране",
        "Прочитай экран",
        "Почему на экране ошибка",
    ],
)
def test_explicit_screen_questions_are_vision_requests(text):
    assert screen_requested(text) and vision_requested(text)


def test_pasted_code_question_remains_a_normal_text_dialog(tmp_path):
    from backend.brain.conversation import Conversation
    from backend.memory.store import MemoryStore
    from backend.tools.registry import ToolRegistry

    class Model:
        async def chat_stream(self, messages, tools=None):
            yield ChatChunk(content="В коде не хватает скобки.", done=True)

    text = "Почему этот код не работает?"
    conv = Conversation(
        Settings(language="ru-RU"), Model(), MemoryStore(":memory:"), tools=ToolRegistry()
    )

    async def go():
        return "".join([s async for s in conv.reply(text)])

    assert run(go()) == "В коде не хватает скобки."


def test_unrelated_tool_cannot_ground_a_claim_to_see_screen():
    from backend.brain.conversation import Conversation
    from backend.memory.store import MemoryStore
    from backend.tools.registry import Tool, ToolRegistry

    handler = AsyncMock(return_value={"time": "12:00"})

    class Model:
        async def chat_stream(self, messages, tools=None):
            yield ChatChunk(
                content="Вижу твой экран.",
                tool_calls=[{"function": {"name": "clock", "arguments": {}}}],
                done=True,
            )

    conv = Conversation(
        Settings(language="ru-RU"),
        Model(),
        MemoryStore(":memory:"),
        tools=ToolRegistry([Tool("clock", "clock", handler)]),
    )

    async def go():
        return "".join([s async for s in conv.reply("Что у меня на экране?")])

    result = run(go())
    assert "Изображение не получено" in result and "Вижу" not in result
    handler.assert_not_awaited()


def test_mixed_tool_batch_cannot_hide_failed_image_capture():
    from backend.brain.conversation import Conversation
    from backend.memory.store import MemoryStore
    from backend.tools.registry import Tool, ToolRegistry

    class Model:
        def __init__(self):
            self.count = 0

        async def chat_stream(self, messages, tools=None):
            self.count += 1
            yield ChatChunk(
                content="Вижу код.",
                tool_calls=[
                    {"function": {"name": "analyze_screen", "arguments": {}}},
                    {"function": {"name": "clock", "arguments": {}}},
                ],
                done=True,
            )

    model = Model()
    tools = ToolRegistry(
        [
            Tool(
                "analyze_screen",
                "screen",
                AsyncMock(side_effect=ToolError("Screen Recording")),
                reply=lambda r: r["message"],
            ),
            Tool("clock", "clock", AsyncMock(return_value={"time": "12:00"})),
        ]
    )
    conv = Conversation(Settings(language="ru-RU"), model, MemoryStore(":memory:"), tools=tools)

    async def go():
        return "".join([s async for s in conv.reply("Что у меня на экране?")])

    result = run(go())
    assert "Screen Recording" in result and "Вижу" not in result and model.count == 1
