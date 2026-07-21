# Context intelligence

Context is evidence about volume, never permission to ignore behavior. The
context plane returns immutable `ContextWindow` records with an explicit source,
honesty label, trust score, validity interval, and only the signals the event is
allowed to explain.

## Sources

The operator calendar in `config/event-calendar.yml` is always available.
Enabled entries are active on the half-open interval `[valid_from, valid_to)`;
disabled examples never enter runtime. Overlapping entries remain separate
records so decomposition can attribute and compose them explicitly.

The optional real connector uses football-data.org API v4. It requests
`/v4/competitions/{code}/matches` with bounded `dateFrom`/`dateTo` filters and
the `X-Auth-Token` header, maps official fixture IDs/kickoff/team/competition
fields, and marks those windows `REAL`. Competition codes, lead/duration,
expected per-signal deltas and trust are versioned YAML rather than embedded in
the connector. The provider's v4 match resource and filters are documented at
<https://docs.football-data.org/general/v4/match.html>.

The connector is disabled in committed configuration. To opt in, set
`sports_connector.enabled: true` and provide `FOOTBALL_DATA_API_KEY` through the
environment. The token is a `SecretStr`, is sent only as a header, and is never
part of cache keys or logs. With no token, an HTTP/schema error, or a cancelled
fixture, the source returns no window and the operator calendar continues
normally. Successful and failed date/competition queries are cached for the
process lifetime to stay within the free API rate limit; a restart deliberately
refreshes the snapshot.

## Determinism

Every lookup receives an explicit timezone-aware UTC `as_of`. Neither calendar
selection nor connector filtering reads wall time. The external response plus
the versioned YAML are capture inputs; Phase 1.9 records connector snapshots so
a replay never calls the live API.

Application code should create the plane with `open_context_service`, which
owns an `httpx` client using the configured HTTPS base URL and a timeout capped
at 30 seconds. Tests use the lower-level factory with a mock transport; no test
requires a key or the public network.
