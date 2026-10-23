"""Focused cross-boundary checks: fake HTTP -> auth/archive -> public API/tools.

All identities and tokens below are synthetic. No account or network is needed.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from hermes_whoop import WhoopService
from hermes_whoop.common import iso, parse_time
from hermes_whoop.http import retry_delay
from hermes_whoop.plugin import TOOLS, make_service, register

NOW = parse_time("2026-01-15T12:00:00Z").timestamp()
TOKEN = {"access_token": "test-access", "refresh_token": "test-refresh", "expires_at": NOW + 3600,
         "scope": "offline read:recovery read:sleep read:cycles read:workout read:profile read:body_measurement"}


def record(kind, rid=1, when="2026-01-15T08:00:00Z"):
    if kind == "recovery":
        return {"cycle_id": rid, "user_id": 999, "created_at": when, "score_state": "SCORED", "score": {"recovery_score": 78, "hrv_rmssd_milli": 42, "resting_heart_rate": 56}}
    if kind == "body":
        return {"height_meter": 1.8, "weight_kilogram": 75, "max_heart_rate": 190}
    if kind == "profile":
        return {"user_id": 999, "email": "synthetic@example.invalid", "first_name": "Example"}
    answer = {"id": rid, "user_id": 999, "start": when, "end": "2026-01-15T09:00:00Z", "score_state": "SCORED", "score": {"strain": 5.2}}
    if kind == "sleep":
        answer["score"] = {"sleep_performance_percentage": 84, "stage_summary": {"total_light_sleep_time_milli": 14400000, "total_slow_wave_sleep_time_milli": 3600000, "total_rem_sleep_time_milli": 7200000, "total_in_bed_time_milli": 28800000, "total_awake_time_milli": 3600000}}
    return answer


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


def failure(status, body=None, headers=None):
    return urllib.error.HTTPError("https://api.prod.whoop.com/test", status, "synthetic", headers or {}, io.BytesIO(json.dumps(body or {}).encode()))


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.http = FakeHTTP()
        self.sleeps = []
        self.now = NOW
        self.service = WhoopService(self.temp.name, client_id="example-client", client_secret="example-secret", opener=self.http,
                                   clock=lambda: self.now, sleeper=self.sleeps.append)
        self.assertTrue(self.service.import_tokens(TOKEN)["success"])

    def tearDown(self):
        self.temp.cleanup()

    def test_oauth_headless_state_rotation_permissions_and_replay(self):
        begun = self.service.begin_auth()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(begun["data"]["authorization_url"]).query)
        self.assertEqual(len(query["state"][0]), 8)
        self.assertIn("read:body_measurement", query["scope"][0])
        bad = self.service.complete_auth("http://127.0.0.1:8787/callback?code=x&state=wrong")
        self.assertEqual(bad["error"]["code"], "oauth_state_mismatch")
        self.http.responses.append({"access_token": "new-test-access", "refresh_token": "new-test-refresh", "expires_in": 3600})
        url = "http://127.0.0.1:8787/callback?code=example-code&state=" + query["state"][0]
        done = self.service.complete_auth(url)
        self.assertTrue(done["success"])
        self.assertNotIn("new-test-access", json.dumps(done))
        self.assertEqual(self.service.complete_auth(url)["error"]["code"], "auth_session_expired")
        self.assertEqual(os.stat(Path(self.temp.name) / "tokens.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.temp.name).st_mode & 0o777, 0o700)

    def test_callback_expiry_denial_duplicates_and_redirect(self):
        url = self.service.begin_auth()["data"]["authorization_url"]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        base = "http://127.0.0.1:8787/callback?state=" + state
        self.assertEqual(self.service.complete_auth(base + "&code=a&code=b")["error"]["code"], "invalid_callback")
        self.assertEqual(self.service.complete_auth(base.replace("127.0.0.1", "example.invalid"))["error"]["code"], "callback_uri_mismatch")
        self.assertEqual(self.service.complete_auth(base + "&error=access_denied")["error"]["code"], "authorization_denied")
        self.service.begin_auth()
        self.now += 601
        self.assertEqual(self.service.complete_auth(base)["error"]["code"], "auth_session_expired")

    def test_refresh_on_401_persists_rotation_and_fetches(self):
        self.http.responses += [failure(401), {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}, {"records": [record("recovery")]}]
        answer = self.service.get_latest("recovery")
        self.assertTrue(answer["success"], answer)
        self.assertEqual(len(self.http.requests), 3)
        self.assertEqual(self.http.requests[-1].get_header("Authorization"), "Bearer rotated-access")
        persisted = self.service.store.read_json("tokens.json")
        self.assertEqual(persisted["refresh_token"], "rotated-refresh")
        self.assertNotIn("rotated-refresh", json.dumps(answer))

    def test_terminal_grant_does_not_retry_or_misclassify_scope_network(self):
        self.http.responses += [failure(400, {"error": "invalid_grant", "secret": "should-not-escape"})]
        answer = self.service.refresh(force=True)
        self.assertEqual(answer["error"]["code"], "reauth_required")
        self.service.refresh(force=True)
        self.assertEqual(len(self.http.requests), 1)
        self.assertNotIn("should-not-escape", json.dumps(answer))

    def test_scope_error_is_not_reauth_or_refresh_loop(self):
        self.http.responses.append(failure(403))
        answer = self.service.get_latest("body")
        self.assertEqual(answer["error"]["code"], "scope_missing")
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(self.service.status()["data"]["auth_state"], "connected")

    def test_retry_after_and_persisted_defer(self):
        self.http.responses += [failure(429, headers={"Retry-After": "2"}), failure(503), {"records": [record("cycle")]}]
        self.assertTrue(self.service.fetch(kinds=["cycle"])["success"])
        self.assertEqual(self.sleeps, [2, 2])
        self.http.responses.append(failure(429, headers={"Retry-After": "120"}))
        deferred = self.service.fetch(kinds=["cycle"])
        self.assertEqual(deferred["error"]["retry_at"], iso(NOW + 120))
        self.service.fetch(kinds=["cycle"])
        self.assertEqual(len(self.http.requests), 4)
        self.assertEqual(retry_delay({"Retry-After": "Thu, 15 Jan 2026 12:01:00 GMT"}, 0, NOW), 60)

    def test_paginated_fetch_deduplicates_and_raw_archive(self):
        self.http.responses += [{"records": [record("cycle", 1)], "next_token": "page-two"}, {"records": [record("cycle", 1), record("cycle", 2)]}]
        self.assertTrue(self.service.fetch(kinds=["cycle"])["success"])
        self.assertIn("nextToken=page-two", self.http.requests[1].full_url)
        listed = self.service.list_records("cycles", limit=1)
        self.assertEqual(listed["next_offset"], 1)
        self.assertEqual(self.service.status()["data"]["inventory"]["cycle"]["count"], 2)
        self.assertEqual(len(list(Path(self.temp.name, "raw").glob("*.json"))), 2)
        self.assertEqual(os.stat(Path(self.temp.name) / "archive.sqlite3").st_mode & 0o777, 0o600)

    def test_backfill_checkpoint_resumes_page_after_failure(self):
        # One backfill chunk: cycle succeeds, recovery page 2 fails, sleep/workout finish.
        self.http.responses += [{"records": [record("cycle")]}, {"records": [record("recovery")], "next_token": "resume-here"}, failure(404), {"records": []}, {"records": []}]
        args = {"start": "2026-01-14", "end": "2026-01-16", "chunk_days": 2, "max_chunks": 1}
        failed = self.service.backfill(**args)
        self.assertFalse(failed["success"])
        self.http.responses += [{"records": [record("cycle")]}, {"records": [record("recovery", 2)]}, {"records": []}, {"records": []}]
        finished = self.service.backfill(**args)
        self.assertTrue(finished["success"], finished)
        self.assertTrue(finished["data"]["complete"])
        self.assertIn("nextToken=resume-here", self.http.requests[6].full_url)
        self.assertEqual(self.service.status()["data"]["inventory"]["recovery"]["count"], 2)
        self.assertEqual(self.service.backfill(**args)["data"]["chunks"], 0)

    def test_pagination_loop_fails_without_fresh_marker(self):
        self.http.responses += [{"records": [record("cycle")], "next_token": "loop"}, {"records": [], "next_token": "loop"}]
        answer = self.service.fetch(kinds=["cycle"])
        self.assertEqual(answer["error"]["code"], "pagination_loop")
        self.assertFalse(self.service.store.meta("fetch:cycle").get("fetched_at"))

    def test_expired_checkpoint_cursor_restarts_window_once(self):
        self.http.responses += [{"records": [record("cycle")], "next_token": "expired"}, failure(404)]
        args = {"kinds": ["cycle"], "start": "2026-01-14", "end": "2026-01-16"}
        self.assertFalse(self.service.fetch(**args)["success"])
        self.http.responses += [failure(400), {"records": [record("cycle")]}]
        self.assertTrue(self.service.fetch(**args)["success"])
        self.assertIn("nextToken=expired", self.http.requests[2].full_url)
        self.assertNotIn("nextToken", self.http.requests[3].full_url)

    def test_latest_separates_old_record_pending_and_missing_day_from_auth(self):
        self.http.responses += [{"records": [record("recovery", when="2026-01-10T10:00:00Z")]}]
        old = self.service.get_latest("recovery")
        self.assertEqual(old["error"]["code"], "stale_record")
        self.assertFalse(old["cache_stale"])
        self.assertIsNone(old["data"])
        recent = record("recovery")
        recent["score_state"] = "PENDING_SCORE"
        self.service.store.save("recovery", [recent], iso(NOW), {"records": [recent]})
        self.assertEqual(self.service.get_latest("recovery")["error"]["code"], "score_pending")
        self.assertEqual(self.service.get_latest("recovery", expected_day="2026-01-16")["error"]["code"], "no_current_day_record")
        self.assertEqual(self.service.status()["data"]["auth_state"], "connected")

    def test_stale_cache_cannot_be_presented_as_current_after_network_error(self):
        self.http.responses += [{"records": [record("recovery")]}]
        self.service.fetch(kinds=["recovery"])
        self.now += 70000
        self.http.responses += [urllib.error.URLError("offline")] * 3
        answer = self.service.get_latest("recovery")
        self.assertFalse(answer["success"])
        self.assertTrue(answer["stale"])
        self.assertIsNone(answer["data"])
        self.assertEqual(answer["error"]["code"], "network_error")

    def test_history_fetch_cannot_freshen_current_data(self):
        recent = record("recovery")
        self.service.store.save("recovery", [recent], iso(NOW - 80000), {"records": [recent]})
        self.http.responses.append({"records": []})
        self.service.fetch(kinds=["recovery"], start="2020-01-01", end="2020-02-01")
        answer = self.service.get_latest("recovery", refresh_if_stale=False)
        self.assertTrue(answer["cache_stale"])

    def test_active_cycle_uses_provider_update_for_freshness(self):
        current = record("cycle", when="2026-01-14T12:00:00Z")
        current["end"] = None
        current["updated_at"] = "2026-01-15T11:00:00Z"
        self.http.responses.append({"records": [current]})
        self.assertTrue(self.service.get_latest("cycle", expected_day="2026-01-15")["success"])

    def test_privacy_and_get_by_id_for_all_endpoints(self):
        for kind in ("cycle", "recovery", "sleep", "workout", "body", "profile"):
            self.http.responses.append(record(kind))
            answer = self.service.get_record(kind, "current" if kind in ("body", "profile") else "1", privacy="structured")
            self.assertTrue(answer["success"], answer)
            self.assertNotIn("user_id", answer["data"])
            self.assertNotIn("email", answer["data"])
            listed = self.service.list_records(kind)
            self.assertTrue(listed["record_ids"])
        raw = self.service.get_record("profile", "current", privacy="raw")
        self.assertIn("email", raw["data"])
        self.assertEqual(self.service.get_record("profile", "current", privacy="summary")["data"], {"available": True})
        self.assertIn("/cycle/1/recovery", self.http.requests[1].full_url)

    def test_summary_sleep_steps_local_date_and_trend(self):
        self.service.timezone = "America/New_York"
        cycle = record("cycle", when="2026-01-15T04:00:00Z")  # 11pm Jan 14, step date Jan 15
        cycle["step_count"] = 6000
        cycle["end"] = None
        for kind, rows in (("cycle", [cycle]), ("recovery", [record("recovery", 2, "2026-01-14T12:00:00Z"), record("recovery")]), ("sleep", [record("sleep")]), ("workout", [])):
            self.service.store.save(kind, rows, iso(NOW), {"records": rows})
            self.service.store.set_meta("fetch:" + kind, {"fetched_at": iso(NOW), "end": iso(NOW)})
        day = self.service.summary(days=1, end="2026-01-15", refresh=False)["data"]["days"][0]
        self.assertEqual(day["date"], "2026-01-15")
        self.assertEqual(day["steps"]["count"], 6000)
        self.assertFalse(day["steps"]["final"])
        self.assertEqual(day["sleep"]["sleep_hours"], 7)
        trend = self.service.trend(days=1, compare_days=1, end="2026-01-15", refresh=False)
        self.assertEqual(trend["data"]["metrics"]["recovery_score"]["delta"], 0)
        self.assertEqual(trend["data"]["metrics"]["recovery_score"]["current_samples"], 1)
        self.now += 5500
        later = self.service.summary(days=1, end="2026-01-15", refresh=False)["data"]["days"][0]
        self.assertNotIn("steps", later)
        self.assertNotIn("step_count", later["cycle"])

    def test_revoke_preserves_archive_and_does_not_clear_on_failure(self):
        self.service.store.save("cycle", [record("cycle")], iso(NOW), {})
        self.http.responses.append(failure(403))
        self.assertFalse(self.service.revoke()["success"])
        self.assertTrue(self.service.status()["data"]["token_present"])
        self.http.responses.append({})
        self.assertTrue(self.service.revoke()["success"])
        self.assertFalse(self.service.status()["data"]["token_present"])
        self.assertEqual(self.service.status()["data"]["inventory"]["cycle"]["count"], 1)
        self.assertEqual(self.http.requests[-1].method, "DELETE")

    def test_stock_register_to_tool_to_archive_e2e(self):
        class Context:
            def __init__(self):
                self.tools = {}
            def get_config(self, key, default=None):
                return default
            def register_tool(self, **spec):
                self.tools[spec["name"]] = spec
            def register_skill(self, name, path):
                self.skill = path
        ctx = Context()
        with patch("hermes_whoop.plugin.make_service", return_value=self.service):
            register(ctx)
            self.http.responses.append({"records": [record("recovery")]})
            answer = json.loads(ctx.tools["whoop_latest"]["handler"]({"kind": "recovery"}, session_id="synthetic"))
            self.assertTrue(answer["success"], answer)
            self.assertEqual(answer["data"]["score"]["recovery_score"], 78)
            self.assertEqual(len(ctx.tools), len(TOOLS))
            self.assertTrue(ctx.skill.exists())

    def test_plugin_settings_reload_preserves_canonical_token_store(self):
        class Context:
            def __init__(self, data_dir):
                self.settings = {"data_dir": data_dir, "timezone": "UTC", "privacy": "structured"}
                self.tools = {}
            def get_config(self, key, default=None):
                return self.settings.get(key, default)
            def register_tool(self, **spec):
                self.tools[spec["name"]] = spec["handler"]
            def register_skill(self, name, path):
                pass
        ctx = Context(self.temp.name)
        token_file = Path(self.temp.name) / "tokens.json"
        original_tokens = token_file.read_bytes()
        with patch("hermes_whoop.plugin.make_service", wraps=make_service) as factory, patch("hermes_whoop.http.HTTP.request") as network:
            register(ctx)
            status = lambda: json.loads(ctx.tools["whoop_status"]({}))
            self.assertTrue(status()["data"]["token_present"])
            self.assertTrue(status()["success"])
            self.assertEqual(factory.call_count, 1)

            ctx.settings["timezone"] = "America/New_York"
            self.assertEqual(status()["data"]["timezone"], "America/New_York")
            ctx.settings["privacy"] = "summary"
            self.assertEqual(status()["data"]["privacy"], "summary")
            self.assertEqual(factory.call_count, 3)
            self.assertEqual(token_file.read_bytes(), original_tokens)

            ctx.settings["data_dir"] = str(Path(self.temp.name) / "separate")
            self.assertFalse(status()["data"]["token_present"])
            ctx.settings["data_dir"] = self.temp.name
            self.assertTrue(status()["data"]["token_present"])
            self.assertEqual(factory.call_count, 5)
            self.assertEqual(token_file.read_bytes(), original_tokens)
            self.assertFalse((Path(self.temp.name) / "separate" / "tokens.json").exists())
            network.assert_not_called()

    def test_doctor_exact_fix_and_no_secret_disclosure(self):
        self.service.client_id = None
        self.service.client_secret = None
        answer = self.service.doctor()
        self.assertFalse(answer["success"])
        self.assertIn("WHOOP_CLIENT_ID", answer["data"]["fixes"][0]["fix"])
        self.assertNotIn("test-access", json.dumps(answer))
        self.assertNotIn("test-refresh", json.dumps(answer))

    def test_setup_cli_e2e_with_no_configuration(self):
        import subprocess
        import sys
        env = {"PATH": os.environ["PATH"], "WHOOP_DATA_DIR": self.temp.name}
        process = subprocess.run([sys.executable, "-m", "hermes_whoop", "setup"], env=env, capture_output=True, text=True)
        answer = json.loads(process.stdout)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(answer["error"]["code"], "doctor_action_required")
        self.assertEqual(len(answer["data"]["steps"]), 3)


if __name__ == "__main__":
    unittest.main()
