"""The RTGS queue in the database (see db.py): participant positions, queued payments and
settlement runs. Amounts are integer paise in storage and Decimal crores everywhere else.

A settlement run reads the queue and the positions, runs one engine, and in the same
transaction marks what settled and books the engine's final balances, so a crash can't
leave a payment settled without its balance moved (or the reverse)."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .gridlock import BankNode, QueuedPayment, settle_bilateral, settle_fifo, settle_multilateral_lsm

PAISE_PER_CRORE = 10**9
ENGINES = {"fifo": settle_fifo, "bilateral": settle_bilateral, "multilateral_lsm": settle_multilateral_lsm}


class DuplicatePayment(ValueError):
    pass


def to_paise(crore: Decimal) -> int:
    paise = Decimal(crore) * PAISE_PER_CRORE  # sum() of nothing is int 0
    if paise != paise.to_integral_value():
        raise ValueError("amounts may not have fractions of a paisa (at most 9 decimal places in crores)")
    return int(paise)


def to_crore(paise: int) -> Decimal:
    return Decimal(paise) / PAISE_PER_CRORE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class RTGSStore:
    def __init__(self, engine: Engine):
        self.db = engine
        self._lock = threading.Lock()  # ponytail: per process; a second replica could settle the same queue

    # --- positions -------------------------------------------------------------------
    def set_positions(self, banks: list[BankNode]) -> None:
        with self._lock, self.db.begin() as c:
            for b in banks:
                args = {"b": b.bic, "n": b.name, "bal": to_paise(b.opening_balance), "f": to_paise(b.reserve_floor), "t": _now()}
                if c.execute(text("UPDATE participants SET name = :n, balance_paise = :bal, reserve_floor_paise = :f, "
                                  "updated_at = :t WHERE bic = :b"), args).rowcount == 0:
                    c.execute(text("INSERT INTO participants VALUES (:b, :n, :bal, :f, :t)"), args)

    def positions(self) -> list[BankNode]:
        with self.db.connect() as c:
            rows = c.execute(text("SELECT * FROM participants ORDER BY bic")).all()
        return [BankNode(r.bic, r.name, to_crore(r.balance_paise), to_crore(r.reserve_floor_paise)) for r in rows]

    # --- queue -----------------------------------------------------------------------
    def enqueue(self, user: str, payments: list[QueuedPayment]) -> None:
        known = {b.bic for b in self.positions()}
        for p in payments:
            if p.sender not in known or p.receiver not in known:
                raise ValueError(f"payment {p.payment_id}: sender and receiver must be participants")
        try:
            with self._lock, self.db.begin() as c:
                start = c.execute(text("SELECT COALESCE(MAX(arrival_order), 0) FROM queued_payments")).scalar()
                for i, p in enumerate(payments, 1):
                    c.execute(text("INSERT INTO queued_payments VALUES (:id, :s, :r, :a, :pr, :o, :ts, :mt, :cc, 'queued', "
                                   ":u, :t, NULL)"),
                              {"id": p.payment_id, "s": p.sender, "r": p.receiver, "a": to_paise(p.amount), "pr": p.priority,
                               "o": start + i, "ts": p.timestamp, "mt": p.msg_type, "cc": p.currency, "u": user, "t": _now()})
        except IntegrityError:
            raise DuplicatePayment("a payment with one of these IDs is already in the queue or settled") from None

    def payments(self, status: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM queued_payments", {"n": limit}
        if status:
            sql, args["st"] = sql + " WHERE status = :st", status
        with self.db.connect() as c:
            rows = c.execute(text(sql + " ORDER BY arrival_order LIMIT :n"), args).mappings().all()
        return [{**{k: v for k, v in r.items() if k != "amount_paise"}, "amount": format(to_crore(r["amount_paise"]), "f")}
                for r in rows]

    def _queued(self, c) -> list[QueuedPayment]:
        rows = c.execute(text("SELECT * FROM queued_payments WHERE status = 'queued' ORDER BY arrival_order")).all()
        return [QueuedPayment(r.payment_id, r.sender, r.receiver, to_crore(r.amount_paise), r.priority, r.arrival_order,
                              r.value_time, r.msg_type, r.currency) for r in rows]

    # --- settlement ------------------------------------------------------------------
    def settle(self, user: str, engine: str) -> dict:
        run_id = uuid.uuid4().hex
        with self._lock, self.db.begin() as c:
            banks = [BankNode(r.bic, r.name, to_crore(r.balance_paise), to_crore(r.reserve_floor_paise))
                     for r in c.execute(text("SELECT * FROM participants ORDER BY bic"))]
            queued = self._queued(c)
            if not queued:
                return {"run_id": None, "queued_count": 0, "settled_count": 0}
            res = ENGINES[engine](banks, queued)
            for p in res.settled_payments:
                c.execute(text("UPDATE queued_payments SET status = 'settled', settled_run = :r WHERE payment_id = :p"),
                          {"r": run_id, "p": p.payment_id})
            for bic, bal in res.final_balances.items():
                c.execute(text("UPDATE participants SET balance_paise = :b, updated_at = :t WHERE bic = :i"),
                          {"b": to_paise(bal), "t": _now(), "i": bic})
            report = res.to_dict()
            c.execute(text("INSERT INTO settlement_runs VALUES (:i, :t, :u, :e, :q, :s, :v, :r)"),
                      {"i": run_id, "t": _now(), "u": user, "e": engine, "q": len(queued), "s": res.settled_count,
                       "v": to_paise(res.settled_volume), "r": json.dumps(report, default=str)})
        return {"run_id": run_id, "queued_count": len(queued), "settled_count": res.settled_count, "report": report}

    def runs(self, limit: int = 50) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute(text("SELECT id, created_at, requested_by, engine, queued_count, settled_count, settled_paise "
                                  "FROM settlement_runs ORDER BY created_at DESC LIMIT :n"), {"n": limit}).mappings().all()
        return [{**{k: v for k, v in r.items() if k != "settled_paise"}, "settled_volume": format(to_crore(r["settled_paise"]), "f")}
                for r in rows]
