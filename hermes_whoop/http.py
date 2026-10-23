"""Bounded WHOOP transport. Error bodies and credentials never escape this layer."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from .common import WhoopError, iso

API_URL = "https://api.prod.whoop.com/developer/v2"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(headers, attempt, now):
    for name in ("Retry-After", "X-RateLimit-Reset"):
        raw = headers.get(name) if headers else None
        if raw is None:
            continue
        try:
            delay = float(raw)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(raw).timestamp() - now
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay):
            return max(0, delay)
    return min(2 ** attempt, 30)


class HTTP:
    def __init__(self, opener=None, clock=time.time, sleeper=time.sleep):
        self.opener = opener or urllib.request.build_opener(NoRedirect).open
        self.clock, self.sleeper = clock, sleeper

    def request(self, url, *, method="GET", token=None, data=None):
        headers = {"Accept": "application/json", "User-Agent": "hermes-whoop/0.1.0"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(data).encode()
        for attempt in range(3):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with self.opener(request, timeout=30) as response:
                    raw = response.read()
                    payload = json.loads(raw) if raw else {}
                    if not isinstance(payload, dict):
                        raise WhoopError("invalid_provider_response", "retry")
                    return payload
            except urllib.error.HTTPError as exc:
                provider_code = ""
                try:
                    if url == TOKEN_URL and exc.code in (400, 401):
                        try:
                            provider_code = json.loads(exc.read()).get("error", "")
                        except (ValueError, AttributeError):
                            pass
                finally:
                    exc.close()
                if exc.code == 429 or 500 <= exc.code < 600:
                    delay = retry_delay(exc.headers, attempt, self.clock())
                    if delay > 30 or attempt == 2:
                        raise WhoopError("rate_limited" if exc.code == 429 else "provider_unavailable", "retry_after", retry_at=iso(self.clock() + delay), http_status=exc.code) from None
                    self.sleeper(delay)
                    continue
                if url == TOKEN_URL and exc.code in (400, 401):
                    if provider_code in ("invalid_client", "invalid_scope", "invalid_request", "unsupported_grant_type"):
                        raise WhoopError("configuration_error", "fix_client_configuration", http_status=exc.code) from None
                    raise WhoopError("reauth_required", "authorize", http_status=exc.code) from None
                labels = {401: ("unauthorized", "refresh"), 403: ("scope_missing", "authorize_required_scopes"), 404: ("not_found", "check_record_id")}
                code, recovery = labels.get(exc.code, ("provider_http_error", "check_configuration"))
                raise WhoopError(code, recovery, http_status=exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt == 2:
                    raise WhoopError("network_error", "check_network") from None
                self.sleeper(2 ** attempt)
            except (ValueError, UnicodeError):
                raise WhoopError("invalid_provider_response", "retry") from None
