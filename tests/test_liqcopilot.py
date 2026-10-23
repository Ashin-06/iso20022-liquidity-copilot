"""
Comprehensive pytest suite for ISO 20022 RTGS Liquidity Copilot (`liqcopilot`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from liqcopilot.api import app
from liqcopilot.bench import (
    format_benchmark_report,
    get_benchmark_scenarios,
    run_all_benchmarks,
)
from liqcopilot.gridlock import (
    BankNode,
    QueuedPayment,
    compare_settlement_engines,
    detect_payment_cycles,
    settle_bilateral,
    settle_fifo,
    settle_multilateral_lsm,
)
from liqcopilot.iso20022 import (
    ISO20022Payment,
    ISO20022ValidationError,
    generate_camt054_xml,
    generate_iso20022_xml,
    generate_pacs008_xml,
    generate_pacs009_xml,
    parse_iso20022_xml,
    validate_bic_or_ifsc,
)
from liqcopilot.stress import (
    optimize_collateral_vs_delay,
    simulate_bcbs248_stress,
)


# ---------------------------------------------------------------------------
# 1. ISO 20022 XML Parser, Validator, and Generator Tests
# ---------------------------------------------------------------------------


def test_pacs008_generate_and_parse_roundtrip_bic() -> None:
    pmt = ISO20022Payment(
        msg_id="MSG-PACS008-001",
        end_to_end_id="E2E-PACS008-001",
        debtor_agent="SBININBBXXX",
        creditor_agent="HDFCINBBXXX",
        amount=425.75,
        currency="INR",
        priority="URGT",
        settlement_timestamp="2026-10-22T09:15:00Z",
        msg_type="pacs.008.001.08",
    )
    xml_doc = generate_pacs008_xml(pmt)
    assert "FIToFICstmrCdtTrf" in xml_doc
    assert "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08" in xml_doc

    parsed = parse_iso20022_xml(xml_doc)
    assert parsed.msg_type == "pacs.008.001.08"
    assert parsed.msg_id == "MSG-PACS008-001"
    assert parsed.end_to_end_id == "E2E-PACS008-001"
    assert parsed.debtor_agent == "SBININBBXXX"
    assert parsed.creditor_agent == "HDFCINBBXXX"
    assert parsed.amount == pytest.approx(425.75)
    assert parsed.currency == "INR"
    assert parsed.priority == "URGT"
    assert parsed.settlement_timestamp == "2026-10-22T09:15:00Z"


def test_pacs009_generate_and_parse_roundtrip_ifsc() -> None:
    pmt = ISO20022Payment(
        msg_id="MSG-PACS009-002",
        end_to_end_id="E2E-PACS009-002",
        debtor_agent="SBIN0000001",
        creditor_agent="ICIC0000002",
        amount=850.00,
        currency="INR",
        priority="HIGH",
        settlement_timestamp="2026-10-22T10:45:00Z",
        msg_type="pacs.009.001.08",
    )
    xml_doc = generate_pacs009_xml(pmt)
    assert "FICdtTrf" in xml_doc
    assert "INFSC" in xml_doc

    parsed = parse_iso20022_xml(xml_doc)
    assert parsed.msg_type == "pacs.009.001.08"
    assert parsed.debtor_agent == "SBIN0000001"
    assert parsed.creditor_agent == "ICIC0000002"
    assert parsed.amount == pytest.approx(850.00)
    assert parsed.priority == "HIGH"


def test_camt054_generate_and_parse_roundtrip() -> None:
    pmt = ISO20022Payment(
        msg_id="MSG-CAMT054-003",
        end_to_end_id="E2E-CAMT054-003",
        debtor_agent="AXISINBBXXX",
        creditor_agent="KKBKINBBXXX",
        amount=310.50,
        currency="INR",
        priority="NORM",
        settlement_timestamp="2026-10-22T14:20:00Z",
        msg_type="camt.054.001.08",
    )
    xml_doc = generate_camt054_xml(pmt, credit_debit="CRDT", status="BOOK")
    assert "BkToCstmrDbtCdtNtfctn" in xml_doc

    parsed = parse_iso20022_xml(xml_doc)
    assert parsed.msg_type == "camt.054.001.08"
    assert parsed.msg_id == "MSG-CAMT054-003"
    assert parsed.end_to_end_id == "E2E-CAMT054-003"
    assert parsed.debtor_agent == "AXISINBBXXX"
    assert parsed.creditor_agent == "KKBKINBBXXX"
    assert parsed.amount == pytest.approx(310.50)
    assert parsed.credit_debit_indicator == "CRDT"
    assert parsed.status == "BOOK"


def test_iso20022_validation_errors() -> None:
    with pytest.raises(ISO20022ValidationError):
        validate_bic_or_ifsc("BAD_CODE")

    with pytest.raises(ISO20022ValidationError):
        generate_iso20022_xml(
            ISO20022Payment(
                msg_id="M1",
                end_to_end_id="E1",
                debtor_agent="SBININBBXXX",
                creditor_agent="HDFCINBBXXX",
                amount=-50.0,
            )
        )

    with pytest.raises(ISO20022ValidationError):
        generate_iso20022_xml(
            ISO20022Payment(
                msg_id="M1",
                end_to_end_id="E1",
                debtor_agent="SBININBBXXX",
                creditor_agent="SBININBBXXX",
                amount=100.0,
            )
        )

    with pytest.raises(ISO20022ValidationError):
        parse_iso20022_xml("<NotADocument><Foo/></NotADocument>")


# ---------------------------------------------------------------------------
# 2. RTGS Gridlock Engines (FIFO, Bilateral, Multilateral LSM)
# ---------------------------------------------------------------------------


def test_cycle_detection_and_multilateral_lsm_breaks_gridlock() -> None:
    banks = [
        BankNode("SBININBBXXX", "SBI", opening_balance=500.0, reserve_floor=400.0),
        BankNode("HDFCINBBXXX", "HDFC", opening_balance=500.0, reserve_floor=400.0),
        BankNode("ICICINBBXXX", "ICICI", opening_balance=500.0, reserve_floor=400.0),
        BankNode("AXISINBBXXX", "Axis", opening_balance=500.0, reserve_floor=400.0),
    ]
    payments = [
        QueuedPayment("P1", "SBININBBXXX", "HDFCINBBXXX", 85.0, "URGT", 1),
        QueuedPayment("P2", "SBININBBXXX", "ICICINBBXXX", 360.0, "URGT", 2),
        QueuedPayment("P3", "HDFCINBBXXX", "ICICINBBXXX", 340.0, "HIGH", 3),
        QueuedPayment("P4", "ICICINBBXXX", "AXISINBBXXX", 690.0, "HIGH", 4),
        QueuedPayment("P5", "AXISINBBXXX", "SBININBBXXX", 425.0, "NORM", 5),
        QueuedPayment("P6", "AXISINBBXXX", "HDFCINBBXXX", 245.0, "NORM", 6),
    ]

    cycles = detect_payment_cycles(payments)
    assert len(cycles) >= 2

    fifo = settle_fifo(banks, payments)
    bilat = settle_bilateral(banks, payments)
    lsm = settle_multilateral_lsm(banks, payments)

    assert fifo.volume_clearing_ratio < 0.20
    assert lsm.volume_clearing_ratio == pytest.approx(1.0)
    assert lsm.peak_liquidity_used < 0.50 * fifo.peak_liquidity_used
    assert fifo.floor_violations == 0
    assert bilat.floor_violations == 0
    assert lsm.floor_violations == 0
    assert len(lsm.camt054_notifications) == len(payments)


def test_bilateral_offset_beats_fifo_on_opposing_obligations() -> None:
    banks = [
        BankNode("SBININBBXXX", "SBI", opening_balance=500.0, reserve_floor=450.0),
        BankNode("HDFCINBBXXX", "HDFC", opening_balance=500.0, reserve_floor=450.0),
    ]
    payments = [
        QueuedPayment("B1", "SBININBBXXX", "HDFCINBBXXX", 320.0, "URGT", 1),
        QueuedPayment("B2", "HDFCINBBXXX", "SBININBBXXX", 300.0, "URGT", 2),
    ]
    fifo = settle_fifo(banks, payments)
    bilat = settle_bilateral(banks, payments)
    assert fifo.settled_count == 0
    assert bilat.settled_count == 2
    assert bilat.settled_volume == pytest.approx(620.0)
    assert bilat.floor_violations == 0


# ---------------------------------------------------------------------------
# 3. Basel III BCBS 248 Intraday Liquidity Stress Simulator Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "counterparty_default",
        "liquidity_hoarding",
        "collateral_haircut_shock",
    ],
)
def test_bcbs248_stress_scenarios(scenario: str) -> None:
    report = simulate_bcbs248_stress(scenario=scenario)
    assert report.scenario == scenario
    assert report.available_intraday_liquidity_sod_cr > 0
    assert report.total_gross_payments_cr > 0
    assert report.settled_by_eod_lsm_pct > report.settled_by_eod_fifo_pct
    assert report.daily_max_liquidity_usage_lsm_cr < report.daily_max_liquidity_usage_fifo_cr
    assert report.floor_violations == 0
    assert "12:00" in report.throughput_lsm_pct
    assert "15:00" in report.throughput_lsm_pct
    assert "18:00" in report.throughput_lsm_pct

    opt = report.collateral_optimization
    assert opt.optimal_lsm_total_cost_lakhs <= opt.unoptimized_zero_pledge_cost_lakhs
    assert opt.optimal_lsm_total_cost_lakhs <= opt.naive_full_prefund_cost_lakhs
    assert len(opt.tradeoff_curve) >= 5


# ---------------------------------------------------------------------------
# 4. 8-Scenario Benchmark Suite Proof Assertions
# ---------------------------------------------------------------------------


def test_benchmark_8_scenarios_proof_targets() -> None:
    scenarios = get_benchmark_scenarios()
    assert len(scenarios) == 8

    bench = run_all_benchmarks()
    summary = bench["summary"]

    assert summary["scenario_count"] == 8
    assert summary["lsm_volume_cleared_pct"] > 85.0
    assert summary["fifo_volume_cleared_pct"] < 20.0
    assert summary["peak_liquidity_reduction_vs_fifo_pct"] > 50.0
    assert summary["peak_liquidity_reduction_vs_gross_pct"] > 50.0
    assert summary["total_floor_violations"] == 0
    assert all(summary["assertions"].values())

    report_str = format_benchmark_report(bench)
    assert "AGGREGATE (8 SCENARIOS)" in report_str


# ---------------------------------------------------------------------------
# 5. FastAPI Endpoints Tests
# ---------------------------------------------------------------------------


def test_fastapi_endpoints() -> None:
    client = TestClient(app)

    r_root = client.get("/")
    assert r_root.status_code == 200
    assert "ISO 20022 RTGS Liquidity Copilot" in r_root.text

    r_gen = client.post(
        "/iso20022/generate",
        json={
            "msg_type": "pacs.008.001.08",
            "msg_id": "API-MSG-01",
            "end_to_end_id": "API-E2E-01",
            "debtor_agent": "SBININBBXXX",
            "creditor_agent": "HDFCINBBXXX",
            "amount": 500.0,
            "currency": "INR",
            "priority": "URGT",
        },
    )
    assert r_gen.status_code == 200
    gen_data = r_gen.json()
    assert gen_data["valid"] is True

    r_parse = client.post("/iso20022/parse", json={"xml": gen_data["xml"]})
    assert r_parse.status_code == 200
    assert r_parse.json()["payment"]["amount"] == pytest.approx(500.0)

    r_settle = client.post(
        "/settle",
        json={"scenario_id": "ring_4bank_classic", "engine": "all"},
    )
    assert r_settle.status_code == 200
    assert r_settle.json()["comparison"]["lsm_volume_cleared_pct"] > 85.0

    r_stress = client.post("/stress", json={"scenario": "collateral_haircut_shock"})
    assert r_stress.status_code == 200
    assert r_stress.json()["collateral_haircut_pct"] == pytest.approx(35.0)

    r_bench = client.get("/bench")
    assert r_bench.status_code == 200
    assert r_bench.json()["summary"]["scenario_count"] == 8


def test_engines_reject_inputs_they_would_misaccount() -> None:
    a, b = "SBININBBXXX", "HDFCINBBXXX"
    banks = [BankNode(a, "A", 10.0, 0.0), BankNode(b, "B", 10.0, 0.0)]
    # Duplicate IDs used to make the 100 Cr payment disappear from both lists
    dup = [
        QueuedPayment("X", a, b, 8.0, "URGT", 1),
        QueuedPayment("X", b, a, 8.0, "URGT", 2),
        QueuedPayment("X", a, b, 100.0, "NORM", 3),
    ]
    for engine in (settle_fifo, settle_bilateral, settle_multilateral_lsm):
        with pytest.raises(ValueError):
            engine(banks, dup)
        with pytest.raises(ValueError):
            engine(banks, [QueuedPayment("P", a, "ICICINBBXXX", 5.0)])
    with pytest.raises(ValueError):
        QueuedPayment("N", a, b, float("nan"))
    with pytest.raises(ValueError):
        QueuedPayment("T", a, b, 5.0, timestamp="yesterday")
    with pytest.raises(ValueError):
        BankNode(a, "A", float("inf"), 0.0)


def test_parser_rejects_bad_amounts_dtds_and_missing_currency() -> None:
    good = generate_pacs008_xml(
        ISO20022Payment("M1", "E1", "SBININBBXXX", "HDFCINBBXXX", 10.0, settlement_timestamp="2026-10-22T09:15:00Z")
    )
    assert parse_iso20022_xml(good).amount == pytest.approx(10.0)
    for bad_amount in ("nan", "inf", "1e308", "-5.00", "10.123456"):
        with pytest.raises(ISO20022ValidationError):
            parse_iso20022_xml(good.replace(">10.00<", f">{bad_amount}<"))
    with pytest.raises(ISO20022ValidationError):
        parse_iso20022_xml(good.replace(' Ccy="INR"', ""))
    with pytest.raises(ISO20022ValidationError):
        parse_iso20022_xml(good.replace("2026-10-22T09:15:00Z", "yesterday"))
    bomb = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><Document>&a;</Document>'
    with pytest.raises(ISO20022ValidationError):
        parse_iso20022_xml(bomb)
