# ISO 20022 RTGS Liquidity Copilot (`liqcopilot`)

A simulator for an RTGS (real-time gross settlement) system that queues high-value payments between banks. It compares three ways of settling a gridlocked queue and runs intraday liquidity stress scenarios loosely based on the BCBS 248 monitoring tools. It also parses and generates pacs.008, pacs.009 and camt.054 messages.

Everything here runs on hand-built scenarios. No real payment data is involved; transaction-level RTGS data is not publicly available.

## What it does

**ISO 20022 messages** (`iso20022.py`). It generates and parses pacs.008.001.08 (customer credit transfer), pacs.009.001.08 (FI credit transfer) and camt.054.001.08 (debit/credit notification) with `xml.etree`. Agents are SWIFT BICs or Indian IFSCs (IFSC goes in `ClrSysMmbId` with code `INFSC`). The parser checks amounts against the ActiveCurrencyAndAmount format, requires `Ccy`, ISO 8601 timestamps and 35-character IDs, and refuses DTDs and documents over 200k characters. Urgent payments carry `SttlmPrty=URGT` with `InstrPrty=HIGH`, because InstrPrty only allows HIGH and NORM. The element structure follows the published message definitions, but the output **has not been validated against the official XSDs**, and only the fields listed here are read or written.

**Settlement engines** (`gridlock.py`). Each bank has a balance B and a reserve floor F it must not go below.

1. *FIFO gross*: payments settle one at a time in priority and arrival order. The default stalls the whole queue at the first payment the sender can't fund. A per-sender variant (`strict_global_hol=False`) is available.
2. *Bilateral offsetting*: opposite queues between two banks settle together when the net amount is fundable.
3. *Multilateral LSM*: find the largest-value subset of the queue that can settle in one atomic batch without any bank ending below its floor. This is a multi-constraint knapsack problem. The solver prunes greedily from the most-short bank, re-inserts what fits, then runs an exact branch-and-bound for queues of up to 28 payments (15,000-node budget). The batch audit entry records whether the result is proven optimal.

Every engine rejects unknown or duplicate banks, duplicate payment IDs and non-finite amounts.

**Stress scenarios** (`stress.py`). These cover a 6-bank network over 18 payments from 09:15 to 17:30 under four scenarios: normal, one bank stops paying, banks hoard liquidity and delay payments, and a 35% repo haircut. For each it reports:
- start-of-day free reserves plus collateral credit;
- *peak liquidity usage*: the system-wide sum of each bank's drawdown below its opening balance, for the payments the LSM settled. FIFO gross with unlimited intraday credit is compared against the LSM's net batch. This simplifies BCBS 248's per-bank "largest net cumulative outflow".
- hourly cumulative throughput (12:00, 15:00, 18:00);
- the share of urgent payments settled;
- a collateral-versus-delay cost comparison. It picks the cheapest of 10 fixed pledge sizes, using illustrative repo and delay rates that are not calibrated to any market.

## Results

`python -m liqcopilot bench`, eight hand-built gridlock scenarios (₹ crore):

| Scenario | Queued | FIFO cleared | Bilateral | LSM | Gross liquidity for the LSM's set | LSM net liquidity |
|---|---|---|---|---|---|---|
| 4-bank ring | 2,145 | 4.0% | 4.0% | 100% | 700 | 30 |
| 6-bank mesh | 1,970 | 9.4% | 9.4% | 100% | 310 | 20 |
| hub and spokes | 2,058 | 4.3% | 4.3% | 100% | 665 | 18 |
| bilateral + 3/5-bank cycles | 1,500 | 5.3% | 42.3% | 100% | 360 | 15 |
| normal intraday wave | 1,562 | 5.9% | 5.9% | 100% | 600 | 15 |
| counterparty default | 1,700 | 5.3% | 5.3% | 89.4% | 670 | 10 |
| liquidity hoarding | 1,582 | 4.5% | 4.5% | 100% | 625 | 15 |
| 35% haircut | 1,718 | 4.0% | 4.0% | 100% | 680 | 15 |
| **all** | **14,235** | **5.3%** | **9.2%** | **98.7%** | **4,610** | **138** |

No bank ends below its floor under any engine, and the LSM result is proven optimal in all eight.

**Read this as a demonstration of the mechanism, not a measured gain.** Every scenario was built so that each payment is larger than its sender's free buffer while the queue as a whole nearly nets out. That is the textbook gridlock, where netting wins by construction. Even a smarter FIFO that skips blocked payments clears only 4–16% of these queues. On real RTGS traffic, which is mostly not gridlocked, the difference would be far smaller.

Stress run (`python -m liqcopilot stress --scenario <name>`):

| Scenario | Settled by 18:00, FIFO / LSM | Peak usage, gross / LSM | Urgent settled (LSM) |
|---|---|---|---|
| normal | 1.2% / 100% | 725 / 30 | 100% |
| counterparty default | 1.5% / 75.5% | 400 / 65 | 52.7% |
| liquidity hoarding | 1.2% / 100% | 725 / 30 | 100% |
| 35% haircut | 1.2% / 100% | 725 / 30 | 100% |

In three of the four scenarios the LSM clears everything with no extra collateral, so the cost comparison is trivially in its favour. In the default scenario, the cheapest pledge is the largest of the 10 candidates (₹650 Cr), meaning the grid is too coarse to show a real optimum.

## Run it

```bash
pip install -r requirements.txt
python -m pytest -q                                  # 17 tests
python -m liqcopilot bench                           # the table above
python -m liqcopilot stress --scenario counterparty_default
python -m liqcopilot serve --port 8013               # API + console at http://127.0.0.1:8013
```

Endpoints: `POST /iso20022/parse`, `POST /iso20022/generate`, `POST /settle` (a benchmark scenario or your own banks and payments), `POST /stress`, `GET /bench`, `GET /health`.

Set `LIQCOPILOT_API_KEY` to require it in the `X-API-Key` header on the POST endpoints and `/bench`; the console has a field for it. Without the key the server runs open, and `serve` refuses to bind a non-loopback address. Custom networks are capped at 30 banks and 100 payments, and request bodies at 1 MB.

## Limits

- **Simulation only.** There is no connection to an RTGS, a SWIFT/ISO 20022 network, or any bank system. The banks, balances and payments are made up; the BICs are only used as labels.
- **XML is not schema-validated.** The generator and parser cover a small subset of each message and have not been checked against the official XSDs. A real integration would validate against the published schemas and handle the business application header (head.001), UETRs, batches with several transactions, and much more.
- **Benchmark scenarios favour netting by design.** See above. There is no evaluation on realistic traffic.
- **Money is float.** Balances and amounts are Python floats with a 1e-9 tolerance. A ledger would use integer minor units or `Decimal`.
- **LSM optimality is limited.** It is proven only for queues of up to 28 payments within the node budget. Larger queues get a greedy answer, and the audit trail says so.
- **Stress metrics are simplified.** Peak usage is system-wide rather than per bank, and the cost model's rates and the 10 pledge sizes are assumptions.
- **No deployment story**: no TLS, rate limiting, persistence, audit log or authentication beyond a shared key.
