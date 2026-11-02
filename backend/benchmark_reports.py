"""Timing-only comparisons from explicit, isolated local benchmark runs."""

import json
from collections import Counter


def summarize(report):
    from backend.performance import aggregate

    samples = report["samples"]

    def stats(rows):
        return aggregate([{**r, "first_content_s": r.get("first_pcm_ready_s")} for r in rows])

    return {
        "summary": stats(samples),
        "categories": {
            key: stats([r for r in samples if r["category"] == key])
            for key in dict.fromkeys(r["category"] for r in samples)
        },
        "configuration": report["configuration"],
        "model_warmup_s": report.get("model_warmup_s"),
        "tts_warmup_s": report.get("tts_warmup_s"),
    }


def comparison(directory):
    reports = {}
    for label in ("before", "after"):
        for path in sorted(directory.glob(f"{label}-*.json"), reverse=True)[:10]:
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                report = json.loads(path.read_text())
                if (
                    report.get("audio_played") is not False
                    or report.get("cloud_requests") is not False
                ):
                    continue
                if not report.get("samples"):
                    continue
                summarize(report)  # Validate before selecting; damaged diagnostics do not break UI.
                reports[label] = report
                break
            except (OSError, ValueError, KeyError, TypeError):
                continue
    if len(reports) != 2:
        return None
    before, after = reports["before"], reports["after"]
    if Counter(r["category"] for r in before["samples"]) != Counter(
        r["category"] for r in after["samples"]
    ):
        return None
    if before.get("method") != after.get("method"):
        return None
    return {"metric": "pcm_ready", "before": summarize(before), "after": summarize(after)}
