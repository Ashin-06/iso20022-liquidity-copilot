# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org/).

## 1.0.0 — 2026-10-27

### Added
- `pyproject.toml` with a `liqcopilot` console command, and pinned `requirements.lock` /
  `requirements-dev.lock` (generated with `uv pip compile --universal`).
- Container image: slim Python 3.12 base, non-root user, health check on `/healthz`.
- CI on every push: ruff, bandit, pip-audit, pytest with a coverage report.
- Settings from `LIQCOPILOT_*` environment variables, checked at startup.
  `LIQCOPILOT_ENV=production` refuses to start without hashed API keys and a database.
- Per-client API keys with roles (`participant`, `analyst`, `operator`, `monitor`, `admin`), stored
  as `sha256:<hex>`; `python -m liqcopilot.ops keygen` makes new ones. Replaces `LIQCOPILOT_API_KEY`.
- A durable RTGS queue (SQLite, or Postgres through `LIQCOPILOT_DATABASE_URL`): participant
  positions, queued payments (JSON or pacs.008 / pacs.009) and settlement runs that book final
  balances in the same transaction. `/rtgs/participants`, `/rtgs/payments`, `/rtgs/settle`,
  `/rtgs/runs`; numbered migrations and `python -m liqcopilot migrate`.
- Per-client rate limit, body cap (also for chunked bodies), security headers, CORS only for
  listed origins; JSON logs with request IDs; `/healthz`, `/readyz` and Prometheus `/metrics`.
- `SECURITY.md`, `docs/threat-model.md`, `docs/runbook.md`.

### Changed
- **Money is `Decimal`, never float.** Balances, floors and amounts are exact decimals in the
  three engines, the ISO 20022 model and the stress runs' settlement; the 1e-9 / 1e-7 float
  tolerances are gone. API inputs are `Decimal` fields (NaN, infinity and sub-paisa amounts are
  refused), responses carry exact decimal strings, and the database stores integer paise. The
  stress cost model (rates, haircuts, delay penalties) stays float and enters the engines only
  as exact amounts.
- Incoming XML is parsed with `defusedxml` (the DOCTYPE/ENTITY check stays as a first layer).
- `/health` is replaced by `/healthz` and `/readyz`; 400s for unknown scenarios or engines no
  longer echo the submitted value.

### Unchanged
- The engines' logic and every benchmark and stress number in the README: the 8-scenario
  benchmark and all four stress scenarios are identical field by field before and after the
  move to `Decimal`.
