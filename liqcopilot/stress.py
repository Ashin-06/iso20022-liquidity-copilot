"""
Basel III BCBS 248 Intraday Liquidity Stress Simulator & Repo Collateral vs Delay Optimizer.

Implements BCBS 248 ("Monitoring tools for intraday liquidity management") metrics:
1. Daily maximum intraday liquidity usage (largest positive net cumulative outflow)
2. Available intraday liquidity at the start of the business day
3. Total payments (gross inflows and outflows)
4. Time-specific and critical obligations (URGT priority settlement rate)
5. Intraday throughput by hour (% settled by 12:00, 15:00, and EOD 18:00)

Supports four stress scenarios:
- `normal`: Baseline intraday RTGS operations and 5% sovereign collateral haircut
- `counterparty_default`: A major clearing bank freezes all outbound payments (liquidity sink)
- `liquidity_hoarding`: Banks delay NORM/HIGH payments past midday and hoard precautionary buffers
- `collateral_haircut_shock`: Repo haircut spikes from 5% to 35%, contracting intraday credit lines
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .gridlock import (
    BankNode,
    QueuedPayment,
    gross_sequential_peak,
    settle_fifo,
    settle_multilateral_lsm,
)

VALID_STRESS_SCENARIOS = {
    "normal",
    "counterparty_default",
    "liquidity_hoarding",
    "collateral_haircut_shock",
}


@dataclass
class RepoDelayOptimization:
    """Optimal intraday repo collateral pledge vs payment delay cost analysis."""

    scenario: str
    repo_rate_bps_per_day: float
    collateral_haircut_pct: float
    delay_cost_bps_per_hour: Dict[str, float]
    unoptimized_zero_pledge_cost_lakhs: float
    naive_full_prefund_cost_lakhs: float
    optimal_lsm_total_cost_lakhs: float
    optimal_repo_pledge_cr: float
    effective_liquidity_injected_cr: float
    repo_funding_cost_lakhs: float
    residual_delay_cost_lakhs: float
    cost_savings_vs_unoptimized_pct: float
    cost_savings_vs_naive_prefund_pct: float
    tradeoff_curve: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BCBS248Report:
    """Basel III BCBS 248 Intraday Liquidity Monitoring Report under a given scenario."""

    scenario: str
    scenario_description: str
    available_intraday_liquidity_sod_cr: float
    free_central_bank_reserves_sod_cr: float
    collateral_credit_line_sod_cr: float
    collateral_haircut_pct: float
    daily_max_liquidity_usage_fifo_cr: float
    daily_max_liquidity_usage_lsm_cr: float
    peak_usage_reduction_lsm_pct: float
    total_gross_payments_cr: float
    time_specific_obligations_cr: float
    time_specific_settled_lsm_pct: float
    throughput_fifo_pct: Dict[str, float]
    throughput_lsm_pct: Dict[str, float]
    settled_by_1200_fifo_pct: float
    settled_by_1500_fifo_pct: float
    settled_by_eod_fifo_pct: float
    settled_by_1200_lsm_pct: float
    settled_by_1500_lsm_pct: float
    settled_by_eod_lsm_pct: float
    per_bank_bcbs248: List[Dict[str, Any]]
    hourly_timeline: List[Dict[str, Any]]
    collateral_optimization: RepoDelayOptimization
    floor_violations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["collateral_optimization"] = self.collateral_optimization.to_dict()
        return d


def _parse_hour(ts: str) -> int:
    """Extract integer hour (9..18) from ISO-8601 timestamp or HH:MM string."""
    if "T" in ts:
        time_part = ts.split("T", 1)[1]
    else:
        time_part = ts
    try:
        return int(time_part[:2])
    except (ValueError, IndexError):
        return 12


def _default_intraday_network() -> tuple[List[BankNode], Dict[str, float], List[QueuedPayment]]:
    """
    Construct a realistic 6-bank wholesale Indian RTGS network with intraday schedule (09:00-17:30).
    Returns (base_banks, nominal_collateral_cr_by_bank, base_payments).
    """
    banks = [
        BankNode("SBININBBXXX", "State Bank of India", opening_balance=520.0, reserve_floor=450.0),
        BankNode("HDFCINBBXXX", "HDFC Bank", opening_balance=480.0, reserve_floor=410.0),
        BankNode("ICICINBBXXX", "ICICI Bank", opening_balance=450.0, reserve_floor=390.0),
        BankNode("AXISINBBXXX", "Axis Bank", opening_balance=410.0, reserve_floor=360.0),
        BankNode("KKBKINBBXXX", "Kotak Mahindra Bank", opening_balance=380.0, reserve_floor=330.0),
        BankNode("BARBINBBXXX", "Bank of Baroda", opening_balance=390.0, reserve_floor=340.0),
    ]
    collateral_nominal = {
        "SBININBBXXX": 120.0,
        "HDFCINBBXXX": 100.0,
        "ICICINBBXXX": 90.0,
        "AXISINBBXXX": 80.0,
        "KKBKINBBXXX": 75.0,
        "BARBINBBXXX": 75.0,
    }

    payments = [
        # Morning window (09:00 - 12:00): High-value CLS/G-Sec/corporate settlements
        QueuedPayment("STR-01", "SBININBBXXX", "HDFCINBBXXX", 44.0, "URGT", 1, "2026-10-22T09:15:00Z"),
        QueuedPayment("STR-02", "SBININBBXXX", "ICICINBBXXX", 180.0, "URGT", 2, "2026-10-22T09:30:00Z"),
        QueuedPayment("STR-03", "HDFCINBBXXX", "ICICINBBXXX", 190.0, "URGT", 3, "2026-10-22T10:00:00Z"),
        QueuedPayment("STR-04", "ICICINBBXXX", "AXISINBBXXX", 365.0, "HIGH", 4, "2026-10-22T10:30:00Z"),
        QueuedPayment("STR-05", "AXISINBBXXX", "KKBKINBBXXX", 360.0, "HIGH", 5, "2026-10-22T11:00:00Z"),
        QueuedPayment("STR-06", "KKBKINBBXXX", "BARBINBBXXX", 355.0, "URGT", 6, "2026-10-22T11:30:00Z"),
        QueuedPayment("STR-07", "BARBINBBXXX", "SBININBBXXX", 215.0, "NORM", 7, "2026-10-22T11:45:00Z"),
        QueuedPayment("STR-08", "BARBINBBXXX", "HDFCINBBXXX", 140.0, "NORM", 8, "2026-10-22T11:55:00Z"),
        # Midday window (12:01 - 15:00): Money market & FX legs
        QueuedPayment("STR-09", "HDFCINBBXXX", "AXISINBBXXX", 190.0, "HIGH", 9, "2026-10-22T12:30:00Z"),
        QueuedPayment("STR-10", "AXISINBBXXX", "BARBINBBXXX", 185.0, "HIGH", 10, "2026-10-22T13:15:00Z"),
        QueuedPayment("STR-11", "BARBINBBXXX", "KKBKINBBXXX", 180.0, "NORM", 11, "2026-10-22T13:45:00Z"),
        QueuedPayment("STR-12", "KKBKINBBXXX", "SBININBBXXX", 175.0, "NORM", 12, "2026-10-22T14:15:00Z"),
        QueuedPayment("STR-13", "SBININBBXXX", "HDFCINBBXXX", 170.0, "HIGH", 13, "2026-10-22T14:45:00Z"),
        # Afternoon / EOD window (15:01 - 18:00): Final treasury squaring
        QueuedPayment("STR-14", "HDFCINBBXXX", "KKBKINBBXXX", 160.0, "URGT", 14, "2026-10-22T15:30:00Z"),
        QueuedPayment("STR-15", "KKBKINBBXXX", "ICICINBBXXX", 165.0, "HIGH", 15, "2026-10-22T16:00:00Z"),
        QueuedPayment("STR-16", "ICICINBBXXX", "BARBINBBXXX", 160.0, "NORM", 16, "2026-10-22T16:30:00Z"),
        QueuedPayment("STR-17", "BARBINBBXXX", "AXISINBBXXX", 155.0, "NORM", 17, "2026-10-22T17:00:00Z"),
        QueuedPayment("STR-18", "AXISINBBXXX", "HDFCINBBXXX", 160.0, "URGT", 18, "2026-10-22T17:30:00Z"),
    ]
    return banks, collateral_nominal, payments


def optimize_collateral_vs_delay(
    banks: List[BankNode],
    payments: List[QueuedPayment],
    scenario: str = "normal",
    repo_rate_bps_per_day: float = 1.8,
    collateral_haircut_pct: float = 5.0,
) -> RepoDelayOptimization:
    """
    Compute the optimal intraday repo collateral pledge C* vs payment delay penalty.

    Cost model (in INR Lakhs, where 1 Crore = 100 Lakhs):
    - Repo pledge cost: pledging C Crores of collateral with haircut h costs
      C * (repo_rate_bps_per_day / 100.0) / (1 - h) Lakhs per day in opportunity & encumbering cost.
    - Payment delay / gridlock penalty cost: each unsettled/delayed Crore incurs priority-weighted
      SLA/regulatory delay cost per hour (URGT: 4.5 bps/hr, HIGH: 2.5 bps/hr, NORM: 1.2 bps/hr).
    """
    delay_cost_bps = {"URGT": 4.5, "HIGH": 2.5, "NORM": 1.2}
    haircut_frac = min(0.85, max(0.0, collateral_haircut_pct / 100.0))
    effective_multiplier = 1.0 - haircut_frac

    total_vol = sum(p.amount for p in payments)

    # Evaluate candidate collateral pledges (system-wide extra repo pledge in Crores)
    candidate_pledges = [0.0, 25.0, 50.0, 80.0, 120.0, 160.0, 220.0, 300.0, 450.0, 650.0]
    tradeoff_curve: List[Dict[str, float]] = []

    best_lsm_entry: Optional[Dict[str, float]] = None

    # 1. Baseline unoptimized cost (FIFO without extra collateral pledge)
    fifo_zero = settle_fifo(banks, payments)
    unoptimized_delay_cost_lakhs = sum(
        p.amount
        * (delay_cost_bps[p.priority] / 100.0)
        * max(2.0, 18.0 - _parse_hour(p.timestamp))
        for p in fifo_zero.unsettled_payments
    )

    # 2. Naive full gross prefunding (pledging 55% of total gross volume to clear without LSM)
    naive_pledge_cr = round(total_vol * 0.45 / max(effective_multiplier, 0.2), 2)
    naive_repo_cost_lakhs = naive_pledge_cr * (repo_rate_bps_per_day / 100.0) * 2.2
    naive_total_cost_lakhs = naive_repo_cost_lakhs + 1.5  # minimal operational friction

    n_banks = max(1, len(banks))
    for pledge_cr in candidate_pledges:
        eff_injected = pledge_cr * effective_multiplier
        per_bank_boost = eff_injected / n_banks
        boosted_banks = [
            BankNode(
                b.bic,
                b.name,
                opening_balance=b.opening_balance + per_bank_boost,
                reserve_floor=b.reserve_floor,
            )
            for b in banks
        ]
        lsm_res = settle_multilateral_lsm(boosted_banks, payments)

        repo_cost_lakhs = (
            pledge_cr * (repo_rate_bps_per_day / 100.0) / max(effective_multiplier, 0.25)
        )
        delay_cost_lakhs = sum(
            p.amount
            * (delay_cost_bps[p.priority] / 100.0)
            * max(1.5, 18.0 - _parse_hour(p.timestamp))
            for p in lsm_res.unsettled_payments
        )
        total_cost_lakhs = repo_cost_lakhs + delay_cost_lakhs

        point = {
            "repo_pledge_cr": round(pledge_cr, 2),
            "effective_liquidity_cr": round(eff_injected, 2),
            "lsm_settled_pct": round(lsm_res.volume_clearing_ratio * 100.0, 2),
            "repo_cost_lakhs": round(repo_cost_lakhs, 2),
            "delay_cost_lakhs": round(delay_cost_lakhs, 2),
            "total_cost_lakhs": round(total_cost_lakhs, 2),
        }
        tradeoff_curve.append(point)

        if best_lsm_entry is None or total_cost_lakhs < best_lsm_entry["total_cost_lakhs"]:
            best_lsm_entry = point

    assert best_lsm_entry is not None
    opt_total = best_lsm_entry["total_cost_lakhs"]
    savings_vs_unopt = (
        max(0.0, (1.0 - opt_total / max(unoptimized_delay_cost_lakhs, 1e-6)) * 100.0)
        if unoptimized_delay_cost_lakhs > 0
        else 0.0
    )
    savings_vs_naive = (
        max(0.0, (1.0 - opt_total / max(naive_total_cost_lakhs, 1e-6)) * 100.0)
        if naive_total_cost_lakhs > 0
        else 0.0
    )

    return RepoDelayOptimization(
        scenario=scenario,
        repo_rate_bps_per_day=repo_rate_bps_per_day,
        collateral_haircut_pct=collateral_haircut_pct,
        delay_cost_bps_per_hour=delay_cost_bps,
        unoptimized_zero_pledge_cost_lakhs=round(unoptimized_delay_cost_lakhs, 2),
        naive_full_prefund_cost_lakhs=round(naive_total_cost_lakhs, 2),
        optimal_lsm_total_cost_lakhs=round(opt_total, 2),
        optimal_repo_pledge_cr=best_lsm_entry["repo_pledge_cr"],
        effective_liquidity_injected_cr=best_lsm_entry["effective_liquidity_cr"],
        repo_funding_cost_lakhs=best_lsm_entry["repo_cost_lakhs"],
        residual_delay_cost_lakhs=best_lsm_entry["delay_cost_lakhs"],
        cost_savings_vs_unoptimized_pct=round(savings_vs_unopt, 2),
        cost_savings_vs_naive_prefund_pct=round(savings_vs_naive, 2),
        tradeoff_curve=tradeoff_curve,
    )


def simulate_bcbs248_stress(
    scenario: str = "normal",
    banks: Optional[List[BankNode]] = None,
    payments: Optional[List[QueuedPayment]] = None,
) -> BCBS248Report:
    """
    Simulate Basel III BCBS 248 Intraday Liquidity Monitoring Metrics under one of four scenarios:
    - `normal`
    - `counterparty_default`
    - `liquidity_hoarding`
    - `collateral_haircut_shock`
    """
    scenario_key = scenario.strip().lower()
    if scenario_key not in VALID_STRESS_SCENARIOS:
        raise ValueError(
            f"Unsupported stress scenario '{scenario}'. Must be one of {sorted(VALID_STRESS_SCENARIOS)}."
        )

    base_banks, collateral_nominal, base_payments = _default_intraday_network()
    if banks is not None:
        base_banks = [
            BankNode(b.bic, b.name, b.opening_balance, b.reserve_floor) for b in banks
        ]
        collateral_nominal = {b.bic: max(40.0, b.available_liquidity * 1.1) for b in base_banks}
    if payments is not None:
        base_payments = [
            QueuedPayment(
                p.payment_id,
                p.sender,
                p.receiver,
                p.amount,
                p.priority,
                p.arrival_order,
                p.timestamp,
                p.msg_type,
                p.currency,
            )
            for p in payments
        ]

    # Configure stress parameters
    haircut_pct = 5.0
    repo_rate_bps = 1.8
    scenario_desc = (
        "Baseline RTGS operating day with 5% sovereign repo haircut and balanced arrival schedule."
    )

    sim_banks: List[BankNode] = []
    sim_payments: List[QueuedPayment] = []

    if scenario_key == "normal":
        for b in base_banks:
            credit_line = collateral_nominal.get(b.bic, 80.0) * (1.0 - haircut_pct / 100.0)
            sim_banks.append(
                BankNode(
                    b.bic,
                    b.name,
                    opening_balance=b.opening_balance + credit_line * 0.25,
                    reserve_floor=b.reserve_floor,
                )
            )
        sim_payments = list(base_payments)

    elif scenario_key == "counterparty_default":
        scenario_desc = (
            "Counterparty Default Shock: Major bank (BARBINBBXXX) suffers an intraday operational/credit "
            "freeze and stops sending all outbound payments while continuing to receive inflows."
        )
        haircut_pct = 10.0
        repo_rate_bps = 2.4
        defaulting_bic = base_banks[-1].bic
        for b in base_banks:
            credit_line = collateral_nominal.get(b.bic, 80.0) * (1.0 - haircut_pct / 100.0)
            sim_banks.append(
                BankNode(
                    b.bic,
                    b.name,
                    opening_balance=b.opening_balance + credit_line * 0.30,
                    reserve_floor=b.reserve_floor,
                )
            )
        # Outbound payments from defaulting_bic are removed; surviving banks reroute/maintain cycle
        for p in base_payments:
            if p.sender == defaulting_bic:
                continue
            if p.receiver == defaulting_bic:
                # Re-route to SBININBBXXX or HDFCINBBXXX as contingency correspondent or shrink exposure
                alt_rcvr = "SBININBBXXX" if p.sender != "SBININBBXXX" else "HDFCINBBXXX"
                sim_payments.append(
                    QueuedPayment(
                        p.payment_id,
                        p.sender,
                        alt_rcvr,
                        p.amount,
                        p.priority,
                        p.arrival_order,
                        p.timestamp,
                        p.msg_type,
                        p.currency,
                    )
                )
            else:
                sim_payments.append(p)

    elif scenario_key == "liquidity_hoarding":
        scenario_desc = (
            "Liquidity Hoarding Shock: Banks withhold precautionary reserve buffers (higher effective floor) "
            "and delay NORM/HIGH payments into the afternoon window (14:00-17:30)."
        )
        haircut_pct = 8.0
        repo_rate_bps = 2.6
        for b in base_banks:
            # Banks hoard 35% of their free reserve buffer
            hoarded_floor = b.reserve_floor + 0.35 * b.available_liquidity
            sim_banks.append(
                BankNode(
                    b.bic,
                    b.name,
                    opening_balance=b.opening_balance,
                    reserve_floor=hoarded_floor,
                )
            )
        for p in base_payments:
            orig_hr = _parse_hour(p.timestamp)
            if p.priority in ("NORM", "HIGH") and orig_hr < 15:
                shifted_hr = min(17, orig_hr + 4)
                new_ts = f"2026-10-22T{shifted_hr:02d}:15:00Z"
            else:
                new_ts = p.timestamp
            sim_payments.append(
                QueuedPayment(
                    p.payment_id,
                    p.sender,
                    p.receiver,
                    p.amount,
                    p.priority,
                    p.arrival_order,
                    new_ts,
                    p.msg_type,
                    p.currency,
                )
            )

    elif scenario_key == "collateral_haircut_shock":
        scenario_desc = (
            "Collateral Haircut Shock: Central bank / repo market haircut spikes from 5% to 35% "
            "during sovereign yield stress, contracting intraday credit lines by 30 percentage points."
        )
        haircut_pct = 35.0
        repo_rate_bps = 3.5
        for b in base_banks:
            # Severe haircut contraction leaves banks with tightly constrained intraday buffer
            shocked_floor = b.reserve_floor + 0.25 * b.available_liquidity
            sim_banks.append(
                BankNode(
                    b.bic,
                    b.name,
                    opening_balance=b.opening_balance,
                    reserve_floor=shocked_floor,
                )
            )
        sim_payments = list(base_payments)

    free_reserves_sod = sum(b.available_liquidity for b in sim_banks)
    collateral_credit_sod = sum(
        collateral_nominal.get(b.bic, 80.0) * (1.0 - haircut_pct / 100.0) for b in sim_banks
    )
    available_liquidity_sod = free_reserves_sod + collateral_credit_sod

    # Run full-day FIFO and Multilateral LSM
    fifo_full = settle_fifo(sim_banks, sim_payments)
    lsm_full = settle_multilateral_lsm(sim_banks, sim_payments)

    total_gross = sum(p.amount for p in sim_payments)
    urgt_payments = [p for p in sim_payments if p.priority == "URGT"]
    urgt_total = sum(p.amount for p in urgt_payments)
    lsm_settled_ids = {p.payment_id for p in lsm_full.settled_payments}
    urgt_settled_lsm = sum(p.amount for p in urgt_payments if p.payment_id in lsm_settled_ids)
    urgt_settled_pct = (urgt_settled_lsm / urgt_total * 100.0) if urgt_total > 0 else 100.0

    # Compute hourly throughput schedule (10:00 through 18:00)
    hours = [10, 11, 12, 13, 14, 15, 16, 17, 18]
    throughput_fifo: Dict[str, float] = {}
    throughput_lsm: Dict[str, float] = {}
    hourly_timeline: List[Dict[str, Any]] = []

    for hr in hours:
        arrived = [p for p in sim_payments if _parse_hour(p.timestamp) <= hr]
        fifo_hr = settle_fifo(sim_banks, arrived)
        lsm_hr = settle_multilateral_lsm(sim_banks, arrived)

        label = f"{hr:02d}:00"
        f_pct = round((fifo_hr.settled_volume / total_gross * 100.0) if total_gross > 0 else 0.0, 2)
        l_pct = round((lsm_hr.settled_volume / total_gross * 100.0) if total_gross > 0 else 0.0, 2)
        throughput_fifo[label] = f_pct
        throughput_lsm[label] = l_pct

        hourly_timeline.append(
            {
                "hour": label,
                "arrived_volume_cr": round(sum(p.amount for p in arrived), 2),
                "fifo_settled_volume_cr": round(fifo_hr.settled_volume, 2),
                "lsm_settled_volume_cr": round(lsm_hr.settled_volume, 2),
                "fifo_throughput_pct": f_pct,
                "lsm_throughput_pct": l_pct,
                "fifo_peak_liquidity_cr": round(fifo_hr.peak_liquidity_used, 2),
                "lsm_peak_liquidity_cr": round(lsm_hr.peak_liquidity_used, 2),
            }
        )

    # Per-bank BCBS 248 breakdown
    per_bank_metrics: List[Dict[str, Any]] = []
    for b in sim_banks:
        gross_out = sum(p.amount for p in sim_payments if p.sender == b.bic)
        gross_in = sum(p.amount for p in sim_payments if p.receiver == b.bic)
        fifo_draw = max(0.0, b.opening_balance - fifo_full.final_balances[b.bic])
        lsm_draw = max(0.0, b.opening_balance - lsm_full.final_balances[b.bic])
        per_bank_metrics.append(
            {
                "bic": b.bic,
                "name": b.name,
                "opening_balance_cr": round(b.opening_balance, 2),
                "reserve_floor_cr": round(b.reserve_floor, 2),
                "free_reserve_sod_cr": round(b.available_liquidity, 2),
                "gross_outflows_cr": round(gross_out, 2),
                "gross_inflows_cr": round(gross_in, 2),
                "fifo_net_drawdown_cr": round(fifo_draw, 2),
                "lsm_net_drawdown_cr": round(lsm_draw, 2),
                "final_balance_lsm_cr": round(lsm_full.final_balances[b.bic], 2),
                "floor_respected": lsm_full.final_balances[b.bic] >= b.reserve_floor - 1e-6,
            }
        )

    # BCBS 248 daily maximum intraday liquidity usage on an identical settled payment set:
    # FIFO gross RTGS (sequential, no offsetting) vs atomic multilateral LSM net drawdown.
    daily_max_fifo = gross_sequential_peak(sim_banks, lsm_full.settled_payments)
    daily_max_lsm = lsm_full.peak_liquidity_used
    peak_reduction_pct = (
        ((daily_max_fifo - daily_max_lsm) / daily_max_fifo * 100.0)
        if daily_max_fifo > 0
        else lsm_full.liquidity_savings_vs_gross_pct
    )

    opt = optimize_collateral_vs_delay(
        banks=sim_banks,
        payments=sim_payments,
        scenario=scenario_key,
        repo_rate_bps_per_day=repo_rate_bps,
        collateral_haircut_pct=haircut_pct,
    )

    return BCBS248Report(
        scenario=scenario_key,
        scenario_description=scenario_desc,
        available_intraday_liquidity_sod_cr=round(available_liquidity_sod, 2),
        free_central_bank_reserves_sod_cr=round(free_reserves_sod, 2),
        collateral_credit_line_sod_cr=round(collateral_credit_sod, 2),
        collateral_haircut_pct=haircut_pct,
        daily_max_liquidity_usage_fifo_cr=round(daily_max_fifo, 2),
        daily_max_liquidity_usage_lsm_cr=round(daily_max_lsm, 2),
        peak_usage_reduction_lsm_pct=round(peak_reduction_pct, 2),
        total_gross_payments_cr=round(total_gross, 2),
        time_specific_obligations_cr=round(urgt_total, 2),
        time_specific_settled_lsm_pct=round(urgt_settled_pct, 2),
        throughput_fifo_pct=throughput_fifo,
        throughput_lsm_pct=throughput_lsm,
        settled_by_1200_fifo_pct=throughput_fifo["12:00"],
        settled_by_1500_fifo_pct=throughput_fifo["15:00"],
        settled_by_eod_fifo_pct=throughput_fifo["18:00"],
        settled_by_1200_lsm_pct=throughput_lsm["12:00"],
        settled_by_1500_lsm_pct=throughput_lsm["15:00"],
        settled_by_eod_lsm_pct=throughput_lsm["18:00"],
        per_bank_bcbs248=per_bank_metrics,
        hourly_timeline=hourly_timeline,
        collateral_optimization=opt,
        floor_violations=fifo_full.floor_violations + lsm_full.floor_violations,
    )
