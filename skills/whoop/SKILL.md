---
name: whoop
description: Connect WHOOP and read recovery, sleep, activity and trends.
---

Use when the user asks about their WHOOP recovery, sleep, strain, workouts,
steps, body measurements, or connection. Start with `whoop_capabilities` when
the available data or setup is unclear.

For setup, run `whoop_doctor`, apply its exact fixes, then use `whoop_auth_url`.
The user opens the authorization link on any device. A loopback redirect can
show a connection error on that device; the complete URL still contains the
code and state. Prefer `hermes-whoop auth --complete` in a private terminal to
paste it without shell history. `whoop_callback` also accepts the complete URL.
Never request the user's WHOOP password, print credentials, or put secrets in
chat, files in the source checkout, or commands that become shell history.

Use `whoop_latest(kind=...)` for current data. Inspect `success`, `stale`,
`cache_stale`, `record_stale`, and `error.code` before describing a result.
`no_data`, `no_current_day_record`, `stale_record`, `score_pending`, and
`unscorable` describe data availability, not revoked access. Do not trigger
reauthorization for them. Use `expected_day` when the user means today.

Use `whoop_daily_summary`, `whoop_weekly_summary`, or `whoop_trend` for compact
comparisons. Missing samples are not zeros. These are measurements and trends,
not diagnoses. Naps are excluded from main sleep summaries. Step dates use
the local date of cycle start plus 12 hours; open-cycle counts older than 90
minutes are omitted from summaries.

Use `whoop_list` and `whoop_get` for history. Follow `next_offset` for more
local records. Recovery IDs are cycle IDs. WHOOP exposes only current body
measurements; body history consists of local snapshots with IDs returned in
`record_ids`. A profile's ID is `current`.

Backfill needs an explicit start date. Repeat the same `whoop_backfill`
arguments until `complete` is true. Its page and chunk checkpoints survive
restarts; gaps never cause early termination. Respect `retry_at` instead of
repeatedly calling a rate-limited provider.

Default to summary privacy. Structured removes personal identifiers while
retaining nested scores. Raw includes full provider records and requires a
reason from the user's request. Privacy affects returned data; the protected
local archive retains raw JSON. These tools do not send data to an additional
service. The invoking agent sees whatever the tool returns.

Use `whoop_revoke` only for an explicit request to disconnect WHOOP. It revokes
provider access and clears local tokens while retaining the user's archive.
