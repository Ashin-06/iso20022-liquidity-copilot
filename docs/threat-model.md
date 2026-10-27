# Threat model

What the liquidity copilot protects, from whom, and what is still open. Written for the 1.0.0
code; nobody independent has reviewed it.

## What it protects

| Asset | Where it lives |
|---|---|
| Participant positions (balances, reserve floors) | `participants` table, integer paise |
| Queued and settled payments | `queued_payments` table, integer paise |
| Settlement runs and their reports | `settlement_runs` table |
| API keys | callers hold the key; the service holds only SHA-256 hashes |

The failures that matter: a payment settled twice or without its balances moving, balances
booked that don't match the settled payments, and a bank's queue or position visible to another
bank.

## Trust boundaries

```
bank gateways (participant) ──► liqcopilot API ──► database (positions, queue, runs)
RTGS operations (operator)  ──►       ▲
treasury desk (analyst)     ──►       └── what-if endpoints: nothing stored
```

1. **Participants → API.** A participant key can queue payments and read; it can't set positions
   or settle.
2. **Operators → API.** Setting positions and running settlement need `operator` or `admin`.
3. **API → database.** A settlement run reads queue and positions, then marks settled payments,
   books final balances and records the run in one transaction.

## Threats and what stops them

| Threat | Mitigation | Residual risk |
|---|---|---|
| A payment settles twice | Payment ID is the primary key; settled payments leave the queue in the same transaction that books balances | Two replicas settling at once could both read the same queue before either commits: run one instance (see README) |
| Balances drift from payments | Engines work in exact `Decimal`; storage is integer paise; sub-paisa and non-finite amounts refused | — |
| A bank queues payments for another bank | — | **Not addressed.** Any `participant` key can queue a payment from any sender. Binding keys to a BIC is needed before real use. |
| A bank reads other banks' positions or payments | — | **Not addressed.** `read` shows the whole system. Per-BIC scoping is needed for a multi-bank deployment. |
| Malicious XML | DOCTYPE/ENTITY refused, then `defusedxml` parsing; 200 KB cap | XML isn't schema-validated |
| Flooding or CPU exhaustion (the engines are super-linear) | Body cap, per-client rate limit, queue capped at 100 payments, custom networks capped at 30 banks / 100 payments | Rate limit is per process |
| Error messages leak data | Validation errors don't echo input; unhandled errors return only a request ID | 400s from the engines name payment IDs and BICs |
| Stolen API key | Per-client keys, revocable by removing the hash and restarting | An `operator` key can overwrite positions until revoked |

## Not covered

- Real RTGS, SFMS or ISO 20022 network connectivity, message signing (PKI) and schema validation.
- Intraday credit, collateral and end-of-day processes.
- TLS (terminate it at the ingress).
