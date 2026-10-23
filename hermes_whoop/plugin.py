"""Stock Hermes register(ctx) entry point; all tools use the same service."""
import json
import os
from pathlib import Path

from .common import ENDPOINTS, result
from .service import WhoopService


def default_data_dir():
    if os.environ.get("WHOOP_DATA_DIR"):
        return Path(os.environ["WHOOP_DATA_DIR"]).expanduser()
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("hermes-whoop")
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "plugin-data" / "hermes-whoop"


def make_service(data_dir=None, **settings):
    return WhoopService(data_dir or default_data_dir(), timezone=settings.get("timezone") or os.environ.get("WHOOP_TIMEZONE", "UTC"),
                        privacy=settings.get("privacy") or os.environ.get("WHOOP_PRIVACY_MODE", "summary"))


KIND = {"type": "string", "enum": list(ENDPOINTS)}
DATE = {"type": "string", "description": "ISO 8601 timestamp or YYYY-MM-DD (UTC if no offset)."}
PRIVACY = {"type": "string", "enum": ["summary", "structured", "raw"], "description": "Raw includes identity and full provider records; request only when needed."}
BOOLEAN = {"type": "boolean"}
INTEGER = {"type": "integer"}
READ = {"kind": KIND, "privacy": PRIVACY}

# name, service method, description, properties, required, fixed arguments
TOOLS = [
    ("whoop_capabilities", "capabilities", "Discover WHOOP tools, data types, scopes, privacy and onboarding.", {}, [], {}),
    ("whoop_status", "status", "Inspect WHOOP connection, cache freshness and data inventory without health records.", {}, [], {}),
    ("whoop_doctor", "doctor", "Diagnose WHOOP setup and return the exact fix for each failure.", {"online": BOOLEAN}, [], {}),
    ("whoop_auth_url", "begin_auth", "Start WHOOP OAuth on a headless or local host. Open the returned URL in a browser.", {}, [], {}),
    ("whoop_callback", "complete_auth", "Complete WHOOP OAuth using the complete redirected URL, including state.", {"callback_url": {"type": "string"}}, ["callback_url"], {}),
    ("whoop_refresh", "refresh", "Refresh expiring WHOOP tokens. Never returns credentials.", {"force": BOOLEAN}, [], {}),
    ("whoop_fetch", "fetch", "Fetch WHOOP records into the private archive with pagination and retry recovery.", {"kinds": {"type": "array", "items": KIND}, "start": DATE, "end": DATE, "days": INTEGER}, [], {}),
    ("whoop_backfill", "backfill", "Archive WHOOP history in resumable chunks. Repeat until complete is true.", {"start": DATE, "end": DATE, "chunk_days": INTEGER, "max_chunks": INTEGER, "resume": BOOLEAN}, ["start"], {}),
    ("whoop_latest", "get_latest", "Get the latest WHOOP record by type with separate cache and observation freshness guards. Never present stale data as current.", {**READ, "max_age_seconds": {"type": "number"}, "max_record_age_seconds": {"type": "number"}, "refresh_if_stale": BOOLEAN, "expected_day": DATE, "allow_stale": BOOLEAN}, ["kind"], {}),
    ("whoop_list", "list_records", "List recovery, sleep, cycle, workout, body snapshots or profile records. Return next_offset for pagination.", {**READ, "start": DATE, "end": DATE, "limit": INTEGER, "offset": INTEGER, "refresh": BOOLEAN}, ["kind"], {}),
    ("whoop_get", "get_record", "Get a WHOOP record by ID. Recovery uses cycle_id; body uses local snapshot ID or current; profile uses current.", {**READ, "record_id": {"type": "string"}, "refresh": BOOLEAN}, ["kind", "record_id"], {}),
    ("whoop_daily_summary", "summary", "Summarize one local calendar day, excluding naps from main sleep. Missing measurements are not zero.", {"end": DATE, "refresh": BOOLEAN}, [], {"days": 1}),
    ("whoop_weekly_summary", "summary", "Summarize seven local calendar days of recovery, sleep, strain, steps and workout counts.", {"end": DATE, "refresh": BOOLEAN}, [], {"days": 7}),
    ("whoop_trend", "trend", "Compare recovery or sleep averages across adjacent windows with sample counts and deltas.", {"kind": {"type": "string", "enum": ["recovery", "sleep"]}, "days": INTEGER, "compare_days": INTEGER, "end": DATE, "refresh": BOOLEAN}, [], {}),
    ("whoop_self_heal", "self_heal", "Refresh and fetch WHOOP, recovering expired or rejected access tokens without replaying revoked refresh grants.", {}, [], {}),
    ("whoop_revoke", "revoke", "Disconnect WHOOP by revoking provider access and clearing local tokens. Preserves the archive. Use only when the user requests disconnect.", {}, [], {}),
]


def register(ctx):
    service = None
    def get_service():
        nonlocal service
        if service is None:
            service = make_service(ctx.get_config("data_dir"), timezone=ctx.get_config("timezone"), privacy=ctx.get_config("privacy"))
        return service

    def handler(method, defaults):
        def call(args=None, **kwargs):
            try:
                answer = getattr(get_service(), method)(**{**(args or {}), **defaults})
            except Exception:
                # Never expose exception text: filesystem paths, callback codes and secrets
                # can occur in exception messages from providers and runtimes.
                answer = result(error={"code": "tool_failed", "recovery": "run_doctor"})
            return json.dumps(answer, allow_nan=False)
        return call

    for name, method, description, properties, required, defaults in TOOLS:
        ctx.register_tool(name=name, toolset="whoop", description=description,
            schema={"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}},
            handler=handler(method, defaults), check_fn=lambda: True, requires_env=[])
    skill = Path(__file__).parent.parent / "skills" / "whoop" / "SKILL.md"
    ctx.register_skill("whoop", skill)
