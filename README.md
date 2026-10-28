# ISO 20022 RTGS Liquidity Copilot

A simulator for a real-time gross settlement (RTGS) system, where high-value payments between banks settle one by one. When every bank waits for incoming money before paying out, the queue gets stuck (gridlock) and banks have to pledge expensive collateral. This project compares ways of clearing a stuck queue, runs intraday liquidity stress tests based on the BCBS 248 monitoring tools, and reads and writes the ISO 20022 messages used for these payments.

All scenarios are hand-built. Transaction-level RTGS data is not publicly available.

## Features

- Generates and parses pacs.008, pacs.009 and camt.054 messages, with SWIFT BICs or Indian IFSCs.
- Three settlement methods: one at a time in order (FIFO), netting between pairs of banks, and multilateral netting that finds the largest set of payments that can settle together without any bank going below its reserve floor.
- Stress scenarios: a normal day, a bank that stops paying, banks hoarding liquidity, and a 35% repo haircut.
- A stored payment queue: register bank positions, queue payments (as JSON or ISO 20022 messages) and settle them in one transaction.
- Money is handled as exact decimals and stored as integer paise.

How each method works is described in [docs/design.md](docs/design.md).

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                                  # 30 tests
python -m liqcopilot bench                           # benchmark below
python -m liqcopilot stress --scenario counterparty_default
python -m liqcopilot serve                           # API and console on http://127.0.0.1:8013
```

To build the container image: `docker build -t iso20022-liquidity-copilot .` (see [docs/runbook.md](docs/runbook.md)).

## API

What-if endpoints, which store nothing:

| Endpoint | Purpose |
|---|---|
| `POST /iso20022/parse`, `/iso20022/generate` | read or write a message |
| `POST /settle` | run a settlement method on a benchmark scenario or your own banks and payments |
| `POST /stress` | run a stress scenario |
| `GET /bench` | the benchmark |

The stored queue:

| Endpoint | Purpose |
|---|---|
| `PUT /rtgs/participants`, `GET /rtgs/participants` | set and read each bank's balance and reserve floor |
| `POST /rtgs/payments` | queue payments as JSON or one pacs.008 / pacs.009 message |
| `POST /rtgs/settle` | settle the queue with one method and book the final balances |
| `GET /rtgs/payments`, `/rtgs/runs` | queue state and past settlement runs |

API keys carry a role: `participant` queues payments, `analyst` runs what-ifs, `operator` also sets positions and settles, `monitor` reads metrics. Create keys with `python -m liqcopilot.ops keygen`.

## Results

Eight hand-built gridlock scenarios, ₹14,235 crore queued in total:

| Method | Share cleared |
|---|---|
| FIFO | 5.3% |
| Bilateral netting | 9.2% |
| Multilateral netting | 98.7% |

Multilateral netting needed ₹138 crore of liquidity, against ₹4,610 crore to settle the same payments one by one. No bank went below its floor, and the result was proven optimal in all eight scenarios. The scenarios were built as textbook gridlocks, where netting wins by design; on real traffic the difference would be much smaller. Per-scenario and stress results are in [docs/design.md](docs/design.md).

## Configuration

| Variable | Purpose |
|---|---|
| `LIQCOPILOT_ENV=production` | refuse to start without hashed API keys and a database |
| `LIQCOPILOT_API_KEYS` / `LIQCOPILOT_KEYS_FILE` | hashed API keys with roles |
| `LIQCOPILOT_DATABASE_URL` | `sqlite:///...` or `postgresql://...` |

## Limitations

- It is a simulation with no connection to a real RTGS, the SFMS/ISO 20022 network or any bank system.
- Messages are not validated against the official XSDs and cover only a small subset of fields.
- The benchmark scenarios favour netting by design.
- Optimality is only proven for queues of up to 28 payments; larger queues get a greedy answer, and the record says so.
- The stored queue has no intraday credit, cancellation or end-of-day processing, and two replicas must not settle the same queue at once.
- The code has not had an independent security review.

See [SECURITY.md](SECURITY.md) and [docs/threat-model.md](docs/threat-model.md).

## License

MIT. See [LICENSE](LICENSE).
