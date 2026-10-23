"""
FastAPI REST API for ISO 20022 RTGS Liquidity Copilot (port 8013).

Endpoints:
- GET  /                 -> Interactive HTML Web Console (`web/console.html`)
- GET  /health           -> Health check & version metadata
- POST /iso20022/parse   -> Parse & validate pacs.008 / pacs.009 / camt.054 XML
- POST /iso20022/generate-> Generate standards-compliant ISO 20022 XML
- POST /settle           -> Run FIFO Gross, Bilateral Offset, and Multilateral LSM
- POST /stress           -> Run Basel III BCBS 248 intraday liquidity stress simulation
- GET  /bench            -> Run 8-scenario RTGS gridlock & stress benchmark suite
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

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
from .stress import simulate_bcbs248_stress

app = FastAPI(
    title="ISO 20022 RTGS Liquidity Copilot & Intraday Gridlock Resolver",
    version="1.0.0",
    description=(
        "Wholesale Digital Banking Intraday Liquidity Stress Simulator (BCBS 248) "
        "& ISO 20022 RTGS Multilateral Cycle-Netting LSM Solver."
    ),
)

CONSOLE_HTML_PATH = Path(__file__).resolve().parent.parent / "web" / "console.html"


class ParseXMLRequest(BaseModel):
    xml: str = Field(..., description="Raw ISO 20022 XML document (pacs.008, pacs.009, or camt.054)")


class GenerateXMLRequest(BaseModel):
    msg_type: str = Field(default="pacs.008.001.08")
    msg_id: str = Field(default="MSG-RTGS-20261022-001")
    end_to_end_id: str = Field(default="E2E-RTGS-20261022-001")
    debtor_agent: str = Field(default="SBININBBXXX")
    creditor_agent: str = Field(default="HDFCINBBXXX")
    amount: float = Field(default=250.0)
    currency: str = Field(default="INR")
    priority: str = Field(default="URGT")
    settlement_timestamp: str = Field(default="2026-10-22T09:30:00Z")
    credit_debit_indicator: str = Field(default="CRDT")
    status: str = Field(default="BOOK")


class BankInput(BaseModel):
    bic: str
    name: str = "Wholesale Bank"
    opening_balance: float
    reserve_floor: float


class PaymentInput(BaseModel):
    payment_id: str
    sender: str
    receiver: str
    amount: float
    priority: str = "NORM"
    arrival_order: int = 0
    timestamp: str = "2026-10-22T09:30:00Z"
    msg_type: str = "pacs.008.001.08"
    currency: str = "INR"


class SettleRequest(BaseModel):
    scenario_id: Optional[str] = Field(
        default=None,
        description="Optional benchmark scenario ID (e.g. 'ring_4bank_classic')",
    )
    engine: Optional[str] = Field(
        default="all",
        description="'fifo', 'bilateral', 'multilateral_lsm', or 'all'",
    )
    banks: Optional[List[BankInput]] = None
    payments: Optional[List[PaymentInput]] = None


class StressRequest(BaseModel):
    scenario: str = Field(
        default="normal",
        description="One of: 'normal', 'counterparty_default', 'liquidity_hoarding', 'collateral_haircut_shock'",
    )
    banks: Optional[List[BankInput]] = None
    payments: Optional[List[PaymentInput]] = None


@app.get("/", response_class=HTMLResponse)
def read_console() -> HTMLResponse:
    """Serve the interactive wholesale RTGS Liquidity Copilot console."""
    if CONSOLE_HTML_PATH.exists():
        return HTMLResponse(content=CONSOLE_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<html><body><h1>ISO 20022 RTGS Liquidity Copilot</h1></body></html>"
    )


@app.get("/health")
def health_check() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "liqcopilot",
        "port": 8013,
        "version": "1.0.0",
    }


@app.post("/iso20022/parse")
def api_parse_iso20022(req: ParseXMLRequest) -> Dict[str, Any]:
    try:
        parsed = parse_iso20022_xml(req.xml)
        return {
            "valid": True,
            "payment": parsed.to_dict(),
        }
    except ISO20022ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/iso20022/generate")
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


@app.post("/settle")
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
                detail=f"Unknown scenario_id '{sc_id}'. Available: {sorted(scenarios.keys())}",
            )
        banks = scenarios[sc_id]["banks"]
        payments = scenarios[sc_id]["payments"]

    engine = (req.engine or "all").strip().lower()
    if engine == "fifo":
        return settle_fifo(banks, payments).to_dict()
    if engine == "bilateral":
        return settle_bilateral(banks, payments).to_dict()
    if engine in ("multilateral_lsm", "lsm"):
        return settle_multilateral_lsm(banks, payments).to_dict()
    if engine == "all":
        return compare_settlement_engines(banks, payments)

    raise HTTPException(
        status_code=400,
        detail=f"Unsupported engine '{req.engine}'. Expected 'fifo', 'bilateral', 'multilateral_lsm', or 'all'.",
    )


@app.post("/stress")
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


@app.get("/bench")
def api_bench() -> Dict[str, Any]:
    return run_all_benchmarks()
