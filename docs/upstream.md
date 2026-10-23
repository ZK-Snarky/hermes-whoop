# Upstream research

Checked 2026-10-23 before implementation:

- [Hermes main](https://github.com/NousResearch/hermes-agent/tree/e04614af7bb4e982b76e5256f5ec207198e0641e)
  and newest release [v0.22.5](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.5),
  published 2026-10-23. No stock WHOOP connector was found.
- Open and closed issues/PRs searched for `WHOOP`. Open
  [#30198](https://github.com/NousResearch/hermes-agent/pull/30198) proposes a
  WHOOP skill and OAuth scripts; it was unmerged. Closed
  [#48044](https://github.com/NousResearch/hermes-agent/pull/48044) proposed
  native WHOOP tools; it was not merged. Neither supplied this package's
  combined private archive, page checkpoints, service boundary and freshness
  contract. The user requested a separate competing standalone plugin.
- [Hermes plugin catalog](https://github.com/NousResearch/hermes-agent/tree/main/plugin-catalog):
  no direct WHOOP connector entry. The `athletedata` entry advertises WHOOP
  among many services through its paid hosted intermediary. This package
  instead connects directly using the user's WHOOP developer app.
- [WHOOP Local Sync](https://github.com/wysie/whoop-local-sync/tree/e0ed827222bd57cd9f22319ee932c4cc92ee3115):
  reviewed plugin registration, OAuth, retry, SQLite/raw storage, stale cache,
  and backfill. Adapted its endpoint/ID archive and checkpoint approach.
- [whoop-mcp](https://github.com/davidmosiah/whoop-mcp/tree/7db44db7a80c7347cf9edcc68abdf645a1662e82):
  reviewed setup/auth, token store, privacy, summaries, capabilities and
  revoke support. Adapted its separation of output privacy modes, summary
  measurements, diagnostic fixes and self-discovery.
- Existing first-party WHOOP collection, reauthorization and self-heal code
  supplied the bounded retry/Retry-After behavior, eight-character OAuth
  state, terminal-grant handling, freshness checks and step-day semantics.
  Personal configuration, database coupling and notification wording were
  removed from the standalone implementation.

The package uses stock `plugin.yaml`, `register(ctx)`, tool registration,
profile-aware `plugin_data_dir` and bundled skill registration. It requires
no changes or patches to Hermes. The service can also run independently of
Hermes; its WHOOP-specific OAuth flow handles the required state format and
one private rotating token store shared by tools, CLI and Python consumers.

Provider contracts: [API v2](https://developer.whoop.com/api/),
[OAuth](https://developer.whoop.com/docs/developing/oauth/),
[pagination](https://developer.whoop.com/docs/developing/pagination/),
[rate limits](https://developer.whoop.com/docs/developing/rate-limiting/).

No upstream issue, comment, PR or catalog submission was posted. The draft
catalog entry in this repository is for a later approved submission and must
be pinned to the verified public release commit first.
