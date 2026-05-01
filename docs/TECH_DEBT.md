# Technical debt register

Backlog of known structural / capability gaps that aren't blocking but
should be paid down. Each row gets a sequential `TD-NNN` ID.

| ID | Description | Date | Priority | Kind | Origin | Target | Status |
|----|-------------|------|----------|------|--------|--------|--------|
| TD-001 | Active sidecar watcher: `embed_service` periodically scans the DB for un-embedded rows and embeds them in the background, eliminating the chain race entirely. Today the sidecar is purely reactive — it only embeds when something POSTs to `/embed`. The chain wait-for-socket fix (commit `<TBD>`) closes the race for the wizard's hot path, but a fully self-driving sidecar would make the entire system robust against any timing or watcher failure (e.g. ingest happening while sidecar was briefly down for maintenance). Design sketch: add a background coroutine in `embed_service.py` that wakes every N seconds, runs `SELECT m.rowid, m.content FROM messages m LEFT JOIN message_vecs v ON v.rowid=m.rowid WHERE v.rowid IS NULL LIMIT 100`, and embeds the batch. Needs care around DB lock contention with watcher-driven ingests. | 2026-05-01 | Medium | missing | deliberate | — | Open |
