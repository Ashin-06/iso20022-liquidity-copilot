"""
FastAPI REST API for ISO 20022 RTGS Liquidity Copilot (port 8013).

Endpoints:
- GET  /                 -> Interactive HTML Web Console (`web/console.html`)
- GET  /healthz, /readyz -> Liveness and readiness; GET /metrics -> Prometheus [metrics]
- POST /iso20022/parse   -> Parse & validate pacs.008 / pacs.009 / camt.054 XML [simulate]
- POST /iso20022/generate-> Generate standards-compliant ISO 20022 XML [simulate]
- POST /settle           -> What-if: FIFO Gross, Bilateral Offset, and Multilateral LSM on a given network [simulate]
- POST /stress           -> Run Basel III BCBS 248 intraday liquidity stress simulation [simulate]
- GET  /bench            -> Run 8-scenario RTGS gridlock & stress benchmark suite [simulate]
- PUT  /rtgs/participants -> Set participants' balances and reserve floors [positions]
- GET  /rtgs/participants -> Current positions [read]
- POST /rtgs/payments    -> Queue payments (JSON, or one pacs.008 / pacs.009 message) [submit]
- GET  /rtgs/payments    -> The queue, by status [read]
- POST /rtgs/settle      -> Settle the queue with one engine and book the result [settle]
- GET  /rtgs/runs        -> Past settlement runs [read]

Amounts are crores, sent as JSON numbers or decimal strings and handled as Decimal; the
queue stores integer paise. Callers send an API key (X-API-Key or Authorization: Bearer)
whose role decides the permissions in brackets; see ops.py and docs/runbook.md.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db, ops
from .bench import get_benchmark_scenarios, run_all_benchmarks
from .gridlock import (
    BankNode,
    QueuedPayment,
    compare_settlement_engines,
    settle_bilateral,
    settle_fifo,
    settle_multilateral_lsm,
)
from .iso20022 import (
    ISO20022Payment,
    ISO20022ValidationError,
    generate_iso20022_xml,
    parse_iso20022_xml,
)
from .store import DuplicatePayment, RTGSStore
from .stress import simulate_bcbs248_stress

settings = ops.load_settings()
ops.setup_logging(settings.log_level)
store = RTGSStore(db.connect(settings.database_url))

app = FastAPI(
    title="ISO 20022 RTGS Liquidity Copilot & Intraday Gridlock Resolver",
    version="1.0.0",
    description=(
        "Wholesale Digital Banking Intraday Liquidity Stress Simulator (BCBS 248) "
        "& ISO 20022 RTGS Multilateral Cycle-Netting LSM Solver."
    ),
)

ops.install(app, settings, {"database": lambda: store.db.connect().close()})

CONSOLE_HTML_PATH = Path(__file__).resolve().parent.parent / "web" / "console.html"
MAX_BANKS = 30
MAX_PAYMENTS = 100  # the bilateral engine is roughly cubic in the queue length
MAX_QUEUE = 100  # payments a settlement run takes on, for the same reason
MAX_AMOUNT = Decimal(10**9)  # crore
# Crores with up to 9 decimals: whole paise. NaN and infinity are refused.
Money = dict(le=MAX_AMOUNT, max_digits=19, decimal_places=9)


class ParseXMLRequest(BaseModel):
    xml: str = Field(..., max_length=200_000, description="Raw ISO 20022 XML document (pacs.008, pacs.009, or camt.054)")


class GenerateXMLRequest(BaseModel):
    msg_type: Literal["pacs.008.001.08", "pacs.009.001.08", "camt.054.001.08"] = "pacs.008.001.08"
    msg_id: str = Field(default="MSG-RTGS-20261022-001", max_length=35)
    end_to_end_id: str = Field(default="E2E-RTGS-20261022-001", max_length=35)
    debtor_agent: str = Field(default="SBININBBXXX", max_length=11)
    creditor_agent: str = Field(default="HDFCINBBXXX", max_length=11)
    amount: Decimal = Field(default=Decimal("250"), gt=0, le=MAX_AMOUNT, max_digits=19, decimal_places=5)
    currency: str = Field(default="INR", max_length=3)
    priority: Literal["URGT", "HIGH", "NORM"] = "URGT"
    settlement_timestamp: str = Field(default="2026-10-22T09:30:00Z", max_length=40)
    credit_debit_indicator: Literal["CRDT", "DBIT"] = "CRDT"
    status: Literal["BOOK", "PDNG", "INFO"] = "BOOK"


class BankInput(BaseModel):
    bic: str = Field(..., max_length=11)
    name: str = Field("Wholesale Bank", max_length=140)
    opening_balance: Decimal = Field(..., ge=0, **Money)
    reserve_floor: Decimal = Field(..., ge=0, **Money)


class PaymentInput(BaseModel):
    payment_id: str = Field(..., min_length=1, max_length=35)
    sender: str = Field(..., max_length=11)
    receiver: str = Field(..., max_length=11)
    amount: Decimal = Field(..., gt=0, **Money)
    priority: Literal["URGT", "HIGH", "NORM"] = "NORM"
    arrival_order: int = Field(0, ge=0, le=10**6)
    timestamp: str = Field("2026-10-22T09:30:00Z", max_length=40)
    msg_type: Literal["pacs.008.001.08", "pacs.009.001.08"] = "pacs.008.001.08"
    currency: str = Field("INR", max_length=3)


class SettleRequest(BaseModel):
    scenario_id: Optional[str] = Field(
        default=None,
        description="Optional benchmark scenario ID (e.g. 'ring_4bank_classic')",
    )
    engine: Optional[str] = Field(
        default="all",
        max_length=20,
        description="'fifo', 'bilateral', 'multilateral_lsm', or 'all'",
    )
    banks: Optional[List[BankInput]] = Field(None, max_length=MAX_BANKS)
    payments: Optional[List[PaymentInput]] = Field(None, max_length=MAX_PAYMENTS)


class StressRequest(BaseModel):
    scenario: str = Field(
        default="normal",
        max_length=40,
        description="One of: 'normal', 'counterparty_default', 'liquidity_hoarding', 'collateral_haircut_shock'",
    )
    banks: Optional[List[BankInput]] = Field(None, max_length=MAX_BANKS)
    payments: Optional[List[PaymentInput]] = Field(None, max_length=MAX_PAYMENTS)


@app.get("/", response_class=HTMLResponse)
def read_console() -> HTMLResponse:
    """Serve the interactive wholesale RTGS Liquidity Copilot console."""
    if CONSOLE_HTML_PATH.exists():
        return HTMLResponse(content=CONSOLE_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<html><body><h1>ISO 20022 RTGS Liquidity Copilot</h1></body></html>"
    )


@app.post("/iso20022/parse", dependencies=[Depends(ops.require("simulate"))])
def api_parse_iso20022(req: ParseXMLRequest) -> Dict[str, Any]:
    try:
        parsed = parse_iso20022_xml(req.xml)
        return {
            "valid": True,
            "payment": parsed.to_dict(),
        }
    except ISO20022ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/iso20022/generate", dependencies=[Depends(ops.require("simulate"))])
def api_generate_iso20022(req: GenerateXMLRequest) -> Dict[str, Any]:
    try:
        payment = ISO20022Payment(
            msg_id=req.msg_id,
            end_to_end_id=req.end_to_end_id,
            debtor_agent=req.debtor_agent,
            creditor_agent=req.creditor_agent,
            amount=req.amount,
            currency=req.currency,
            priority=req.priority,
            settlement_timestamp=req.settlement_timestamp,
            msg_type=req.msg_type,
            credit_debit_indicator=req.credit_debit_indicator,
            status=req.status,
        )
        xml_str = generate_iso20022_xml(payment)
        roundtrip = parse_iso20022_xml(xml_str)
        return {
            "valid": True,
            "msg_type": roundtrip.msg_type,
            "xml": xml_str,
            "payment": roundtrip.to_dict(),
        }
    except ISO20022ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/settle", dependencies=[Depends(ops.require("simulate"))])
def api_settle(req: SettleRequest) -> Dict[str, Any]:
    scenarios = {s["scenario_id"]: s for s in get_benchmark_scenarios()}

    if req.banks and req.payments:
        try:
            banks = [
                BankNode(
                    bic=b.bic,
                    name=b.name,
                    opening_balance=b.opening_balance,
                    reserve_floor=b.reserve_floor,
                )
                for b in req.banks
            ]
            payments = [
                QueuedPayment(
                    payment_id=p.payment_id,
                    sender=p.sender,
                    receiver=p.receiver,
                    amount=p.amount,
                    priority=p.priority,
                    arrival_order=p.arrival_order if p.arrival_order > 0 else idx + 1,
                    timestamp=p.timestamp,
                    msg_type=p.msg_type,
                    currency=p.currency,
                )
                for idx, p in enumerate(req.payments)
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        sc_id = req.scenario_id or "ring_4bank_classic"
        if sc_id not in scenarios:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown scenario_id. Available: {sorted(scenarios.keys())}",
            )
        banks = scenarios[sc_id]["banks"]
        payments = scenarios[sc_id]["payments"]

    engine = (req.engine or "all").strip().lower()
    try:
        if engine == "fifo":
            return settle_fifo(banks, payments).to_dict()
        if engine == "bilateral":
            return settle_bilateral(banks, payments).to_dict()
        if engine in ("multilateral_lsm", "lsm"):
            return settle_multilateral_lsm(banks, payments).to_dict()
        if engine == "all":
            return compare_settlement_engines(banks, payments)
    except ValueError as exc:  # unknown or duplicate banks/payments in a custom network
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raise HTTPException(
        status_code=400,
        detail="Unsupported engine. Expected 'fifo', 'bilateral', 'multilateral_lsm', or 'all'.",
    )


@app.post("/stress", dependencies=[Depends(ops.require("simulate"))])
def api_stress(req: StressRequest) -> Dict[str, Any]:
    try:
        custom_banks = (
            [
                BankNode(b.bic, b.name, b.opening_balance, b.reserve_floor)
                for b in req.banks
            ]
            if req.banks
            else None
        )
        custom_payments = (
            [
                QueuedPayment(
                    p.payment_id,
                    p.sender,
                    p.receiver,
                    p.amount,
                    p.priority,
                    p.arrival_order if p.arrival_order > 0 else idx + 1,
                    p.timestamp,
                    p.msg_type,
                    p.currency,
                )
                for idx, p in enumerate(req.payments)
            ]
            if req.payments
            else None
        )
        report = simulate_bcbs248_stress(
            scenario=req.scenario,
            banks=custom_banks,
            payments=custom_payments,
        )
        return report.to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/bench", dependencies=[Depends(ops.require("simulate"))])
def api_bench() -> Dict[str, Any]:
    return run_all_benchmarks()


class PositionsRequest(BaseModel):
    participants: List[BankInput] = Field(..., min_length=1, max_length=MAX_BANKS)


class EnqueueRequest(BaseModel):
    payments: Optional[List[PaymentInput]] = Field(None, max_length=MAX_PAYMENTS)
    xml: Optional[str] = Field(None, max_length=200_000, description="One pacs.008 or pacs.009 message")


class RTGSSettleRequest(BaseModel):
    engine: Literal["fifo", "bilateral", "multilateral_lsm"] = "multilateral_lsm"


@app.put("/rtgs/participants")
def put_participants(req: PositionsRequest, ident: dict = Depends(ops.require("positions"))) -> Dict[str, Any]:
    try:
        store.set_positions([BankNode(b.bic, b.name, b.opening_balance, b.reserve_floor) for b in req.participants])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    ops.log.info("positions set", extra={"fields": {"by": ident["user"] or "demo", "participants": len(req.participants)}})
    return get_participants()


@app.get("/rtgs/participants", dependencies=[Depends(ops.require("read"))])
def get_participants() -> Dict[str, Any]:
    # exact to the paisa (BankNode.to_dict rounds to two places for the console)
    return {"participants": [{"bic": b.bic, "name": b.name, "opening_balance": format(b.opening_balance, "f"),
                              "reserve_floor": format(b.reserve_floor, "f"),
                              "available_liquidity": format(b.available_liquidity, "f")} for b in store.positions()]}


@app.post("/rtgs/payments")
def enqueue_payments(req: EnqueueRequest, ident: dict = Depends(ops.require("submit"))) -> Dict[str, Any]:
    try:
        if req.xml:
            m = parse_iso20022_xml(req.xml)
            if not m.msg_type.startswith(("pacs.008", "pacs.009")):
                raise HTTPException(400, "only pacs.008 and pacs.009 messages can be queued")
            items = [QueuedPayment(m.end_to_end_id, m.debtor_agent, m.creditor_agent, m.amount, m.priority, 0,
                                   m.settlement_timestamp, m.msg_type, m.currency)]
        elif req.payments:
            items = [QueuedPayment(p.payment_id, p.sender, p.receiver, p.amount, p.priority, 0, p.timestamp, p.msg_type,
                                   p.currency) for p in req.payments]
        else:
            raise HTTPException(400, "send payments or xml")
        if len(store.payments("queued", MAX_QUEUE + 1)) + len(items) > MAX_QUEUE:
            raise HTTPException(429, f"the queue holds at most {MAX_QUEUE} payments; settle it first")
        store.enqueue(ident["user"] or "demo", items)
    except DuplicatePayment as exc:
        raise HTTPException(409, str(exc)) from None
    except (ValueError, ISO20022ValidationError) as exc:
        raise HTTPException(400, str(exc)) from None
    ops.DECISIONS.labels(decision="queued").inc(len(items))
    return {"queued": [p.payment_id for p in items]}


@app.get("/rtgs/payments", dependencies=[Depends(ops.require("read"))])
def list_payments(status: Optional[Literal["queued", "settled"]] = None,
                  limit: int = Query(500, ge=1, le=5000)) -> Dict[str, Any]:
    rows = store.payments(status, limit)
    return {"count": len(rows), "payments": rows}


@app.post("/rtgs/settle")
def settle_queue(req: RTGSSettleRequest, ident: dict = Depends(ops.require("settle"))) -> Dict[str, Any]:
    out = store.settle(ident["user"] or "demo", req.engine)
    ops.DECISIONS.labels(decision="settled").inc(out["settled_count"])
    ops.DECISIONS.labels(decision="left_queued").inc(out["queued_count"] - out["settled_count"])
    return out


@app.get("/rtgs/runs", dependencies=[Depends(ops.require("read"))])
def list_runs(limit: int = Query(50, ge=1, le=1000)) -> Dict[str, Any]:
    return {"runs": store.runs(limit)}
