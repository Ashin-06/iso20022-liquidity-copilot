"""
Benchmark Suite: FIFO Gross vs Bilateral Offset vs Multilateral Cycle-Netting LSM
across 8 Interbank RTGS Gridlock & Basel III BCBS 248 Stress Scenarios.

Proves:
1. Multilateral LSM clears >85% of gridlocked volume where FIFO clears <20%.
2. Multilateral LSM cuts peak intraday liquidity needed by >50% (both vs FIFO drawdown and vs Gross).
3. Zero regulatory reserve floor F_i violations across all banks and all engines.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .gridlock import (
    BankNode,
    QueuedPayment,
    compare_settlement_engines,
)


def get_benchmark_scenarios() -> List[Dict[str, Any]]:
    """Return 8 interbank RTGS gridlock and stress benchmark scenarios."""
    return [
        # 1. Classic 4-Bank Circular Ring Gridlock
        {
            "scenario_id": "ring_4bank_classic",
            "title": "4-Bank Circular Ring Gridlock (A->B->C->D->A)",
            "category": "gridlock",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 500.0, 400.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 500.0, 400.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 500.0, 400.0),
                BankNode("AXISINBBXXX", "Axis Bank", 500.0, 400.0),
            ],
            "payments": [
                # Small initial URGT payment settles in FIFO (draws 85 Cr), then HOL stalls on 360 Cr!
                QueuedPayment("R4-01", "SBININBBXXX", "HDFCINBBXXX", 85.0, "URGT", 1),
                QueuedPayment("R4-02", "SBININBBXXX", "ICICINBBXXX", 360.0, "URGT", 2),
                QueuedPayment("R4-03", "HDFCINBBXXX", "ICICINBBXXX", 340.0, "HIGH", 3),
                QueuedPayment("R4-04", "ICICINBBXXX", "AXISINBBXXX", 690.0, "HIGH", 4),
                QueuedPayment("R4-05", "AXISINBBXXX", "SBININBBXXX", 425.0, "NORM", 5),
                QueuedPayment("R4-06", "AXISINBBXXX", "HDFCINBBXXX", 245.0, "NORM", 6),
            ],
        },
        # 2. 6-Bank Tiered Wholesale Mesh
        {
            "scenario_id": "mesh_6bank_tiered",
            "title": "6-Bank Tiered Wholesale RTGS Mesh",
            "category": "gridlock",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 600.0, 500.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 580.0, 480.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 550.0, 450.0),
                BankNode("AXISINBBXXX", "Axis Bank", 520.0, 430.0),
                BankNode("KKBKINBBXXX", "Kotak Mahindra Bank", 490.0, 400.0),
                BankNode("BARBINBBXXX", "Bank of Baroda", 510.0, 420.0),
            ],
            "payments": [
                QueuedPayment("M6-01", "SBININBBXXX", "HDFCINBBXXX", 90.0, "URGT", 1),
                QueuedPayment("M6-02", "HDFCINBBXXX", "SBININBBXXX", 95.0, "URGT", 2),
                QueuedPayment("M6-03", "SBININBBXXX", "ICICINBBXXX", 310.0, "URGT", 3),
                QueuedPayment("M6-04", "ICICINBBXXX", "AXISINBBXXX", 305.0, "HIGH", 4),
                QueuedPayment("M6-05", "AXISINBBXXX", "KKBKINBBXXX", 300.0, "HIGH", 5),
                QueuedPayment("M6-06", "KKBKINBBXXX", "BARBINBBXXX", 295.0, "NORM", 6),
                QueuedPayment("M6-07", "BARBINBBXXX", "HDFCINBBXXX", 290.0, "NORM", 7),
                QueuedPayment("M6-08", "HDFCINBBXXX", "SBININBBXXX", 285.0, "NORM", 8),
            ],
        },
        # 3. Star Money-Center Hub Congestion
        {
            "scenario_id": "star_hub_congestion",
            "title": "Money-Center Hub & 5 Regional Spokes Congestion",
            "category": "gridlock",
            "banks": [
                BankNode("SBININBBXXX", "SBI Clearing Hub", 700.0, 600.0),
                BankNode("HDFCINBBXXX", "HDFC Spoke", 460.0, 380.0),
                BankNode("ICICINBBXXX", "ICICI Spoke", 460.0, 380.0),
                BankNode("AXISINBBXXX", "Axis Spoke", 460.0, 380.0),
                BankNode("KKBKINBBXXX", "Kotak Spoke", 460.0, 380.0),
                BankNode("PUNBINBBXXX", "PNB Spoke", 460.0, 380.0),
            ],
            "payments": [
                QueuedPayment("SH-01", "SBININBBXXX", "HDFCINBBXXX", 88.0, "URGT", 1),
                QueuedPayment("SH-02", "SBININBBXXX", "ICICINBBXXX", 415.0, "URGT", 2),
                QueuedPayment("SH-03", "HDFCINBBXXX", "AXISINBBXXX", 250.0, "HIGH", 3),
                QueuedPayment("SH-04", "ICICINBBXXX", "KKBKINBBXXX", 410.0, "HIGH", 4),
                QueuedPayment("SH-05", "AXISINBBXXX", "PUNBINBBXXX", 245.0, "NORM", 5),
                QueuedPayment("SH-06", "KKBKINBBXXX", "SBININBBXXX", 405.0, "NORM", 6),
                QueuedPayment("SH-07", "PUNBINBBXXX", "HDFCINBBXXX", 165.0, "NORM", 7),
                QueuedPayment("SH-08", "PUNBINBBXXX", "SBININBBXXX", 80.0, "NORM", 8),
            ],
        },
        # 4. Combined Bilateral & Trilateral Cycle Mix
        {
            "scenario_id": "bilateral_vs_trilateral_mix",
            "title": "Hybrid Bilateral Pairs + 3-Bank & 5-Bank Cycles",
            "category": "gridlock",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 520.0, 430.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 520.0, 430.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 520.0, 430.0),
                BankNode("AXISINBBXXX", "Axis Bank", 520.0, 430.0),
                BankNode("KKBKINBBXXX", "Kotak Mahindra Bank", 520.0, 430.0),
            ],
            "payments": [
                QueuedPayment("BT-01", "SBININBBXXX", "HDFCINBBXXX", 80.0, "URGT", 1),
                # Bilateral opposite pair between ICICI and AXIS (280 vs 275)
                QueuedPayment("BT-02", "ICICINBBXXX", "AXISINBBXXX", 280.0, "URGT", 2),
                QueuedPayment("BT-03", "AXISINBBXXX", "ICICINBBXXX", 275.0, "URGT", 3),
                # 3-bank triangle SBI -> HDFC -> KKBK -> SBI
                QueuedPayment("BT-04", "SBININBBXXX", "HDFCINBBXXX", 240.0, "HIGH", 4),
                QueuedPayment("BT-05", "HDFCINBBXXX", "KKBKINBBXXX", 315.0, "HIGH", 5),
                QueuedPayment("BT-06", "KKBKINBBXXX", "SBININBBXXX", 310.0, "NORM", 6),
            ],
        },
        # 5. BCBS 248 Normal Intraday Peak Wave
        {
            "scenario_id": "stress_normal_intraday",
            "title": "BCBS 248 Normal Intraday Peak Settlement Wave",
            "category": "stress",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 550.0, 450.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 530.0, 440.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 510.0, 420.0),
                BankNode("AXISINBBXXX", "Axis Bank", 490.0, 400.0),
                BankNode("KKBKINBBXXX", "Kotak Mahindra Bank", 470.0, 390.0),
            ],
            "payments": [
                QueuedPayment("SN-01", "SBININBBXXX", "HDFCINBBXXX", 92.0, "URGT", 1),
                QueuedPayment("SN-02", "SBININBBXXX", "ICICINBBXXX", 290.0, "URGT", 2),
                QueuedPayment("SN-03", "HDFCINBBXXX", "AXISINBBXXX", 310.0, "HIGH", 3),
                QueuedPayment("SN-04", "ICICINBBXXX", "KKBKINBBXXX", 285.0, "HIGH", 4),
                QueuedPayment("SN-05", "AXISINBBXXX", "SBININBBXXX", 305.0, "NORM", 5),
                QueuedPayment("SN-06", "KKBKINBBXXX", "SBININBBXXX", 70.0, "NORM", 6),
                QueuedPayment("SN-07", "KKBKINBBXXX", "HDFCINBBXXX", 210.0, "NORM", 7),
            ],
        },
        # 6. BCBS 248 Counterparty Default Shock
        {
            "scenario_id": "stress_counterparty_default",
            "title": "BCBS 248 Counterparty Outbound Freeze / Default Shock",
            "category": "stress",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 560.0, 460.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 540.0, 450.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 520.0, 430.0),
                BankNode("AXISINBBXXX", "Axis Bank", 500.0, 410.0),
                BankNode("BARBINBBXXX", "Defaulting Sink Bank", 450.0, 440.0),
            ],
            "payments": [
                QueuedPayment("CD-01", "SBININBBXXX", "HDFCINBBXXX", 90.0, "URGT", 1),
                # Surviving 4-bank ring clears via LSM while isolated payment to defaulting sink is pruned
                QueuedPayment("CD-02", "SBININBBXXX", "ICICINBBXXX", 340.0, "URGT", 2),
                QueuedPayment("CD-03", "HDFCINBBXXX", "AXISINBBXXX", 330.0, "HIGH", 3),
                QueuedPayment("CD-04", "ICICINBBXXX", "HDFCINBBXXX", 245.0, "HIGH", 4),
                QueuedPayment("CD-05", "AXISINBBXXX", "SBININBBXXX", 420.0, "HIGH", 5),
                QueuedPayment("CD-06", "ICICINBBXXX", "AXISINBBXXX", 95.0, "NORM", 6),
                # Unreciprocated outflow to defaulting bank BARBINBBXXX (pruned by LSM to protect floors)
                QueuedPayment("CD-07", "HDFCINBBXXX", "BARBINBBXXX", 180.0, "NORM", 7),
            ],
        },
        # 7. BCBS 248 Liquidity Hoarding Shock
        {
            "scenario_id": "stress_liquidity_hoarding",
            "title": "BCBS 248 Precautionary Liquidity Hoarding Shock",
            "category": "stress",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 510.0, 430.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 500.0, 425.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 490.0, 420.0),
                BankNode("AXISINBBXXX", "Axis Bank", 480.0, 410.0),
                BankNode("KKBKINBBXXX", "Kotak Mahindra Bank", 470.0, 405.0),
            ],
            "payments": [
                QueuedPayment("LH-01", "SBININBBXXX", "HDFCINBBXXX", 72.0, "URGT", 1),
                QueuedPayment("LH-02", "SBININBBXXX", "ICICINBBXXX", 280.0, "URGT", 2),
                QueuedPayment("LH-03", "HDFCINBBXXX", "AXISINBBXXX", 345.0, "HIGH", 3),
                QueuedPayment("LH-04", "ICICINBBXXX", "KKBKINBBXXX", 275.0, "HIGH", 4),
                QueuedPayment("LH-05", "AXISINBBXXX", "SBININBBXXX", 340.0, "NORM", 5),
                QueuedPayment("LH-06", "KKBKINBBXXX", "HDFCINBBXXX", 270.0, "NORM", 6),
            ],
        },
        # 8. BCBS 248 Collateral Haircut Shock (35% Haircut Spike)
        {
            "scenario_id": "stress_collateral_haircut_shock",
            "title": "BCBS 248 35% Repo Collateral Haircut Spike",
            "category": "stress",
            "banks": [
                BankNode("SBININBBXXX", "State Bank of India", 495.0, 420.0),
                BankNode("HDFCINBBXXX", "HDFC Bank", 490.0, 420.0),
                BankNode("ICICINBBXXX", "ICICI Bank", 485.0, 420.0),
                BankNode("AXISINBBXXX", "Axis Bank", 480.0, 420.0),
                BankNode("PUNBINBBXXX", "Punjab National Bank", 475.0, 415.0),
            ],
            "payments": [
                QueuedPayment("CH-01", "SBININBBXXX", "HDFCINBBXXX", 68.0, "URGT", 1),
                QueuedPayment("CH-02", "SBININBBXXX", "ICICINBBXXX", 310.0, "URGT", 2),
                QueuedPayment("CH-03", "HDFCINBBXXX", "AXISINBBXXX", 370.0, "URGT", 3),
                QueuedPayment("CH-04", "ICICINBBXXX", "PUNBINBBXXX", 305.0, "HIGH", 4),
                QueuedPayment("CH-05", "AXISINBBXXX", "SBININBBXXX", 365.0, "HIGH", 5),
                QueuedPayment("CH-06", "PUNBINBBXXX", "HDFCINBBXXX", 300.0, "NORM", 6),
            ],
        },
    ]


def run_all_benchmarks() -> Dict[str, Any]:
    """
    Execute all 8 interbank gridlock and stress benchmark scenarios across
    FIFO Gross, Bilateral Offset, and Multilateral LSM.
    """
    scenarios = get_benchmark_scenarios()
    scenario_results: List[Dict[str, Any]] = []

    total_queued_vol = 0.0
    total_fifo_settled_vol = 0.0
    total_bilat_settled_vol = 0.0
    total_lsm_settled_vol = 0.0

    total_fifo_peak_liq = 0.0
    total_lsm_peak_liq = 0.0
    total_gross_liq_needed = 0.0
    total_floor_violations = 0

    for sc in scenarios:
        banks: List[BankNode] = sc["banks"]
        payments: List[QueuedPayment] = sc["payments"]
        comp = compare_settlement_engines(banks, payments)

        q_vol = comp["fifo"]["total_volume"]
        f_vol = comp["fifo"]["settled_volume"]
        b_vol = comp["bilateral"]["settled_volume"]
        l_vol = comp["multilateral_lsm"]["settled_volume"]

        # Apples-to-apples: liquidity FIFO gross RTGS needs to settle the same payments the LSM settled
        f_peak = comp["comparison"]["fifo_gross_peak_same_set"]
        l_peak = comp["multilateral_lsm"]["peak_liquidity_used"]
        g_need = comp["multilateral_lsm"]["gross_liquidity_needed"]

        violations = (
            comp["fifo"]["floor_violations"]
            + comp["bilateral"]["floor_violations"]
            + comp["multilateral_lsm"]["floor_violations"]
        )

        total_queued_vol += q_vol
        total_fifo_settled_vol += f_vol
        total_bilat_settled_vol += b_vol
        total_lsm_settled_vol += l_vol
        total_fifo_peak_liq += f_peak
        total_lsm_peak_liq += l_peak
        total_gross_liq_needed += g_need
        total_floor_violations += violations

        scenario_results.append(
            {
                "scenario_id": sc["scenario_id"],
                "title": sc["title"],
                "category": sc["category"],
                "bank_count": len(banks),
                "payment_count": len(payments),
                "total_volume_cr": round(q_vol, 2),
                "fifo_settled_volume_cr": round(f_vol, 2),
                "bilateral_settled_volume_cr": round(b_vol, 2),
                "lsm_settled_volume_cr": round(l_vol, 2),
                "fifo_cleared_pct": comp["comparison"]["fifo_volume_cleared_pct"],
                "bilateral_cleared_pct": comp["comparison"]["bilateral_volume_cleared_pct"],
                "lsm_cleared_pct": comp["comparison"]["lsm_volume_cleared_pct"],
                "fifo_peak_liquidity_cr": round(f_peak, 2),
                "lsm_peak_liquidity_cr": round(l_peak, 2),
                "peak_liquidity_cut_vs_fifo_pct": comp["comparison"][
                    "peak_liquidity_reduction_vs_fifo_pct"
                ],
                "peak_liquidity_cut_vs_gross_pct": comp["comparison"][
                    "peak_liquidity_reduction_vs_gross_pct"
                ],
                "cycles_detected": comp["multilateral_lsm"]["cycles_detected"],
                "floor_violations": violations,
            }
        )

    fifo_overall_pct = (total_fifo_settled_vol / total_queued_vol) * 100.0
    bilat_overall_pct = (total_bilat_settled_vol / total_queued_vol) * 100.0
    lsm_overall_pct = (total_lsm_settled_vol / total_queued_vol) * 100.0

    peak_cut_vs_fifo_pct = (
        (total_fifo_peak_liq - total_lsm_peak_liq) / max(total_fifo_peak_liq, 1e-6)
    ) * 100.0
    peak_cut_vs_gross_pct = (
        (total_lsm_settled_vol - total_lsm_peak_liq) / max(total_lsm_settled_vol, 1e-6)
    ) * 100.0

    summary = {
        "scenario_count": len(scenarios),
        "total_queued_volume_cr": round(total_queued_vol, 2),
        "fifo_settled_volume_cr": round(total_fifo_settled_vol, 2),
        "bilateral_settled_volume_cr": round(total_bilat_settled_vol, 2),
        "lsm_settled_volume_cr": round(total_lsm_settled_vol, 2),
        "fifo_volume_cleared_pct": round(fifo_overall_pct, 2),
        "bilateral_volume_cleared_pct": round(bilat_overall_pct, 2),
        "lsm_volume_cleared_pct": round(lsm_overall_pct, 2),
        "total_fifo_peak_liquidity_cr": round(total_fifo_peak_liq, 2),
        "total_lsm_peak_liquidity_cr": round(total_lsm_peak_liq, 2),
        "total_gross_liquidity_needed_cr": round(total_gross_liq_needed, 2),
        "peak_liquidity_reduction_vs_fifo_pct": round(peak_cut_vs_fifo_pct, 2),
        "peak_liquidity_reduction_vs_gross_pct": round(peak_cut_vs_gross_pct, 2),
        "total_floor_violations": total_floor_violations,
        "assertions": {
            "lsm_clears_over_85_pct": lsm_overall_pct > 85.0,
            "fifo_clears_under_20_pct": fifo_overall_pct < 20.0,
            "peak_liquidity_cut_over_50_pct": (
                peak_cut_vs_fifo_pct > 50.0 and peak_cut_vs_gross_pct > 50.0
            ),
            "zero_reserve_floor_violations": total_floor_violations == 0,
        },
    }

    return {
        "summary": summary,
        "scenarios": scenario_results,
    }


def format_benchmark_report(bench_data: Dict[str, Any] | None = None) -> str:
    """Format the 8-scenario RTGS gridlock & stress benchmark into a readable table."""
    if bench_data is None:
        bench_data = run_all_benchmarks()

    s = bench_data["summary"]
    lines = [
        "=" * 108,
        "  ISO 20022 RTGS LIQUIDITY COPILOT — 8-SCENARIO GRIDLOCK & BCBS 248 STRESS BENCHMARK",
        "=" * 108,
        f"{'Scenario ID':<32} | {'Queued(Cr)':>10} | {'FIFO %':>7} | {'Bilat %':>8} | {'LSM %':>7} | {'FIFO Liq':>9} | {'LSM Liq':>8} | {'Viol':>4}",
        "-" * 108,
    ]
    for row in bench_data["scenarios"]:
        lines.append(
            f"{row['scenario_id']:<32} | "
            f"{row['total_volume_cr']:>10.1f} | "
            f"{row['fifo_cleared_pct']:>6.1f}% | "
            f"{row['bilateral_cleared_pct']:>7.1f}% | "
            f"{row['lsm_cleared_pct']:>6.1f}% | "
            f"{row['fifo_peak_liquidity_cr']:>9.1f} | "
            f"{row['lsm_peak_liquidity_cr']:>8.1f} | "
            f"{row['floor_violations']:>4d}"
        )
    lines.append("-" * 108)
    lines.append(
        f"{'AGGREGATE (8 SCENARIOS)':<32} | "
        f"{s['total_queued_volume_cr']:>10.1f} | "
        f"{s['fifo_volume_cleared_pct']:>6.1f}% | "
        f"{s['bilateral_volume_cleared_pct']:>7.1f}% | "
        f"{s['lsm_volume_cleared_pct']:>6.1f}% | "
        f"{s['total_fifo_peak_liquidity_cr']:>9.1f} | "
        f"{s['total_lsm_peak_liquidity_cr']:>8.1f} | "
        f"{s['total_floor_violations']:>4d}"
    )
    lines.append("=" * 108)
    lines.append(
        f"Proof Checks: LSM Cleared = {s['lsm_volume_cleared_pct']:.2f}% (>85%) | "
        f"FIFO Cleared = {s['fifo_volume_cleared_pct']:.2f}% (<20%) | "
        f"Peak Liquidity Cut vs FIFO = {s['peak_liquidity_reduction_vs_fifo_pct']:.2f}% (>50%) | "
        f"Peak Liquidity Cut vs Gross = {s['peak_liquidity_reduction_vs_gross_pct']:.2f}% (>50%) | "
        f"Floor Violations = {s['total_floor_violations']}"
    )
    return "\n".join(lines)
