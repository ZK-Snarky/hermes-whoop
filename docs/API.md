# Python API

```python
from hermes_whoop import WhoopService

whoop = WhoopService(
    data_dir="/your/private/whoop",  # caller owns this directory and backup
    timezone="UTC",
    privacy="structured",
)
answer = whoop.get_latest("recovery", max_age_seconds=18 * 3600)
if answer["success"]:
    score = answer["data"]["score"]["recovery_score"]
```

Construction makes no network requests. It creates the private directory and
SQLite schema. `client_id`, `client_secret`, and `redirect_uri` can be supplied
as keyword arguments or through `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`, and
`WHOOP_REDIRECT_URI`. No account-specific credential adapter is embedded.
`opener`, `clock`, and `sleeper` are injectable for offline tests.

Every operation returns a dictionary with:

```json
{
  "success": true,
  "data": {},
  "fetched_at": "2026-01-15T12:00:00Z",
  "stale": false,
  "error": null
}
```

Errors use `{ "code": "...", "recovery": "..." }`, sometimes with safe
details such as `retry_at`, `http_status`, or missing environment variable
names. Token values, callback codes, provider error bodies and owner-facing
notices are never returned. Setup/doctor supply generic operational fix text.
Non-fetch operations and an empty archive can have a null `fetched_at`.

## Methods

- `get_latest(kind, max_age_seconds=64800, refresh_if_stale=True,
  max_record_age_seconds=None, privacy=None, expected_day=None,
  allow_stale=False)`: returns one provider-shaped record in structured/raw
  mode. `cache_stale` and `record_stale` are separate; the default observation
  age limit equals `max_age_seconds` for recovery, sleep and cycle. Workout,
  body and profile observations have no age limit unless explicitly set.
  `expected_day` is the required local `YYYY-MM-DD`. A stale, pending or
  mismatched record is withheld by default. `allow_stale=True` exposes it
  while preserving the unsuccessful status and error. `fetched_at` is when
  that record was downloaded, `record_at` is its observation timestamp (sleep
  end, cycle update, or recovery creation; cycle start if no update exists), and
  `record_id` is the archive key.
- `list_records(kind, start=None, end=None, limit=100, offset=0,
  refresh=False, privacy=None)`: lists archive data, newest first. Filters use
  record start/created timestamps, UTC, inclusive start/exclusive end. Dates
  without an offset are interpreted as UTC. Returns parallel `record_ids`
  and `next_offset`; follow until null. `source="archive"` does not claim
  old observations are current. With `refresh=True`, a failed fetch is
  returned rather than silently falling back to old data.
- `get_record(kind, record_id, refresh=False, privacy=None)`: reads locally,
  fetching from WHOOP when absent or forced. Recovery IDs are cycle IDs;
  profile uses `current`. Body IDs are local snapshot hashes from list/latest;
  `current` fetches a new body snapshot. A historical body ID can never be
  refreshed from WHOOP.
- `fetch(kinds=None, start=None, end=None, days=7)`: defaults to all six types
  and the last seven days. Returns counts and `endpoint_errors`; a partial
  fetch has `success=False`. Pages commit individually but a successful
  collection marker is written only after all pages finish. Singleton body
  and profile endpoints always return current state, regardless of range.
- `backfill(start, end=None, chunk_days=30, max_chunks=12, resume=True)`:
  scans the four historical collections backwards. Returns `complete`,
  `chunks`, `cursor_end`. Repeat identical arguments to resume. No empty-gap
  shortcut. No destructive reset. Body/profile history cannot be backfilled.
- `refresh(force=False)`: rotates expiring tokens; returns safe auth state.
- `self_heal()`: fetches all types through the same automatic refresh path.
- `status()`: connection, inventory counts, per-type successful windows and
  endpoint errors, scopes, expiry, and retry cooldown; no record contents.
- `summary(days=1, end=None, refresh=True)`: local calendar-day observations;
  `end` is the last included date, defaults to today. Seven days gives a
  weekly summary. Missing records remain absent. Open cycle step counts
  older than 90 minutes are withheld; completed cycle steps are final.
- `trend(kind="recovery", days=7, compare_days=7, end=None, refresh=True)`:
  adjacent-window averages, sample counts and deltas. No inferred zeros.
- `begin_auth()`, `complete_auth(callback_url)`: headless OAuth authorization.
- `doctor(online=False)`, `capabilities()`: setup and agent self-discovery.
- `revoke()`: requests provider revocation, then clears tokens and pending
  OAuth state only on success. Keeps local health data.

Canonical kinds: `recovery`, `sleep`, `cycle`, `workout`, `body`, `profile`.
Plural aliases and `body_measurement` / `body_measurements` also work.

## Existing token migration

The caller captures legacy credentials privately and calls `import_tokens`
in-process once. Do not put values in command arguments, tool output or logs.

Accepted dictionary keys: `access_token`, `refresh_token`, `expires_at` (epoch
seconds or ISO 8601) or `expires_in` (seconds), and optional `scope` (space
separated). A refresh token alone is enough; an omitted expiry forces a refresh
on first use. The caller maps any old environment names to these keys. Existing
stored refresh tokens are never overwritten by migration: the method returns
`tokens_already_present`. Migration does not itself contact WHOOP.

After import, this service owns rotation in `tokens.json`. Stop the old refresh
writer before first use so two independent token stores cannot race. Preserve
the old store privately for rollback; do not re-import its stale rotating token.
Give the process access to its developer client ID/secret through the existing
secret mechanism. If the old grant lacks `read:body_measurement`, that endpoint
returns `scope_missing` until the owner grants the full scope set. Other
endpoints remain individually reported. Restarting the process rereads the
same canonical token file.

## Error handling for an embedding application

| Code | Meaning / recovery |
|---|---|
| `auth_required`, `reauth_required`, `offline_token_missing` | One owner authorization; do not replay an invalid grant |
| `configuration_missing`, `configuration_error` | Fix client configuration; not a network retry |
| `scope_missing` | Consent to the missing read scope; other endpoints may work |
| `rate_limited`, `provider_unavailable` | Respect `retry_at` across runs |
| `network_error` | Check network; keep existing tokens |
| `stale_cache` | Fetch has not succeeded recently enough |
| `stale_record` | Download succeeded, latest observation is old |
| `no_data`, `no_current_day_record` | No suitable observation; not an auth failure |
| `score_pending`, `unscorable` | Provider has no usable score yet |
| `partial_fetch` | Inspect `data.endpoint_errors` for each endpoint |
| `pagination_loop`, `pagination_limit` | Incomplete collection; no fresh marker |

The embedding application owns notifications, user wording, scheduling and
its own derived metrics. It can use `privacy="raw"` internally for exact
provider-field compatibility; the data must not then leak through user-facing
status tools or logs.
