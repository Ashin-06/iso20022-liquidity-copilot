# Design

## ISO 20022 messages

`iso20022.py` generates and parses pacs.008.001.08 (customer credit transfer), pacs.009.001.08 (bank-to-bank credit transfer) and camt.054.001.08 (debit/credit notification). Banks are identified by SWIFT BIC or Indian IFSC; an IFSC goes in `ClrSysMmbId` with the code `INFSC`. The parser checks amounts against the ActiveCurrencyAndAmount format and requires a currency, ISO 8601 timestamps and IDs of at most 35 characters. Incoming XML is parsed with `defusedxml`, and DTDs and documents over 200,000 characters are refused. Urgent payments carry `SttlmPrty=URGT` with `InstrPrty=HIGH`, because InstrPrty only allows HIGH and NORM.

The element structure follows the published message definitions, but the output has not been validated against the official XSDs, and only the fields listed here are read or written.

## Settlement engines

`gridlock.py`. Each bank has a balance and a reserve floor it must not go below.

1. FIFO gross: payments settle one at a time in priority and arrival order. By default the whole queue stops at the first payment its sender can't fund; a per-sender variant (`strict_global_hol=False`) is available.
2. Bilateral offsetting: opposite queues between two banks settle together when the net amount can be funded.
3. Multilateral LSM: find the largest-value set of queued payments that can settle in one atomic batch with no bank ending below its floor. This is a multi-constraint knapsack problem. The solver removes payments greedily from the most-short bank, adds back what fits, and then runs an exact branch-and-bound search for queues of up to 28 payments (15,000-node budget). Each batch records whether its result is proven optimal.

Every engine rejects unknown or duplicate banks, duplicate payment IDs and non-finite amounts.

## Money

Amounts are in crores, accepted as JSON numbers or decimal strings with up to 9 decimal places (whole paise). They are `Decimal` in the engines and the settlement part of the stress runs, integer paise in the database and exact decimal strings in responses. NaN, infinity and fractions of a paisa are refused. The stress cost model (repo rates, haircuts, delay penalties) is an estimate and stays in floating point; it only reaches the engines as exact amounts. Switching to `Decimal` left every benchmark number unchanged.

## Stress scenarios

`stress.py` runs a 6-bank network over 18 payments between 09:15 and 17:30 in four scenarios: normal, one bank stops paying, banks hoard liquidity and delay payments, and a 35% repo haircut. For each it reports:

- start-of-day free reserves plus collateral credit;
- peak liquidity usage, the system-wide sum of each bank's drawdown below its opening balance, comparing FIFO gross (with unlimited intraday credit) against the LSM's netted batch. This simplifies BCBS 248's per-bank "largest net cumulative outflow";
- cumulative throughput at 12:00, 15:00 and 18:00;
- the share of urgent payments settled;
- a collateral-versus-delay cost comparison that picks the cheapest of 10 fixed pledge sizes, using illustrative rates not calibrated to any market.

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
| all | 14,235 | 5.3% | 9.2% | 98.7% | 4,610 | 138 |

No bank ends below its floor under any engine, and the LSM result is proven optimal in all eight. Every scenario was built so that each payment is larger than its sender's free buffer while the queue as a whole nearly nets out. That is the textbook gridlock, where netting wins by construction; on real RTGS traffic, which is mostly not gridlocked, the difference would be much smaller.

Stress runs (`python -m liqcopilot stress --scenario <name>`):

| Scenario | Settled by 18:00, FIFO / LSM | Peak usage, gross / LSM | Urgent settled (LSM) |
|---|---|---|---|
| normal | 1.2% / 100% | 725 / 30 | 100% |
| counterparty default | 1.5% / 75.5% | 400 / 65 | 52.7% |
| liquidity hoarding | 1.2% / 100% | 725 / 30 | 100% |
| 35% haircut | 1.2% / 100% | 725 / 30 | 100% |

In the default scenario the cheapest pledge is the largest of the 10 candidates (₹650 crore), which means the grid of pledge sizes is too coarse to show the real optimum.
