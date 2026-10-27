# Runbook

How to deploy, configure and operate the liquidity copilot. Commands assume the repository root.

## Deploy

```bash
docker build -t iso20022-liquidity-copilot:1.0.0 .
python -m liqcopilot.ops keygen         # one key per bank gateway, operator, analyst and monitor
docker run -d --name liqcopilot -p 8013:8013 -v liqcopilot-data:/data \
  -e LIQCOPILOT_KEYS_FILE=/run/secrets/liqcopilot-keys.json \
  iso20022-liquidity-copilot:1.0.0
curl -fsS http://127.0.0.1:8013/readyz
```

The image runs in production mode as a non-root user with its SQLite database in `/data`. It
refuses to start, and logs why, without keys or a database URL. Terminate TLS in front of it.
**Run a single instance**: settlement runs are serialised per process only. Postgres
(`LIQCOPILOT_DATABASE_URL=postgresql://...`) is supported for durability, not for several
replicas settling at once.

Without Docker: `pip install -r requirements.lock && pip install -e .`, then `liqcopilot serve`.

## Configure

| Variable | Required in production | Meaning |
|---|---|---|
| `LIQCOPILOT_ENV` | — | `development` (default) or `production` |
| `LIQCOPILOT_API_KEYS` / `LIQCOPILOT_KEYS_FILE` | yes | JSON `{"sha256:<hex>": {"user": "...", "role": "..."}}`. Roles: `participant` (queue, read), `analyst` (read, what-ifs), `operator` (queue, read, what-ifs, positions, settle), `monitor` (`/metrics`), `admin` (all) |
| `LIQCOPILOT_DATABASE_URL` | yes | `sqlite:////data/liqcopilot.db` or `postgresql://...` (development default: in memory) |
| `LIQCOPILOT_HOST`, `LIQCOPILOT_PORT` | — | default `127.0.0.1:8013`; a non-loopback address needs API keys |
| `LIQCOPILOT_RATE_LIMIT` | — | requests per minute per client (default 120) |
| `LIQCOPILOT_MAX_BODY_BYTES` | — | default 1,000,000 |
| `LIQCOPILOT_CORS_ORIGINS` | — | origins allowed to call from a browser; none by default |
| `LIQCOPILOT_LOG_LEVEL` | — | default `INFO` |

## Operate a day

1. `PUT /rtgs/participants` with each bank's opening balance and reserve floor (crores, up to 9
   decimals).
2. Gateways `POST /rtgs/payments` through the day.
3. Operators `POST /rtgs/settle` with `multilateral_lsm` (or `fifo` / `bilateral`) as often as
   needed. Unsettled payments stay queued for the next run. `GET /rtgs/runs` lists what each run
   did.

## Watch

- `GET /healthz`: process up. `GET /readyz`: database answers.
- `GET /metrics` (role `monitor`): `http_requests_total`, `http_request_duration_seconds`,
  `errors_total`, `liqcopilot_decisions_total{decision="queued|settled|left_queued"}`.
- Logs: JSON lines with `request_id` (also in `X-Request-ID`); every position change is logged with
  who made it.

Suggested alerts: `/readyz` failing; `left_queued` growing run after run (gridlock the engines can't
clear: liquidity injection needed); the queue near its 100-payment cap; p95 `/rtgs/settle` latency
above 5 s (the exact LSM search is exponential beyond 28 payments, then falls back to greedy).

## Rotate keys

API keys are the only secrets. Add the new hash, restart, hand over the key, remove the old hash,
restart. Remove a hash and restart to revoke at once.

## Back up and restore

SQLite (in the `/data` volume):

```bash
docker exec liqcopilot python -c "import sqlite3; s=sqlite3.connect('/data/liqcopilot.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker cp liqcopilot:/data/backup.db ./liqcopilot-$(date +%F).db
```

Take one before setting positions each morning and after the last settlement run. Restore: stop
the container, put the backup at `/data/liqcopilot.db`, start it, check `/readyz` and compare
`GET /rtgs/participants` with the RTGS's own balances before settling anything. Postgres:
`pg_dump` / `pg_restore`; migrations run on start or with `python -m liqcopilot migrate`.

## Incidents

1. **Balances here disagree with the RTGS.** Stop settling. The RTGS is the record: reset
   positions from it with `PUT /rtgs/participants`, then compare `settlement_runs` with the RTGS
   statement to find the first run that differs.
2. **A run failed half-way.** It can't book half a run: the transaction rolls back and the
   payments stay queued. Check the error log line by its request ID and retry.
3. **Leaked key.** Remove its hash and restart; review payments queued by that user
   (`queued_payments.submitted_by`) and runs it started (`settlement_runs.requested_by`).
