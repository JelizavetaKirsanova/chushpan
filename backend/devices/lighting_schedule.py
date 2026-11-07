"""UTC deadlines, DST-safe wall clock rules and local approximate solar events."""

from datetime import UTC, date, datetime, time, timedelta
from math import acos, asin, atan, cos, degrees, radians, sin, tan
from zoneinfo import ZoneInfo

from backend.devices.lighting_models import Rule


def solar(day: date, latitude: float, longitude: float, setting: bool):
    """Approximate USNO sunrise/sunset (90.833 degree zenith), no location lookup.

    Polar day/night returns None. This approximation is for household schedules,
    not navigation. Round trips to the requested local day are checked below.
    """
    n = day.timetuple().tm_yday
    lng = longitude / 15
    t = n + ((18 if setting else 6) - lng) / 24
    mean = 0.9856 * t - 3.289
    longitude_sun = (
        mean + 1.916 * sin(radians(mean)) + 0.020 * sin(2 * radians(mean)) + 282.634
    ) % 360
    ra = degrees(atan(0.91764 * tan(radians(longitude_sun)))) % 360
    ra += (int(longitude_sun / 90) - int(ra / 90)) * 90
    ra /= 15
    decl = asin(0.39782 * sin(radians(longitude_sun)))
    denominator = cos(decl) * cos(radians(latitude))
    if abs(denominator) < 1e-10:
        return None
    ch = (cos(radians(90.833)) - sin(decl) * sin(radians(latitude))) / denominator
    if not -1 <= ch <= 1:
        return None
    hour = degrees(acos(ch)) if setting else 360 - degrees(acos(ch))
    utc_hour = (hour / 15 + ra - 0.06571 * t - 6.622 - lng) % 24
    return datetime.combine(day, time(), UTC) + timedelta(hours=utc_hour)


def next_occurrence(rule: Rule, after: datetime, anchor: datetime | None = None):
    after = after.astimezone(UTC)
    if rule.kind == "once":
        return None
    if rule.kind == "interval":
        anchor = anchor or after
        count = max(1, int((after - anchor).total_seconds() // rule.interval_s) + 1)
        return anchor + timedelta(seconds=count * rule.interval_s)
    zone = ZoneInfo(rule.timezone)
    local_day = after.astimezone(zone).date()
    for offset in range(371):
        day = local_day + timedelta(days=offset)
        if rule.kind == "date":
            day = date.fromisoformat(rule.date)
        elif day.weekday() not in rule.weekdays:
            continue
        if rule.kind in {"sunrise", "sunset"}:
            candidate = solar(day, rule.latitude, rule.longitude, rule.kind == "sunset")
            if candidate is None:
                continue
            # UTC may be on the previous/next date for an eastern/western longitude.
            candidate += timedelta(days=(day - candidate.astimezone(zone).date()).days)
            candidate += timedelta(minutes=rule.offset_minutes)
        else:
            wall = datetime.combine(day, time.fromisoformat(rule.at))
            candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) != wall:
                continue  # Nonexistent DST time: skip; repeated time runs only once.
        if candidate > after:
            return candidate
        if rule.kind == "date":
            return None
    return None
