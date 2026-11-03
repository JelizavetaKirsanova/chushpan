"""Ten repetitions per requested safety category, using mocked models/STT/playback only."""

import pytest

from tests.test_low_latency import test_natural_commands_use_guarded_registry_without_model as local
from tests.test_providers import (
    test_disabled_fallback_does_not_call_ollama as offline,
)
from tests.test_providers import (
    test_fallback_only_reports_success_after_local_output as fallback,
)
from tests.test_voice_interaction import (
    test_neutral_followup_without_name_is_heard_after_an_accepted_turn as followup,
)
from tests.test_voice_interaction import (
    test_silero_skips_punctuation_and_chunks_all_long_text_without_devices as long_answer,
)
from tests.test_voice_overhaul import (
    test_gemini_reasoning_keeps_model_default_for_complex_requests as reasoning,
)
from tests.test_voice_reliability import (
    test_barge_requires_addressed_non_echo_speech as barge,
)
from tests.test_voice_reliability import (
    test_non_speech_never_reaches_stt_or_response as noise,
)


@pytest.mark.parametrize("iteration", range(10))
@pytest.mark.parametrize(
    "category",
    [
        "local",
        "compound",
        "ordinary",
        "complex",
        "followup",
        "barge",
        "gemini_unavailable",
        "ollama_fallback",
        "long_answer",
        "noise",
    ],
)
def test_matrix(iteration, category):
    if category == "local":
        local("Сделай звук тише", [("set_volume", {"direction": "down"})])
    elif category == "compound":
        local(
            "Сделай звук тише и открой Safari",
            [("set_volume", {"direction": "down"}), ("open_app", {"name": "Safari"})],
        )
    elif category == "ordinary":
        reasoning("Что такое радуга?", "low")
    elif category == "complex":
        reasoning("Докажи теорему и приведи источники", None)
    elif category == "followup":
        followup()
    elif category == "barge":
        barge("addressed_user")
    elif category == "gemini_unavailable":
        offline()
    elif category == "ollama_fallback":
        fallback()
    elif category == "long_answer":
        long_answer()
    else:
        noise("rustle")
