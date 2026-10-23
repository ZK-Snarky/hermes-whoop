"""Public result envelope and provider-independent record helpers."""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ENDPOINTS = {"cycle": "/cycle", "recovery": "/recovery", "sleep": "/activity/sleep",
             "workout": "/activity/workout", "body": "/user/measurement/body",
             "profile": "/user/profile/basic"}
COLLECTIONS = ("cycle", "recovery", "sleep", "workout")
SCOPES = ("offline", "read:recovery", "read:sleep", "read:cycles", "read:workout",
          "read:profile", "read:body_measurement")
ALIASES = {"cycles": "cycle", "recoveries": "recovery", "sleeps": "sleep",
           "workouts": "workout", "body_measurement": "body", "body_measurements": "body"}


class WhoopError(Exception):
    def __init__(self, code, recovery="retry", **details):
        self.details = {"code": code, "recovery": recovery, **details}
        super().__init__(code)


def result(data=None, *, error=None, fetched_at=None, stale=False, **extra):
    return {"success": error is None, "data": data, "fetched_at": fetched_at,
            "stale": stale, "error": error, **extra}


def kind_name(kind):
    name = ALIASES.get(kind, kind)
    if name not in ENDPOINTS:
        raise WhoopError("invalid_kind", "use_capabilities")
    return name


def parse_time(value):
    if not isinstance(value, str):
        raise WhoopError("invalid_timestamp", "use_iso8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise WhoopError("invalid_timestamp", "use_iso8601") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value):
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def record_time(kind, record, fallback=None):
    keys = ("created_at", "updated_at") if kind == "recovery" else ("start", "created_at", "updated_at")
    for key in keys:
        if record.get(key):
            return iso(parse_time(record[key]))
    return fallback


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def cycle_day(record, timezone_name="UTC"):
    return (parse_time(record["start"]) + timedelta(hours=12)).astimezone(ZoneInfo(timezone_name)).date().isoformat()


def summary_record(kind, record, timezone_name="UTC"):
    score = record.get("score") or {}
    out = {key: record[key] for key in ("id", "cycle_id", "sleep_id", "start", "end", "created_at", "score_state", "nap", "sport_id") if key in record}
    fields = {"recovery": ("recovery_score", "resting_heart_rate", "hrv_rmssd_milli", "spo2_percentage", "skin_temp_celsius"),
              "sleep": ("sleep_performance_percentage", "sleep_efficiency_percentage", "sleep_consistency_percentage", "respiratory_rate"),
              "cycle": ("strain", "average_heart_rate", "max_heart_rate"),
              "workout": ("strain", "average_heart_rate", "max_heart_rate", "kilojoule"),
              "body": ("height_meter", "weight_kilogram", "max_heart_rate"), "profile": ()}
    for key in fields[kind]:
        value = score.get(key, record.get(key))
        if value is not None:
            out[key] = value
    if kind == "profile":
        return {"available": bool(record)}
    if kind == "sleep":
        stages = score.get("stage_summary") or {}
        parts = [number(stages.get(k)) for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli")]
        if all(v is not None for v in parts):
            out["sleep_hours"] = round(sum(parts) / 3600000, 3)
        elif number(stages.get("total_in_bed_time_milli")) is not None and number(stages.get("total_awake_time_milli")) is not None:
            out["sleep_hours"] = round(max(0, float(stages["total_in_bed_time_milli"]) - float(stages["total_awake_time_milli"])) / 3600000, 3)
    if kind == "cycle" and record.get("start"):
        out["step_date"] = cycle_day(record, timezone_name)
        steps = number(record.get("step_count", score.get("step_count")))
        if steps is not None:
            out["step_count"] = int(steps)
    return out


def private_view(kind, record, mode, timezone_name="UTC"):
    if mode == "raw":
        return record
    if mode == "summary":
        return summary_record(kind, record, timezone_name)
    if mode != "structured":
        raise WhoopError("invalid_privacy_mode", "choose_summary_structured_or_raw")
    # Keep nested API scores for consumers, but strip identity and location recursively.
    removed = {"user_id", "email", "first_name", "last_name", "name", "latitude", "longitude", "gps", "location"}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k.lower() not in removed}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(record)
