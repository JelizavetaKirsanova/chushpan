"""Independent observations; unknown cost or controls never means unavailable video."""

import re
from urllib.parse import urlsplit, urlunsplit

from backend.cinema.classification import FULL_TYPES, PROMO_TYPES


def public_url(value):
    """Diagnostics retain the target path, never signed queries, fragments or credentials."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def observations(row, kind):
    exact = row.get("kind") == "page" and row.get("match", 0) >= 0.95
    episode = kind == "movie" or row.get("scope") == "episode"
    full = row.get("content_verified") is True and row.get("video_type") in FULL_TYPES
    promo = row.get("video_type") in PROMO_TYPES
    reachable = row.get("availability") == "available"
    blocked = row.get("region_access") == "blocked"
    playable = exact and episode and full and reachable and not blocked and not promo
    probable = row.get("manual_candidate") and row.get("identity_status") == "probable"
    browser = (exact or probable) and reachable and not blocked and not promo
    state = (
        "region_blocked"
        if blocked
        else "trailer"
        if promo
        else "playable"
        if playable
        else "browser_only"
        if browser
        else "not_found"
        if row.get("kind") in {"home", "search"}
        else "unverified"
    )
    return {
        "page_status": row.get("availability", "unknown"),
        "identity_status": "confirmed" if exact else "probable" if probable else "unconfirmed",
        "episode_status": "confirmed" if exact and episode else "unconfirmed",
        "video_status": row.get("video_type", "unknown"),
        "region_status": row.get("region_access", "unknown"),
        "player_status": "compatible" if row.get("embedded") and full else "unverified",
        "control_status": "unverified",
        "playback_status": "unconfirmed",
        "browser_openable": browser,
        "viewing_status": state,
    }


def diagnostic_reason(row):
    # Do not replace HTTP/provider failures with the default classification explanation.
    return "; ".join(
        dict.fromkeys(str(row[k]) for k in ("reason", "classification_reason") if row.get(k))
    )


def safe_reason(value):
    text = re.sub(r"https?://[^\s<>\"]+", lambda m: public_url(m[0]), str(value))
    return re.sub(
        r"(?i)(bearer|authorization|api[_-]?key|token|secret|credential)"
        r"[\s:=]+[^\s,;]+",
        r"\1 [redacted]",
        text,
    )[:1500]
