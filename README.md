# hermes-whoop

WHOOP recovery, sleep, strain, workouts and trends in stock Hermes, with a
private local archive and OAuth that works on a headless host. Python 3.11+;
no runtime dependencies or hosted intermediary. Uses your own WHOOP developer
app and the official v2 API.

## Set up in three steps

1. **Install.** In your Hermes environment:
   ```sh
   hermes plugins install ZK-Snarky/hermes-whoop
   hermes plugins enable hermes-whoop
   ```
   The CLI is also available from the checkout with
   `python -m hermes_whoop`, or after `python -m pip install .` as `hermes-whoop`.
2. **Configure your app.** Create a [WHOOP developer app](https://developer-dashboard.whoop.com/).
   Register `http://127.0.0.1:8787/callback` as its redirect URI and enable all
   six read scopes plus `offline`. Put `WHOOP_CLIENT_ID` and
   `WHOOP_CLIENT_SECRET` in your private shell/service environment or secret
   manager. Never put credentials in this repository. Set `WHOOP_TIMEZONE`
   to your IANA timezone. Run `hermes-whoop setup` for missing configuration.
3. **Connect.** Run `hermes-whoop auth`, open its URL on any device and approve
   access. Copy the complete final URL, even if the browser says it cannot
   connect to localhost. On the host, run `hermes-whoop auth --complete` and
   paste that URL at the hidden prompt. Then run `hermes-whoop doctor --online`.
   In Hermes: “Show my WHOOP weekly summary.”

The same setup works over SSH without a public callback server, inbound port,
browser on the host, or SSH tunnel. Authorization state expires after ten
minutes and can be used only once. The registered redirect must match exactly.
WHOOP's [developer app requirements](https://developer.whoop.com/docs/developing/getting-started/)
apply; an app distributed to other users may need WHOOP approval.

## What it does

| Need | Tool / CLI |
|---|---|
| Discover supported data and onboarding | `whoop_capabilities` / `capabilities` |
| Connection, inventory and cache status | `whoop_status` / `status` or `inventory` |
| Diagnose and fix configuration | `whoop_doctor` / `doctor --online` |
| Current data, guarded against stale observations | `whoop_latest(kind=...)` / `latest recovery` |
| List and get any type | `whoop_list`, `whoop_get` / `list sleep`, `get recovery <cycle-id>` |
| Fetch recent data / recover token state | `whoop_fetch`, `whoop_self_heal` / `fetch`, `self-heal` |
| Historical archive with restart checkpoints | `whoop_backfill` / `backfill --start 2022-01-01` |
| Daily and weekly summaries | `whoop_daily_summary`, `whoop_weekly_summary` / `summary --days 7` |
| Recovery and sleep comparisons | `whoop_trend` / `trend --kind sleep` |
| Disconnect, retaining the archive | `whoop_revoke` / `revoke` |

Types are `recovery`, `sleep`, `cycle`, `workout`, `body`, and `profile`.
Recovery IDs are cycle IDs. WHOOP offers only current body measurements;
body list/get history contains snapshots observed by this connector, with
local IDs. It cannot reconstruct measurements from before installation.

The archive deduplicates by endpoint and record ID, preserves raw JSON, and
records successful fetch windows separately from record timestamps. Backfill
resumes both pages and chunks; repeat until `complete` is true. Empty periods
do not stop the scan. Daily summaries use your configured timezone and exclude
naps from main sleep. Steps belong to the local date of cycle start plus 12
hours. Open-cycle steps older than 90 minutes are omitted from summaries.

Expiring access tokens refresh automatically. A rejected token triggers one
refresh and retry, with rotated tokens persisted atomically under a process
lock. Revoked grants stop with `reauth_required`. Network errors, missing
scores, missing days and scope failures remain distinct. Retry-After and
WHOOP rate-limit reset headers are honored; long waits return `retry_at`
instead of blocking the agent. The next run observes the persisted cooldown.

## Privacy and configuration

Hermes tools and the CLI default to `summary`; the Python service defaults to
`structured`. `structured` preserves nested scores while removing identity
and location fields. `raw` returns complete provider records, including profile
identity. Returned information is visible to the invoking agent and its model.
Output privacy does not redact the local archive: raw health JSON stays on the
host. There is no telemetry and no third-party relay.

State defaults to `$HERMES_HOME/plugin-data/hermes-whoop` (normally
`~/.hermes/plugin-data/hermes-whoop`). `WHOOP_DATA_DIR` overrides it. Keep this
directory outside the plugin checkout and include it in your private backup.
Directories use mode 0700; tokens, OAuth state, raw JSON and SQLite use 0600
on POSIX. On Windows, use an account-private directory with appropriate ACLs.
Do not share one directory between accounts. Uninstalling the plugin does not
revoke provider access or remove the archive; use `revoke` first if disconnecting.

Optional environment: `WHOOP_REDIRECT_URI`, `WHOOP_TIMEZONE`,
`WHOOP_PRIVACY_MODE` (`summary`, `structured`, `raw`). Hermes settings under
`plugins.entries.hermes-whoop.settings` also accept `data_dir`, `timezone`, and
`privacy`. Credentials remain in the environment; refreshed OAuth tokens live
only in the private token file. Fetch on demand or use an existing scheduler
to run `hermes-whoop self-heal`. The plugin does not install background jobs.

## How this relates to existing projects

This builds on ideas from [WHOOP Local Sync](https://github.com/wysie/whoop-local-sync)
and [whoop-mcp](https://github.com/davidmosiah/whoop-mcp), both MIT licensed.
They are useful alternatives. Their source was reviewed before implementation;
the archive/checkpoint design and privacy/summary conventions are adapted here
into one Python service. Their copyright and license notices are included in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

| Capability | WHOOP Local Sync | whoop-mcp | hermes-whoop |
|---|---|---|---|
| Primary integration | Hermes plugin + CLI | TypeScript MCP server | Native Hermes plugin + Python service + CLI |
| Local record archive and historical backfill | SQLite + raw JSON, chunk resume | Local cache and paginated reads | SQLite + raw JSON, page and chunk resume |
| Setup, doctor, privacy modes and trends | Basic auth/status/latest | Yes | Yes |
| Freshness | Latest cache guard | Cache and latest tools | Separate fetch/observation guards and explicit missing-day status |
| Step dates and transient auth recovery | General sync | General API tools | Start + 12h local date; locked token rotation and one 401 recovery |

Comparison reflects source inspected on 2026-10-23, not every future release.
The [upstream notes](docs/upstream.md) record exact revisions and stock Hermes
searches. This is an independent integration, not an official WHOOP product.

For embedding and migrating an existing installation, see [the Python API](docs/API.md).
For local verification: `python -m unittest discover -s tests -v`. Tests use
synthetic HTTP responses and never contact an account. No GitHub workflows are
provided. Real provider authorization and live acceptance remain installation
steps, separate from passing local tests.
