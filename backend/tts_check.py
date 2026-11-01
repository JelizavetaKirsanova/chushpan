"""Explicit local speech diagnostics; no calendar access, audio input or playback."""

import json
import re
import wave
from pathlib import Path

from backend.audio.russian import PROTECTED, normalize
from backend.audio.tts import SileroSpeaker

SAMPLES = {
    "calendar": "Сегодня в календаре: Diskreetne matemaatika. Programmeerimise algkursus. "
    "Andmebaasid. Tarkvaratehnika. Sissejuhatus infotehnoloogiasse. "
    "IT süsteemide arendus. Tallinna Tehnikaülikool.",
    "abbreviations": "ДТП. ГИБДД. МЧС. МВД. API. HTML. CSS. SQL. USB. CPU.",
    "mixed": "Сегодня Diskreetne matemaatika в Jõhvi, завтра Kohtla-Järve. "
    "ДТП рядом с TalTech. API и IT süsteemide arendus.",
}


def run(settings, args) -> int:
    custom = bool(args.text_file)
    source = Path(args.text_file).read_text() if custom else SAMPLES[args.sample]
    details = []
    spoken = normalize(source, settings.tts.pronunciations, diagnostics=details)
    show = not custom or args.show_text
    report = {
        "engine": settings.tts.engine,
        "voice": settings.tts.silero_speaker,
        "model": settings.voice_path.name,
        "source": PROTECTED.sub("[защищено]", source) if show else "[скрыто]",
        "normalized": spoken if show else "[скрыто]",
        "protected_fragments": sum(item["kind"] == "protected_literal" for item in details),
        "estimated_words": sum(item["kind"] == "estimated_foreign" for item in details),
        "unrendered_latin_tokens": len(re.findall(r"[A-Za-zõäöüÕÄÖÜ]+", spoken)),
        "synthesis": "not_requested",
        "playback": False,
    }
    if args.synthesize:
        if settings.tts.engine != "silero":
            report.update(synthesis="error", error_type="ConfiguredEngineIsNotSilero")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        speaker = SileroSpeaker(
            settings.voice_path,
            speaker=settings.tts.silero_speaker,
            pronunciations=settings.tts.pronunciations,
        )
        try:
            audio = speaker._synth(source)
            path = Path(args.output_dir) / (("custom" if custom else args.sample) + ".wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(speaker.sample_rate)
                stream.writeframes(audio.tobytes())
            report.update(
                synthesis="generated",
                wav=str(path.resolve()),
                duration_s=round(len(audio) / speaker.sample_rate, 3),
            )
        except Exception as exc:
            report.update(synthesis="error", error_type=type(exc).__name__)
        finally:
            speaker._synth_pool.shutdown()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["synthesis"] == "error" else 0
