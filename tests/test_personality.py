"""The same editable personality must reach chat, voice, tools and briefing."""

import asyncio
import copy
from pathlib import Path

import pytest

from backend.brain.briefing import BriefingData, build_messages
from backend.brain.conversation import Conversation
from backend.brain.llm import ChatChunk
from backend.brain.prompts import build_greeting, build_system_prompt
from backend.config import CONFIG_FILE, ROOT_DIR, Settings
from backend.memory.store import MemoryStore
from backend.tools.registry import Tool, ToolError, ToolRegistry
from backend.voice import reply_aloud

PROFILE = ROOT_DIR / "personality.md"


def settings(**kwargs):
    return Settings(
        language="ru-RU",
        assistant_name="Чушпан",
        persona={"file": PROFILE, "max_sentences": 3},
        **kwargs,
    )


class RecordingLLM:
    def __init__(self, replies=None):
        self.requests = []
        self.primed = []
        self.replies = list(replies or [ChatChunk(content="Я здесь.", done=True)])

    async def chat_stream(self, messages, tools=None):
        self.requests.append(copy.deepcopy(messages))
        yield self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]

    async def prime(self, prompt, tools=None, history=None):
        self.primed.append(prompt)


class RecordingSpeaker:
    def __init__(self):
        self.spoken = []

    async def speak(self, sentences, on_start=None):
        async for sentence in sentences:
            self.spoken.append(sentence)
        return True


def test_config_enables_one_profile_and_the_selected_local_voice(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "yaml_file", CONFIG_FILE)
    s = Settings(_env_file=None)
    assert s.persona.file == Path("personality.md")
    assert s.persona.max_sentences == 3
    assert s.persona_style == PROFILE.read_text(encoding="utf-8").strip()
    assert s.llm.model == "qwen3.5:9b-q4_K_M"
    assert s.llm.think is True and s.llm.num_ctx == 16384
    assert s.stt.model == "mlx-community/whisper-large-v3-turbo"
    assert s.tts.engine == "silero" and s.voice_name == "eugene"
    assert s.wakeword.phrase == "эй чушпан"


def test_relative_profile_path_uses_project_root_not_working_directory(tmp_path, monkeypatch):
    s = settings()
    s.persona.file = Path("personality.md")
    monkeypatch.chdir(tmp_path)
    assert s.persona_style == PROFILE.read_text(encoding="utf-8").strip()


def test_existing_style_setting_adds_to_the_profile():
    s = settings()
    s.persona.style = "Говори особенно спокойно вечером."
    prompt = build_system_prompt(s)
    assert PROFILE.read_text(encoding="utf-8").strip() in prompt
    assert s.persona.style in prompt
    assert prompt.count("# Чушпан") == 1


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_empty_profile_does_not_silently_restore_the_butler(tmp_path, missing):
    profile = tmp_path / "personality.md"
    if not missing:
        profile.write_text("  \n", encoding="utf-8")
    s = settings()
    s.persona.file = profile
    with pytest.raises(FileNotFoundError if missing else ValueError):
        build_system_prompt(s)


def test_profile_is_reloaded_between_turns_and_survives_new_conversations(tmp_path):
    profile = tmp_path / "personality.md"
    profile.write_text("Узнаваемый характер, редакция один.", encoding="utf-8")
    s = settings()
    s.persona.file = profile
    store = MemoryStore(tmp_path / "history.db")
    try:
        conv = Conversation(s, RecordingLLM(), store)
        first = conv.system_prompt
        assert conv.system_prompt == first
        profile.write_text("Узнаваемый характер, редакция два.", encoding="utf-8")
        assert "редакция два" in conv.system_prompt
        assert "редакция один" not in conv.system_prompt
        updated = conv.system_prompt
        conv.reset()
        assert conv.system_prompt == updated
        assert Conversation(s, RecordingLLM(), store).system_prompt == updated
        assert store.facts() == []
    finally:
        store.close()


def test_familiar_feminine_address_and_flexible_response_length():
    s = settings(user={"name": "Лиза", "gender": "female"})
    prompt = build_system_prompt(s)
    assert "на «ты», в женском роде" in prompt
    assert "в мужском роде" in prompt
    assert "не жёсткий лимит" in prompt and "не сокращай ответ искусственно" in prompt
    assert "Не более" not in prompt
    assert "британский дворецкий" not in prompt
    assert "на «вы»" not in prompt
    assert s.owner_address == "Лиза" and s.locale.you_label == "Ты"
    assert build_greeting(s) is None
    assert s.locale.welcome.format(addr="") == "Не за что."
    assert "К вашим услугам" not in s.locale.farewell


def test_russian_conversation_does_not_prepend_a_canned_greeting(tmp_path):
    async def scenario():
        s = settings()
        store = MemoryStore(tmp_path / "history.db")
        try:
            llm = RecordingLLM([ChatChunk(content="120.", done=True)])
            conv = Conversation(s, llm, store)
            answer = "".join([part async for part in conv.reply("Сколько будет 15 на 8?")])
            assert answer == "120."
            assert store.transcript(conv.id)[-1]["content"] == answer
        finally:
            store.close()

    asyncio.run(scenario())


def test_wait_phrase_is_not_part_of_russian_response_or_history(tmp_path):
    async def scenario():
        async def weather():
            return {"degrees": 10}

        s = settings()
        llm = RecordingLLM(
            [
                ChatChunk(tool_calls=[{"function": {"name": "get_weather", "arguments": {}}}]),
                ChatChunk(content="Десять градусов.", done=True),
            ]
        )
        registry = ToolRegistry([Tool("get_weather", "Weather", weather, slow=True)])
        store = MemoryStore(tmp_path / "history.db")
        try:
            conv = Conversation(s, llm, store, tools=registry)
            answer = "".join([part async for part in conv.reply("Какая погода?")])
            assert answer == "Десять градусов."
            assert store.transcript(conv.id)[-1]["content"] == "Десять градусов."
        finally:
            store.close()

    asyncio.run(scenario())


def test_profile_balances_sarcasm_with_serious_help_and_truthfulness():
    prompt = build_system_prompt(settings(), ["открывать приложения"], facts=[])
    for rule in (
        "Русский мат допустим изредка",
        "Не делай каждую реплику шуткой",
        "Если пользовательнице плохо",
        "Не унижай её",
        "без шуток и сарказма",
        "пока инструмент не подтвердил успех",
        "не сохраняй их без просьбы",
    ):
        assert rule in prompt
    assert prompt.startswith("Ты — Чушпан")
    assert "You are Jarvis" not in prompt and "Você é Jarvis" not in prompt


def test_text_voice_and_briefing_receive_the_same_profile_and_leave_memory_intact(tmp_path):
    async def scenario():
        s = settings()
        store = MemoryStore(tmp_path / "history.db")
        try:
            llm = RecordingLLM()
            # Test-only facts and old transcript; real user files are never opened.
            store.add_fact("Предпочитает чай.")
            memory_tool = Tool(name="remember", description="test", handler=_unused)
            registry = ToolRegistry([memory_tool])
            text_conv = Conversation(s, llm, store, tools=registry)
            store.add(text_conv.id, "assistant", "Здравствуйте, я Jarvis, ваш дворецкий.")
            speaker = RecordingSpeaker()
            voice_conv = Conversation(s, llm, store, tools=registry)
            before = store.facts()
            await text_conv.prime()
            _ = [part async for part in text_conv.reply("Просто поговорим.")]
            await reply_aloud(voice_conv, speaker, "Поговори со мной.", min_chars=1)
            expected = text_conv.system_prompt
            assert llm.primed == [expected]
            assert [request[0]["content"] for request in llm.requests] == [expected, expected]
            messages = build_messages(voice_conv, BriefingData(news=[{"title": "Новость"}]))
            assert messages[0]["content"] == expected
            assert PROFILE.read_text(encoding="utf-8").strip() in expected
            assert "Предпочитает чай." in expected
            assert store.facts() == before
            assert any("Jarvis" in m["content"] for m in llm.requests[0][1:])
            assert speaker.spoken and "Я здесь." in speaker.spoken
        finally:
            store.close()

    asyncio.run(scenario())


async def _unused(**kwargs):
    raise AssertionError("No memory write was requested")


def test_tool_error_round_keeps_the_personality_and_does_not_claim_success(tmp_path):
    async def scenario():
        s = settings()
        calls = []

        async def unavailable(name):
            calls.append(name)
            raise ToolError("Spotify is unavailable")

        registry = ToolRegistry(
            [
                Tool(
                    name="open_app",
                    description="open an app",
                    parameters={"name": {"type": "string"}},
                    handler=unavailable,
                )
            ]
        )
        llm = RecordingLLM(
            [
                ChatChunk(
                    tool_calls=[
                        {"function": {"name": "open_app", "arguments": {"name": "Spotify"}}}
                    ],
                    done=True,
                ),
                ChatChunk(content="Не вышло: Spotify недоступен.", done=True),
            ]
        )
        store = MemoryStore(tmp_path / "history.db")
        try:
            conv = Conversation(s, llm, store, tools=registry)
            spoken = "".join([part async for part in conv.reply("Открой Spotify.")])
            assert calls == ["Spotify"]
            assert "Не вышло" in spoken
            assert len(llm.requests) == 2
            assert all(request[0]["content"] == conv.system_prompt for request in llm.requests)
            result = llm.requests[1][-1]
            assert result["role"] == "tool" and '"error"' in result["content"]
            assert store.facts() == []
        finally:
            store.close()

    asyncio.run(scenario())


def test_diagnostics_use_the_same_system_prompt(monkeypatch):
    import httpx

    from backend import doctor

    s = settings()
    s.llm.think = True
    captured = []

    def get(url, **kwargs):
        data = (
            {"version": "test"} if url.endswith("/version") else {"models": [{"name": s.llm.model}]}
        )
        return httpx.Response(200, json=data)

    def post(url, *, json, **kwargs):
        captured.append(json)
        return httpx.Response(
            200, json={"message": {"content": "системы готовы"}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(doctor.httpx, "get", get)
    monkeypatch.setattr(doctor.httpx, "post", post)
    assert all(check.ok for check in doctor.check_ollama(s))
    assert captured[0]["messages"][0]["content"] == build_system_prompt(s)
    assert captured[0]["options"]["num_ctx"] == s.llm.num_ctx
    assert captured[0]["options"]["temperature"] == s.llm.temperature
    assert captured[0]["options"]["num_predict"] == 32
    assert captured[0]["think"] is False and s.llm.think is True


def test_diagnostics_report_a_broken_profile_without_sending_a_prompt(tmp_path, monkeypatch):
    import httpx

    from backend import doctor

    s = settings()
    s.persona.file = tmp_path / "missing.md"

    def get(url, **kwargs):
        data = (
            {"version": "test"} if url.endswith("/version") else {"models": [{"name": s.llm.model}]}
        )
        return httpx.Response(200, json=data)

    def post(*args, **kwargs):
        raise AssertionError("No model request with a missing personality")

    monkeypatch.setattr(doctor.httpx, "get", get)
    monkeypatch.setattr(doctor.httpx, "post", post)
    checks = doctor.check_ollama(s)
    assert checks[-1].name == "Личность" and not checks[-1].ok
    assert "missing.md" in checks[-1].detail


def test_russian_response_templates_are_brief_without_corporate_politeness():
    s = settings()
    templates = [
        s.locale.welcome,
        s.locale.sign_off,
        s.locale.sign_off_thanked,
        s.locale.farewell,
        s.locale.one_moment,
        s.locale.no_reply,
        s.locale.timer_done,
        s.locale.timer_done_label,
    ]
    banned = [
        "с удовольствием помогу",
        "конечно, я могу",
        "чем ещё могу помочь",
        "буду рад помочь",
        "отличный вопрос",
        "пожалуйста, обращайтесь",
        "всегда здесь, чтобы помочь",
        "к вашим услугам",
    ]
    for template in templates:
        assert len(template) < 160
        assert any("а" <= char.lower() <= "я" for char in template)
        assert not any(phrase in template.casefold() for phrase in banned)
    profile = s.persona_style
    assert "дерзкий" in profile and "жив" in profile
    assert "одно-два коротких предложения" in profile
    assert "Не делай каждую реплику шуткой" in profile
    assert "Если пользовательнице плохо" in profile and "без шуток и сарказма" in profile


def test_turn_local_tone_distinguishes_distress_from_everyday_fatigue():
    from backend.brain.prompts import build_response_hint

    s = settings()
    serious = build_response_hint(s, "Мне очень тяжело и страшно")
    assert "без шуток и мата" in serious and "один конкретный вопрос" in serious
    fatigue = build_response_hint(s, "Я устала писать код")
    assert "Бытовая усталость" in fatigue and "без лекций" in fatigue
    assert "Без непрошеных советов" in build_response_hint(s, "Сколько заряда?")
    assert "женском роде" in build_response_hint(s, "Объясни ошибку")
    assert "не обвиняй" in build_response_hint(s, "Как дела?").casefold()


def test_java_regression_distinguishes_locals_fields_and_runtime_null():
    from backend.brain.prompts import build_response_hint

    hint = build_response_hint(settings(), "Почему student.getName() выбрасывает NPE в Java?")
    assert "локальная Student student;" in hint and "не компилируется" in hint
    assert "Student student = null;" in hint and "NPE при выполнении" in hint
    assert "Поля объекта" in hint and "default null" in hint
    assert "Без кода getter" in hint and "Без подколов" in hint
