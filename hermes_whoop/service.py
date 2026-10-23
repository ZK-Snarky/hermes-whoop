"""The single WHOOP service shared by Hermes tools, the CLI, and Python consumers."""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import os
import secrets
import sqlite3
import string
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import (COLLECTIONS, ENDPOINTS, SCOPES, WhoopError, cycle_day, iso,
                     kind_name, number, parse_time, private_view, result, summary_record)
from .http import API_URL, AUTH_URL, TOKEN_URL, HTTP
from .storage import Store


def operation(fn):
    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except WhoopError as exc:
            try:
                self._remember_error(exc.details)
            except (OSError, sqlite3.Error):
                return result(error={"code": "local_storage_error", "recovery": "check_private_data_dir"})
            return result(error=exc.details)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            return result(error={"code": "invalid_arguments", "recovery": "check_arguments"})
        except (OSError, sqlite3.Error):
            return result(error={"code": "local_storage_error", "recovery": "check_private_data_dir"})
    return run


class WhoopService:
    def __init__(self, data_dir, *, client_id=None, client_secret=None, redirect_uri=None,
                 timezone="UTC", privacy="structured", opener=None, clock=time.time,
                 sleeper=time.sleep):
        self.store = Store(data_dir)
        self.client_id = client_id if client_id is not None else os.environ.get("WHOOP_CLIENT_ID")
        self.client_secret = client_secret if client_secret is not None else os.environ.get("WHOOP_CLIENT_SECRET")
        self.redirect_uri = redirect_uri or os.environ.get("WHOOP_REDIRECT_URI", "http://127.0.0.1:8787/callback")
        self.timezone = timezone
        ZoneInfo(timezone)
        if privacy not in ("summary", "structured", "raw"):
            raise ValueError("privacy must be summary, structured, or raw")
        self.privacy, self.clock = privacy, clock
        self.http = HTTP(opener, clock, sleeper)

    def _remember_error(self, error):
        if error.get("retry_at"):
            self.store.set_meta("retry_error", error)

    def _check_retry(self):
        error = self.store.meta("retry_error")
        if error and parse_time(error["retry_at"]).timestamp() > self.clock():
            raise WhoopError(error["code"], "retry_after", retry_at=error["retry_at"])

    def _config(self):
        missing = [name for name, value in (("WHOOP_CLIENT_ID", self.client_id), ("WHOOP_CLIENT_SECRET", self.client_secret)) if not value]
        if missing:
            raise WhoopError("configuration_missing", "set_environment", missing=missing)

    def _save_tokens(self, payload, previous=None):
        previous = previous or {}
        access = payload.get("access_token")
        refresh = payload.get("refresh_token") or previous.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise WhoopError("offline_token_missing", "authorize_with_offline_scope")
        expiry = payload.get("expires_at")
        if expiry is None:
            duration = number(payload.get("expires_in"))
            if duration is None or duration <= 0:
                raise WhoopError("invalid_token_response", "authorize")
            expiry = self.clock() + duration
        elif isinstance(expiry, str) and number(expiry) is None:
            expiry = parse_time(expiry).timestamp()
        expiry = number(expiry)
        if expiry is None:
            raise WhoopError("invalid_token_response", "authorize")
        saved = {"access_token": access, "refresh_token": refresh, "expires_at": expiry,
                 "scope": payload.get("scope", previous.get("scope", "")), "auth_state": "connected"}
        self.store.write_json("tokens.json", saved)
        return saved

    def _refresh(self, force=False, rejected_token=None):
        self._check_retry()
        with self.store.lock():
            tokens = self.store.read_json("tokens.json")
            if tokens.get("auth_state") in ("reauth_required", "configuration_error"):
                raise WhoopError(tokens["auth_state"], "authorize" if tokens["auth_state"] == "reauth_required" else "fix_client_configuration")
            if rejected_token and tokens.get("access_token") != rejected_token and tokens.get("access_token"):
                return tokens
            if not force and tokens.get("access_token") and float(tokens.get("expires_at", 0)) > self.clock() + 60:
                return tokens
            self._config()
            if not tokens.get("refresh_token"):
                raise WhoopError("auth_required", "authorize")
            try:
                payload = self.http.request(TOKEN_URL, method="POST", data={"grant_type": "refresh_token", "client_id": self.client_id,
                    "client_secret": self.client_secret, "refresh_token": tokens["refresh_token"], "scope": "offline"})
                return self._save_tokens(payload, tokens)
            except WhoopError as exc:
                if exc.details["code"] in ("reauth_required", "configuration_error"):
                    tokens["auth_state"] = exc.details["code"]
                    self.store.write_json("tokens.json", tokens)
                raise

    def _request(self, path, params=None, method="GET"):
        self._check_retry()
        tokens = self._refresh()
        url = API_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return self.http.request(url, method=method, token=tokens["access_token"])
        except WhoopError as exc:
            if exc.details["code"] != "unauthorized":
                raise
            tokens = self._refresh(force=True, rejected_token=tokens["access_token"])
            try:
                return self.http.request(url, method=method, token=tokens["access_token"])
            except WhoopError as second:
                if second.details["code"] == "unauthorized":
                    with self.store.lock():
                        current = self.store.read_json("tokens.json")
                        if current.get("access_token") == tokens["access_token"]:
                            current["auth_state"] = "reauth_required"
                            self.store.write_json("tokens.json", current)
                    raise WhoopError("reauth_required", "authorize", http_status=401) from None
                raise

    @operation
    def import_tokens(self, tokens):
        """One-time migration, in-process only. Never returns token values."""
        with self.store.lock():
            if self.store.read_json("tokens.json").get("refresh_token"):
                raise WhoopError("tokens_already_present", "use_existing_tokens")
            incoming = dict(tokens)
            if "expires_at" not in incoming and "expires_in" not in incoming:
                incoming["expires_at"] = 0
            # A refresh-only migration is valid; the first read obtains an access token.
            if not incoming.get("access_token") and incoming.get("refresh_token"):
                incoming["access_token"] = "pending-refresh"
                incoming["expires_at"] = 0
            saved = self._save_tokens(incoming)
        return result({"auth_state": "connected", "expires_at": iso(saved["expires_at"])})

    def _validate_redirect(self):
        parsed = urllib.parse.urlsplit(self.redirect_uri)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path or not parsed.hostname:
            raise WhoopError("invalid_redirect_uri", "set_registered_redirect_uri")
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback and parsed.port):
            raise WhoopError("invalid_redirect_uri", "use_https_or_loopback")
        return parsed

    @operation
    def begin_auth(self):
        self._config()
        self._validate_redirect()
        # WHOOP requires an eight-character state, rather than arbitrary OAuth lengths.
        state = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        with self.store.lock():
            self.store.write_json("oauth.json", {"state": state, "expires_at": self.clock() + 600, "redirect_uri": self.redirect_uri})
        query = urllib.parse.urlencode({"client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": " ".join(SCOPES), "state": state})
        return result({"authorization_url": AUTH_URL + "?" + query, "expires_at": iso(self.clock() + 600), "callback_mode": "paste_redirect_url"})

    @operation
    def complete_auth(self, callback_url):
        expected = self._validate_redirect()
        callback = urllib.parse.urlsplit(callback_url.strip())
        if (callback.scheme, callback.netloc, callback.path) != (expected.scheme, expected.netloc, expected.path) or callback.fragment:
            raise WhoopError("callback_uri_mismatch", "paste_complete_redirect_url")
        values = urllib.parse.parse_qs(callback.query, keep_blank_values=True)
        if any(len(v) != 1 for v in values.values()):
            raise WhoopError("invalid_callback", "restart_authorization")
        state = values.get("state", [""])[0]
        with self.store.lock():
            pending = self.store.read_json("oauth.json")
            if not pending.get("state") or pending.get("expires_at", 0) <= self.clock():
                raise WhoopError("auth_session_expired", "restart_authorization")
            if pending.get("redirect_uri") != self.redirect_uri or not secrets.compare_digest(state, pending["state"]):
                raise WhoopError("oauth_state_mismatch", "paste_matching_redirect_url")
            self.store.write_json("oauth.json", {})
            if values.get("error"):
                raise WhoopError("authorization_denied", "authorize")
            code = values.get("code", [""])[0]
            if not code:
                raise WhoopError("authorization_code_missing", "restart_authorization")
            self._config()
            payload = self.http.request(TOKEN_URL, method="POST", data={"grant_type": "authorization_code", "client_id": self.client_id,
                "client_secret": self.client_secret, "redirect_uri": self.redirect_uri, "code": code})
            saved = self._save_tokens(payload)
        return result({"auth_state": "connected", "expires_at": iso(saved["expires_at"])})

    @operation
    def refresh(self, force=False):
        tokens = self._refresh(force=force)
        return result({"auth_state": "connected", "expires_at": iso(tokens["expires_at"])})

    def _range(self, start, end, days):
        stop = parse_time(end) if end else datetime.fromtimestamp(self.clock(), dt_timezone.utc) + timedelta(seconds=1)
        begin = parse_time(start) if start else stop - timedelta(days=days)
        if begin >= stop:
            raise WhoopError("invalid_date_range", "set_start_before_end")
        return iso(begin), iso(stop)

    def _fetch_one(self, kind, start, end):
        fetched_at = iso(self.clock())
        key = "page:" + hashlib.sha256((kind + start + end).encode()).hexdigest()
        checkpoint = self.store.meta(key, {})
        token = checkpoint.get("next_token")
        seen = set(checkpoint.get("seen", []))
        count = checkpoint.get("count", 0)
        cursor_restarted = False
        for _ in range(1000):
            params = {"start": start, "end": end, "limit": 25} if kind in COLLECTIONS else None
            if params and token:
                params["nextToken"] = token
            try:
                payload = self._request(ENDPOINTS[kind], params)
            except WhoopError as exc:
                # A provider cursor can expire between runs. Replay the bounded window
                # from page one once; upserts preserve records already archived.
                if token and exc.details.get("http_status") == 400 and not cursor_restarted:
                    token, seen, count, cursor_restarted = None, set(), 0, True
                    self.store.set_meta(key, {})
                    continue
                raise
            records = payload.get("records") if kind in COLLECTIONS else [payload]
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise WhoopError("invalid_provider_response", "retry")
            next_token = payload.get("next_token") or payload.get("nextToken")
            if next_token and (not isinstance(next_token, str) or next_token in seen or next_token == token):
                raise WhoopError("pagination_loop", "retry")
            self.store.save(kind, records, fetched_at, payload)
            count += len(records)
            if not next_token or kind not in COLLECTIONS:
                self.store.set_meta(key, {})
                self.store.set_meta("fetch:" + kind, {"fetched_at": fetched_at, "start": start, "end": end, "error": None})
                return count
            seen.add(next_token)
            token = next_token
            self.store.set_meta(key, {"next_token": token, "seen": sorted(seen), "count": count})
        raise WhoopError("pagination_limit", "resume_fetch")

    @operation
    def fetch(self, kinds=None, start=None, end=None, days=7):
        if not 0 < days <= 3660:
            raise WhoopError("invalid_days", "choose_1_to_3660")
        kinds = list(dict.fromkeys(kind_name(k) for k in (kinds or ENDPOINTS)))
        start, end = self._range(start, end, days)
        counts, errors = {}, {}
        # Serialize sync/backfill so a stale response cannot overwrite a newer archive page.
        with self.store.lock("sync"):
            for kind in kinds:
                try:
                    counts[kind] = self._fetch_one(kind, start, end)
                except WhoopError as exc:
                    errors[kind] = exc.details
                    self._remember_error(exc.details)
                    previous = self.store.meta("fetch:" + kind, {})
                    self.store.set_meta("fetch:" + kind, {**previous, "error": exc.details})
        error = None
        if errors:
            error = next(iter(errors.values())) if len(errors) == len(kinds) else {"code": "partial_fetch", "recovery": "inspect_endpoint_errors"}
        return result({"counts": counts, "start": start, "end": end, "endpoint_errors": errors},
                      error=error, fetched_at=iso(self.clock()) if counts else None)

    def _records(self, kind, rows, privacy):
        return [private_view(kind, json.loads(row["raw_json"]), privacy or self.privacy, self.timezone) for row in rows]

    @operation
    def list_records(self, kind, start=None, end=None, limit=100, offset=0, refresh=False, privacy=None):
        kind = kind_name(kind)
        if not 1 <= limit <= 10000 or offset < 0:
            raise WhoopError("invalid_pagination", "use_limit_1_to_10000_and_nonnegative_offset")
        if start:
            start = iso(parse_time(start))
        if end:
            end = iso(parse_time(end))
        if start and end and start >= end:
            raise WhoopError("invalid_date_range", "set_start_before_end")
        if refresh:
            synced = self.fetch(kinds=[kind], start=start, end=end)
            if not synced["success"]:
                return synced
        rows = self.store.rows(kind, start=start, end=end, limit=limit + 1, offset=offset)
        more = len(rows) > limit
        rows = rows[:limit]
        fetched = min((row["fetched_at"] for row in rows), default=None)
        return result(self._records(kind, rows, privacy), fetched_at=fetched, source="archive",
                      record_ids=[row["record_id"] for row in rows],
                      next_offset=offset + limit if more else None)

    @operation
    def get_record(self, kind, record_id, refresh=False, privacy=None):
        kind = kind_name(kind)
        record_id = str(record_id)
        rows = self.store.rows(kind, record_id=record_id, limit=1)
        if refresh or not rows:
            if kind == "body":
                # WHOOP exposes only current measurements. Historical IDs refer to our snapshots.
                if record_id != "current":
                    raise WhoopError("not_found", "list_body_snapshots")
                payload = self._request(ENDPOINTS[kind])
            elif kind == "profile":
                if record_id != "current":
                    raise WhoopError("invalid_record_id", "use_current")
                payload = self._request(ENDPOINTS[kind])
            else:
                safe_id = urllib.parse.quote(record_id, safe="")
                path = "/cycle/" + safe_id + "/recovery" if kind == "recovery" else ENDPOINTS[kind] + "/" + safe_id
                payload = self._request(path)
            self.store.save(kind, [payload], iso(self.clock()), payload)
            rows = self.store.rows(kind, limit=1) if kind == "body" else self.store.rows(kind, record_id=record_id, limit=1)
        if not rows:
            raise WhoopError("not_found", "check_record_id")
        return result(self._records(kind, rows, privacy)[0], fetched_at=rows[0]["fetched_at"], record_id=rows[0]["record_id"])

    @operation
    def get_latest(self, kind, max_age_seconds=64800, refresh_if_stale=True,
                   max_record_age_seconds=None, privacy=None, expected_day=None, allow_stale=False):
        kind = kind_name(kind)
        if max_age_seconds < 0 or (max_record_age_seconds is not None and max_record_age_seconds < 0):
            raise WhoopError("invalid_max_age", "use_nonnegative_seconds")
        state = self.store.meta("fetch:" + kind, {})
        fetched = state.get("fetched_at")
        age = self.clock() - parse_time(fetched).timestamp() if fetched else float("inf")
        cache_stale = not (-600 <= age <= max_age_seconds)
        if kind in COLLECTIONS and (not state.get("end") or self.clock() - parse_time(state["end"]).timestamp() > max_age_seconds):
            cache_stale = True
        fetch_error = None
        if refresh_if_stale and (cache_stale or state.get("error")):
            synced = self.fetch(kinds=[kind])
            fetch_error = synced["error"]
            state = self.store.meta("fetch:" + kind, {})
            fetched = state.get("fetched_at")
            age = self.clock() - parse_time(fetched).timestamp() if fetched else float("inf")
            cache_stale = not (-600 <= age <= max_age_seconds)
        rows = self.store.rows(kind, limit=1)
        if not rows:
            return result(error=fetch_error or {"code": "no_data", "recovery": "sync_device_or_backfill"}, fetched_at=fetched, stale=cache_stale)
        record = json.loads(rows[0]["raw_json"])
        row_age = self.clock() - parse_time(rows[0]["fetched_at"]).timestamp()
        cache_stale = cache_stale or not (-600 <= row_age <= max_age_seconds)
        observed = (record.get("end") if kind == "sleep" else record.get("updated_at") if kind == "cycle" else rows[0]["timestamp"])
        observed = observed or rows[0]["timestamp"]
        record_limit = max_record_age_seconds
        if record_limit is None and kind in ("recovery", "sleep", "cycle"):
            record_limit = max_age_seconds
        record_age = self.clock() - parse_time(observed).timestamp()
        record_stale = record_limit is not None and not (-600 <= record_age <= record_limit)
        error = fetch_error
        if not error and cache_stale:
            error = {"code": "stale_cache", "recovery": "fetch"}
        if not error and record_stale:
            error = {"code": "stale_record", "recovery": "sync_device"}
        if not error and expected_day:
            day = cycle_day(record, self.timezone) if kind == "cycle" else parse_time(observed).astimezone(ZoneInfo(self.timezone)).date().isoformat()
            if day != expected_day:
                error = {"code": "no_current_day_record", "recovery": "sync_device", "record_day": day}
        if not error and record.get("score_state") in ("PENDING_SCORE", "UNSCORABLE"):
            error = {"code": "score_pending" if record["score_state"] == "PENDING_SCORE" else "unscorable", "recovery": "wait_for_provider"}
        data = private_view(kind, record, privacy or self.privacy, self.timezone) if not error or allow_stale else None
        return result(data, error=error, fetched_at=rows[0]["fetched_at"], stale=cache_stale or record_stale,
                      cache_stale=cache_stale, record_stale=record_stale, record_at=observed, record_id=rows[0]["record_id"], cached_available=True)

    @operation
    def backfill(self, start, end=None, chunk_days=30, max_chunks=12, resume=True):
        if not 1 <= chunk_days <= 366 or not 1 <= max_chunks <= 1000:
            raise WhoopError("invalid_backfill_bounds", "check_chunk_days_and_max_chunks")
        begin = parse_time(start)
        key = "backfill:" + hashlib.sha256((iso(begin) + str(end) + str(chunk_days)).encode()).hexdigest()
        checkpoint = self.store.meta(key) if resume else None
        stop = parse_time(checkpoint["cursor_end"]) if checkpoint else (parse_time(end) if end else datetime.fromtimestamp(self.clock(), dt_timezone.utc))
        if begin > stop:
            raise WhoopError("invalid_date_range", "set_start_before_end")
        chunks = 0
        while stop > begin and chunks < max_chunks:
            floor = max(begin, stop - timedelta(days=chunk_days))
            fetched = self.fetch(kinds=COLLECTIONS, start=iso(floor), end=iso(stop))
            if not fetched["success"]:
                return result({"complete": False, "chunks": chunks, "cursor_end": iso(stop), "endpoint_errors": (fetched.get("data") or {}).get("endpoint_errors", {})}, error=fetched["error"])
            chunks += 1
            stop = floor
            self.store.set_meta(key, {"cursor_end": iso(stop)})
        return result({"complete": stop <= begin, "chunks": chunks, "cursor_end": iso(stop), "body_history": "local_snapshots_only"}, fetched_at=iso(self.clock()))

    @operation
    def status(self):
        tokens = self.store.read_json("tokens.json")
        inventory = self.store.inventory()
        cache = {kind: self.store.meta("fetch:" + kind, {}) for kind in ENDPOINTS}
        auth_state = tokens.get("auth_state", "auth_required")
        return result({"auth_state": auth_state, "token_present": bool(tokens.get("refresh_token")),
            "expires_at": iso(tokens["expires_at"]) if number(tokens.get("expires_at")) is not None else None,
            "scopes": tokens.get("scope", "").split(), "inventory": inventory, "cache": cache,
            "privacy": self.privacy, "timezone": self.timezone, "retry": self.store.meta("retry_error")})

    @operation
    def self_heal(self):
        # _request refreshes once on 401 and re-reads rotated tokens under an auth lock.
        # Invalid refresh grants and scope errors are terminal, not retry loops.
        return self.fetch()

    @operation
    def revoke(self):
        self._request("/user/access", method="DELETE")
        with self.store.lock():
            self.store.write_json("tokens.json", {"auth_state": "revoked"})
            self.store.write_json("oauth.json", {})
        return result({"auth_state": "revoked", "archive_preserved": True})

    @operation
    def doctor(self, online=False):
        fixes = []
        missing = [key for key, val in (("WHOOP_CLIENT_ID", self.client_id), ("WHOOP_CLIENT_SECRET", self.client_secret)) if not val]
        if missing:
            fixes.append({"code": "configuration_missing", "fix": "Set " + " and ".join(missing) + " in your private environment, then run hermes-whoop auth."})
        try:
            self._validate_redirect()
        except WhoopError:
            fixes.append({"code": "invalid_redirect_uri", "fix": "Set WHOOP_REDIRECT_URI to your registered HTTPS URL or http://127.0.0.1:8787/callback."})
        status = self.status()
        if not status["success"]:
            return status
        state = status["data"]["auth_state"]
        if state != "connected":
            fix = self._fix({"code": state}) if state == "configuration_error" else "Run hermes-whoop auth and finish with hermes-whoop auth --complete."
            fixes.append({"code": state, "fix": fix})
        scopes = status["data"]["scopes"]
        missing_scopes = sorted(set(SCOPES) - set(scopes)) if scopes else []
        if missing_scopes:
            fixes.append({"code": "scope_missing", "fix": "Run hermes-whoop auth again and grant: " + ", ".join(missing_scopes)})
        online_checked = False
        if online and not fixes:
            online_checked = True
            fetched = self.fetch()
            endpoint_errors = (fetched.get("data") or {}).get("endpoint_errors", {})
            for kind, error in endpoint_errors.items():
                fixes.append({"code": error["code"], "endpoint": kind, "fix": self._fix(error)})
            if not fetched["success"] and not endpoint_errors:
                fixes.append({"code": fetched["error"]["code"], "fix": self._fix(fetched["error"])})
            status = self.status()
        return result({"healthy": not fixes, "fixes": fixes, "status": status["data"], "online_checked": online_checked},
                      error={"code": "doctor_action_required", "recovery": "apply_fixes"} if fixes else None)

    @staticmethod
    def _fix(error):
        if error["code"] in ("reauth_required", "auth_required", "scope_missing"):
            return "Run hermes-whoop auth, approve all read scopes, then hermes-whoop auth --complete."
        if error.get("retry_at"):
            return "Run hermes-whoop fetch after " + error["retry_at"] + "."
        if error["code"] == "configuration_error":
            return "Correct WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET for the registered developer app, then run hermes-whoop auth."
        if error["code"] in ("local_storage_error", "invalid_local_state"):
            return "Check read/write permissions and free space for WHOOP_DATA_DIR; restore damaged private state from your backup."
        return "Check network access to api.prod.whoop.com, then run hermes-whoop fetch."

    @operation
    def capabilities(self):
        return result({"version": "0.1.0", "kinds": list(ENDPOINTS), "scopes": list(SCOPES),
            "operations": ["setup", "auth", "doctor", "refresh", "fetch", "backfill", "latest", "list", "get", "summary", "trend", "status", "inventory", "capabilities", "revoke"],
            "privacy_modes": ["summary", "structured", "raw"], "default_privacy": self.privacy,
            "body_history": "WHOOP offers current measurements only; list/get expose observed local snapshots.",
            "recovery_id": "cycle_id", "profile_id": "current", "latest_freshness": "cache and record checked separately",
            "onboarding_prompt": "Connect my WHOOP account using my developer app, check the connection, fetch the last week, and show a summary. Keep raw records local unless I request them."})

    @operation
    def summary(self, days=1, end=None, refresh=True):
        if not 1 <= days <= 366:
            raise WhoopError("invalid_days", "choose_1_to_366")
        zone = ZoneInfo(self.timezone)
        last_day = (date.fromisoformat(end) if len(end) == 10 else parse_time(end).astimezone(zone).date()) if end else datetime.fromtimestamp(self.clock(), zone).date()
        stop = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), zone)
        begin = stop - timedelta(days=days)
        if refresh:
            synced = self.fetch(kinds=COLLECTIONS, start=iso(begin - timedelta(days=2)), end=iso(stop))
            if not synced["success"]:
                return synced
        daily = {(begin + timedelta(days=i)).date().isoformat(): {"date": (begin + timedelta(days=i)).date().isoformat(), "workouts": 0} for i in range(days)}
        fetched = []
        for kind in COLLECTIONS:
            rows = self.store.rows(kind, start=iso(begin - timedelta(days=2)), end=iso(stop), limit=10000)
            # Ascending means the latest observation wins when a day has multiple scores.
            for row in reversed(rows):
                record = json.loads(row["raw_json"])
                if record.get("score_state") not in (None, "SCORED"):
                    continue
                if kind == "sleep" and record.get("nap"):
                    continue
                timestamp = record.get("end") if kind == "sleep" else row["timestamp"]
                day = cycle_day(record, self.timezone) if kind == "cycle" else parse_time(timestamp or row["timestamp"]).astimezone(zone).date().isoformat()
                if day not in daily:
                    continue
                projected = summary_record(kind, record, self.timezone)
                fetched.append(row["fetched_at"])
                if kind == "workout":
                    daily[day]["workouts"] += 1
                else:
                    daily[day][kind] = projected
                if kind == "cycle" and projected.get("step_count") is not None:
                    fresh_steps = 0 <= self.clock() - parse_time(row["fetched_at"]).timestamp() <= 5400
                    if record.get("end") or fresh_steps:
                        daily[day]["steps"] = {"count": projected["step_count"], "final": bool(record.get("end")), "as_of": None if record.get("end") else row["fetched_at"]}
                    else:
                        daily[day]["cycle"].pop("step_count", None)
        cache_stale = False
        for kind in COLLECTIONS:
            state = self.store.meta("fetch:" + kind, {})
            if (not all(state.get(k) for k in ("fetched_at", "start", "end")) or state.get("error")
                    or not -600 <= self.clock() - parse_time(state["fetched_at"]).timestamp() <= 64800
                    or parse_time(state["start"]) > begin.astimezone(dt_timezone.utc)
                    or parse_time(state["end"]) < min(stop.astimezone(dt_timezone.utc), datetime.fromtimestamp(self.clock(), dt_timezone.utc))):
                cache_stale = True
        return result({"days": list(daily.values()), "timezone": self.timezone, "missing_means": "not_observed"},
                      fetched_at=min(fetched) if fetched else None, stale=cache_stale)

    @operation
    def trend(self, kind="recovery", days=7, compare_days=7, end=None, refresh=True):
        kind = kind_name(kind)
        if kind not in ("recovery", "sleep") or not 1 <= days <= 180 or not 1 <= compare_days <= 180:
            raise WhoopError("invalid_trend_window", "use_recovery_or_sleep_and_1_to_180_days")
        summary = self.summary(days=days + compare_days, end=end, refresh=refresh)
        if not summary["success"]:
            return summary
        rows = summary["data"]["days"]
        before, current = rows[:compare_days], rows[compare_days:]
        fields = ("recovery_score", "hrv_rmssd_milli", "resting_heart_rate") if kind == "recovery" else ("sleep_hours", "sleep_performance_percentage", "sleep_efficiency_percentage")
        metrics = {}
        for field in fields:
            def average(group):
                values = [number(row.get(kind, {}).get(field)) for row in group]
                values = [v for v in values if v is not None]
                return (round(sum(values) / len(values), 3) if values else None), len(values)
            old, old_n = average(before)
            new, new_n = average(current)
            metrics[field] = {"current": new, "previous": old, "delta": round(new - old, 3) if new is not None and old is not None else None, "current_samples": new_n, "previous_samples": old_n}
        return result({"kind": kind, "days": days, "compare_days": compare_days, "metrics": metrics}, fetched_at=summary["fetched_at"], stale=summary["stale"])
