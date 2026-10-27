"""
Interbank RTGS Payment Gridlock Resolver & Liquidity-Saving Mechanism (LSM) Engines.

Models an N-bank RTGS network where each bank i has:
- Opening central-bank reserve balance B_i
- Regulatory minimum reserve floor F_i (available buffer = B_i - F_i >= 0)
- A queue Q of pending high-value ISO 20022 payments

Implements three settlement engines:
1. FIFO Gross Settlement (`settle_fifo`)
2. Bilateral Offsetting (`settle_bilateral`)
3. Multilateral Cycle-Netting LSM Solver (`settle_multilateral_lsm`)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from .iso20022 import ISO20022Payment, as_money, generate_camt054_xml, validate_bic_or_ifsc

ZERO = Decimal(0)
CENT = Decimal("0.01")

PRIORITY_RANK: Dict[str, int] = {
    "URGT": 0,
    "HIGH": 1,
    "NORM": 2,
}
MAX_CYCLES = 200
MAX_CYCLE_EXPANSIONS = 100_000
EXACT_SEARCH_MAX_PAYMENTS = 28
EXACT_SEARCH_MAX_NODES = 15_000


@dataclass
class BankNode:
    """Participant bank in the central bank RTGS network."""

    bic: str
    name: str
    opening_balance: Decimal
    reserve_floor: Decimal

    def __post_init__(self) -> None:
        self.bic = validate_bic_or_ifsc(self.bic)
        try:
            self.opening_balance, self.reserve_floor = as_money(self.opening_balance), as_money(self.reserve_floor)
        except ValueError:
            raise ValueError(f"Balance and floor must be finite numbers for {self.bic}.") from None
        if self.reserve_floor < 0:
            raise ValueError(f"Reserve floor F_i must be non-negative (got {self.reserve_floor}).")
        if self.opening_balance < self.reserve_floor:
            raise ValueError(
                f"Opening balance B_i ({self.opening_balance}) cannot be below "
                f"reserve floor F_i ({self.reserve_floor}) for {self.bic}."
            )

    @property
    def available_liquidity(self) -> Decimal:
        return max(ZERO, self.opening_balance - self.reserve_floor)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bic": self.bic,
            "name": self.name,
            "opening_balance": round(self.opening_balance, 2),
            "reserve_floor": round(self.reserve_floor, 2),
            "available_liquidity": round(self.available_liquidity, 2),
        }


@dataclass
class QueuedPayment:
    """High-value ISO 20022 payment waiting in the central RTGS queue."""

    payment_id: str
    sender: str
    receiver: str
    amount: Decimal
    priority: str = "NORM"
    arrival_order: int = 0
    timestamp: str = "2026-10-22T09:30:00Z"
    msg_type: str = "pacs.008.001.08"
    currency: str = "INR"

    def __post_init__(self) -> None:
        self.priority = self.priority.strip().upper()
        if self.priority not in PRIORITY_RANK:
            raise ValueError(f"Invalid priority '{self.priority}'.")
        # NaN slips through a plain `<= 0` check and then poisons every balance it touches
        try:
            self.amount = as_money(self.amount)
        except ValueError:
            raise ValueError(f"Payment amount must be a positive finite number (got {self.amount!r}).") from None
        if self.amount <= 0:
            raise ValueError(f"Payment amount must be a positive finite number (got {self.amount}).")
        self.sender = validate_bic_or_ifsc(self.sender)
        self.receiver = validate_bic_or_ifsc(self.receiver)
        try:
            datetime.fromisoformat(self.timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Timestamp must be ISO 8601 (got {self.timestamp!r}).") from exc
        if self.sender == self.receiver:
            raise ValueError(f"Sender and receiver cannot be identical ('{self.sender}').")

    @property
    def sort_key(self) -> Tuple[int, int, str]:
        return (PRIORITY_RANK[self.priority], self.arrival_order, self.payment_id)

    def to_iso20022_payment(self) -> ISO20022Payment:
        return ISO20022Payment(
            msg_id=f"MSG-{self.payment_id}",
            end_to_end_id=self.payment_id,
            debtor_agent=self.sender,
            creditor_agent=self.receiver,
            amount=self.amount,
            currency=self.currency,
            priority=self.priority,
            settlement_timestamp=self.timestamp,
            msg_type=self.msg_type,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SettlementResult:
    """Comprehensive outcome of running an RTGS settlement engine on a queue Q."""

    engine: str
    settled_payments: List[QueuedPayment]
    unsettled_payments: List[QueuedPayment]
    settled_count: int
    total_count: int
    settled_volume: Decimal
    total_volume: Decimal
    volume_clearing_ratio: float
    count_clearing_ratio: float
    opening_balances: Dict[str, Decimal]
    final_balances: Dict[str, Decimal]
    reserve_floors: Dict[str, Decimal]
    floor_violations: int
    peak_liquidity_used: Decimal
    gross_liquidity_needed: Decimal
    liquidity_savings_vs_gross_pct: float
    cycles_detected: int
    cycle_paths: List[List[str]] = field(default_factory=list)
    audit_trail: List[Dict[str, Any]] = field(default_factory=list)
    camt054_notifications: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "settled_count": self.settled_count,
            "total_count": self.total_count,
            "settled_volume": round(self.settled_volume, 2),
            "total_volume": round(self.total_volume, 2),
            "volume_clearing_ratio": round(self.volume_clearing_ratio, 4),
            "count_clearing_ratio": round(self.count_clearing_ratio, 4),
            "opening_balances": {k: round(v, 2) for k, v in self.opening_balances.items()},
            "final_balances": {k: round(v, 2) for k, v in self.final_balances.items()},
            "reserve_floors": {k: round(v, 2) for k, v in self.reserve_floors.items()},
            "floor_violations": self.floor_violations,
            "peak_liquidity_used": round(self.peak_liquidity_used, 2),
            "gross_liquidity_needed": round(self.gross_liquidity_needed, 2),
            "liquidity_savings_vs_gross_pct": round(self.liquidity_savings_vs_gross_pct, 2),
            "cycles_detected": self.cycles_detected,
            "cycle_paths": self.cycle_paths,
            "settled_payments": [p.to_dict() for p in self.settled_payments],
            "unsettled_payments": [p.to_dict() for p in self.unsettled_payments],
            "audit_trail": self.audit_trail,
            "camt054_count": len(self.camt054_notifications),
        }


def _check_network(banks: List[BankNode], payments: List[QueuedPayment]) -> None:
    """Reject inputs the engines would otherwise mis-account: unknown or duplicate banks, duplicate IDs."""
    bics = [b.bic for b in banks]
    if len(set(bics)) != len(bics):
        raise ValueError("Duplicate bank BIC in the network.")
    ids = [p.payment_id for p in payments]
    if len(set(ids)) != len(ids):
        # Engines track settled payments by ID; a duplicate makes one of them vanish
        raise ValueError("Duplicate payment_id in the queue.")
    unknown = {bic for p in payments for bic in (p.sender, p.receiver)} - set(bics)
    if unknown:
        raise ValueError(f"Payments reference banks not in the network: {sorted(unknown)}")


def detect_payment_cycles(payments: List[QueuedPayment]) -> List[List[str]]:
    """
    Detect elementary directed cycles (up to 8 banks long) in the queued payment obligation graph.
    Returns a deduplicated list of cycle node sequences [B_1, B_2, ..., B_k, B_1].

    The number of cycles grows exponentially with network density, so the search stops after
    MAX_CYCLES cycles or MAX_CYCLE_EXPANSIONS path extensions. It is diagnostic only; the LSM
    solver does not depend on it.
    """
    expansions = 0
    adj: Dict[str, Set[str]] = {}
    nodes: Set[str] = set()
    for p in payments:
        adj.setdefault(p.sender, set()).add(p.receiver)
        nodes.add(p.sender)
        nodes.add(p.receiver)

    ordered_nodes = sorted(nodes)
    found_canonical: Set[Tuple[str, ...]] = set()
    cycles: List[List[str]] = []

    for start_node in ordered_nodes:
        stack: List[Tuple[str, List[str], Set[str]]] = [
            (start_node, [start_node], {start_node})
        ]
        while stack:
            if len(cycles) >= MAX_CYCLES or expansions >= MAX_CYCLE_EXPANSIONS:
                break
            curr, path, visited = stack.pop()
            expansions += 1
            for nxt in sorted(adj.get(curr, set())):
                if nxt == start_node and len(path) >= 2:
                    # Canonicalize cycle rotation by smallest BIC first
                    min_idx = min(range(len(path)), key=lambda i: path[i])
                    canonical = tuple(path[min_idx:] + path[:min_idx])
                    if canonical not in found_canonical:
                        found_canonical.add(canonical)
                        cycles.append(list(canonical) + [canonical[0]])
                elif nxt > start_node and nxt not in visited and len(path) < 8:
                    stack.append((nxt, path + [nxt], visited | {nxt}))

    cycles.sort(key=lambda c: (len(c), c))
    return cycles


def _build_settlement_result(
    engine: str,
    banks: List[BankNode],
    all_payments: List[QueuedPayment],
    settled: List[QueuedPayment],
    unsettled: List[QueuedPayment],
    final_balances: Dict[str, Decimal],
    peak_liquidity_used: Decimal,
    cycles: List[List[str]],
    audit_trail: List[Dict[str, Any]],
    generate_notifications: bool = True,
) -> SettlementResult:
    opening_balances = {b.bic: b.opening_balance for b in banks}
    reserve_floors = {b.bic: b.reserve_floor for b in banks}

    floor_violations = sum(
        1
        for bic, bal in final_balances.items()
        if bal < reserve_floors[bic]
    )

    settled_vol = sum(p.amount for p in settled)
    total_vol = sum(p.amount for p in all_payments)
    vol_ratio = float(settled_vol / total_vol) if total_vol > 0 else 1.0
    cnt_ratio = (len(settled) / len(all_payments)) if all_payments else 1.0

    # Gross liquidity needed to settle all queued obligations without incoming offsets
    gross_out_by_bank: Dict[str, Decimal] = {b.bic: ZERO for b in banks}
    for p in all_payments:
        gross_out_by_bank[p.sender] = gross_out_by_bank.get(p.sender, ZERO) + p.amount
    gross_liquidity_needed = sum(gross_out_by_bank.values())

    # Compare peak liquidity used vs gross outbound volume of settled payments
    gross_settled_vol = settled_vol if settled_vol > 0 else gross_liquidity_needed
    savings_pct = (
        max(0.0, (1.0 - float(peak_liquidity_used / gross_settled_vol)) * 100.0)
        if gross_settled_vol > 0
        else 0.0
    )

    notifications: List[str] = []
    if generate_notifications:
        for p in settled[:10]:  # Cap XML generation for speed while keeping real camt.054s
            iso_pmt = p.to_iso20022_payment()
            notifications.append(generate_camt054_xml(iso_pmt, credit_debit="CRDT", status="BOOK"))

    return SettlementResult(
        engine=engine,
        settled_payments=settled,
        unsettled_payments=unsettled,
        settled_count=len(settled),
        total_count=len(all_payments),
        settled_volume=settled_vol,
        total_volume=total_vol,
        volume_clearing_ratio=vol_ratio,
        count_clearing_ratio=cnt_ratio,
        opening_balances=opening_balances,
        final_balances=final_balances,
        reserve_floors=reserve_floors,
        floor_violations=floor_violations,
        peak_liquidity_used=peak_liquidity_used,
        gross_liquidity_needed=gross_liquidity_needed,
        liquidity_savings_vs_gross_pct=savings_pct,
        cycles_detected=len(cycles),
        cycle_paths=cycles,
        audit_trail=audit_trail,
        camt054_notifications=notifications,
    )


def settle_fifo(
    banks: List[BankNode],
    payments: List[QueuedPayment],
    strict_global_hol: bool = True,
) -> SettlementResult:
    """
    Engine 1: FIFO Gross Settlement.

    Settles payments strictly in arrival/priority order when available liquidity
    B_sender - F_sender >= amount. Stalls when the head-of-line payment is blocked.
    """
    _check_network(banks, payments)
    balances: Dict[str, Decimal] = {b.bic: b.opening_balance for b in banks}
    floors: Dict[str, Decimal] = {b.bic: b.reserve_floor for b in banks}
    opening: Dict[str, Decimal] = dict(balances)

    ordered = sorted(payments, key=lambda p: p.sort_key)
    settled: List[QueuedPayment] = []
    unsettled: List[QueuedPayment] = []
    audit_trail: List[Dict[str, Any]] = []
    peak_system_drawdown = ZERO

    if strict_global_hol:
        stalled = False
        for idx, p in enumerate(ordered):
            avail = balances[p.sender] - floors[p.sender]
            if not stalled and avail >= p.amount:
                balances[p.sender] -= p.amount
                balances[p.receiver] += p.amount
                settled.append(p)
                curr_drawdown = sum(
                    max(ZERO, opening[bic] - balances[bic]) for bic in balances
                )
                peak_system_drawdown = max(peak_system_drawdown, curr_drawdown)
                audit_trail.append(
                    {
                        "step": idx + 1,
                        "action": "SETTLED_FIFO_GROSS",
                        "payment_id": p.payment_id,
                        "sender": p.sender,
                        "receiver": p.receiver,
                        "amount": p.amount,
                        "sender_available_after": round(
                            balances[p.sender] - floors[p.sender], 2
                        ),
                    }
                )
            else:
                if not stalled:
                    stalled = True
                    audit_trail.append(
                        {
                            "step": idx + 1,
                            "action": "HOL_BLOCKED",
                            "payment_id": p.payment_id,
                            "sender": p.sender,
                            "receiver": p.receiver,
                            "amount": p.amount,
                            "sender_available": round(avail, 2),
                            "shortfall": round(p.amount - avail, 2),
                        }
                    )
                unsettled.append(p)
    else:
        # Per-sender head-of-line FIFO blocking
        pending = list(ordered)
        progress = True
        step = 0
        while progress and pending:
            progress = False
            blocked_senders: Set[str] = set()
            next_pending: List[QueuedPayment] = []
            for p in pending:
                if p.sender in blocked_senders:
                    next_pending.append(p)
                    continue
                avail = balances[p.sender] - floors[p.sender]
                if avail >= p.amount:
                    step += 1
                    balances[p.sender] -= p.amount
                    balances[p.receiver] += p.amount
                    settled.append(p)
                    progress = True
                    curr_drawdown = sum(
                        max(ZERO, opening[bic] - balances[bic]) for bic in balances
                    )
                    peak_system_drawdown = max(peak_system_drawdown, curr_drawdown)
                    audit_trail.append(
                        {
                            "step": step,
                            "action": "SETTLED_FIFO_GROSS",
                            "payment_id": p.payment_id,
                            "sender": p.sender,
                            "receiver": p.receiver,
                            "amount": p.amount,
                        }
                    )
                else:
                    blocked_senders.add(p.sender)
                    next_pending.append(p)
            pending = next_pending
        unsettled = pending

    cycles = detect_payment_cycles(payments)
    return _build_settlement_result(
        engine="fifo",
        banks=banks,
        all_payments=ordered,
        settled=settled,
        unsettled=unsettled,
        final_balances=balances,
        peak_liquidity_used=peak_system_drawdown,
        cycles=cycles,
        audit_trail=audit_trail,
    )


def settle_bilateral(
    banks: List[BankNode],
    payments: List[QueuedPayment],
) -> SettlementResult:
    """
    Engine 2: Bilateral Offsetting.

    Pairs opposite queued obligations between (A, B) and (B, A) and settles the
    net difference if within both banks' available liquidity, interleaved with
    gross settlement of unblocked head-of-line obligations.
    """
    _check_network(banks, payments)
    balances: Dict[str, Decimal] = {b.bic: b.opening_balance for b in banks}
    floors: Dict[str, Decimal] = {b.bic: b.reserve_floor for b in banks}
    opening: Dict[str, Decimal] = dict(balances)

    ordered = sorted(payments, key=lambda p: p.sort_key)
    pending: List[QueuedPayment] = list(ordered)
    settled: List[QueuedPayment] = []
    audit_trail: List[Dict[str, Any]] = []
    peak_system_drawdown = ZERO
    step = 0

    progress = True
    while progress and pending:
        progress = False

        # 1. Try bilateral offsetting between all bank pairs (A, B) with opposite flows
        by_pair: Dict[Tuple[str, str], List[QueuedPayment]] = {}
        for p in pending:
            by_pair.setdefault((p.sender, p.receiver), []).append(p)

        visited_unordered: Set[Tuple[str, str]] = set()
        settled_ids_in_round: Set[str] = set()

        for (a, b), ab_list in list(by_pair.items()):
            pair_key = (min(a, b), max(a, b))
            if pair_key in visited_unordered:
                continue
            visited_unordered.add(pair_key)

            ba_list = by_pair.get((b, a), [])
            if not ab_list or not ba_list:
                continue

            # Filter out any already settled in this pass
            ab_cand = [p for p in ab_list if p.payment_id not in settled_ids_in_round]
            ba_cand = [p for p in ba_list if p.payment_id not in settled_ids_in_round]
            if not ab_cand or not ba_cand:
                continue

            # Try matching best non-empty prefix combinations (prioritizing head of queue)
            best_pair_batch: Optional[Tuple[List[QueuedPayment], List[QueuedPayment], float]] = None
            best_vol = ZERO

            for i in range(1, len(ab_cand) + 1):
                sub_ab = ab_cand[:i]
                sum_ab = sum(p.amount for p in sub_ab)
                for j in range(1, len(ba_cand) + 1):
                    sub_ba = ba_cand[:j]
                    sum_ba = sum(p.amount for p in sub_ba)
                    net_a_to_b = sum_ab - sum_ba  # positive => A pays net to B
                    avail_a = balances[a] - floors[a]
                    avail_b = balances[b] - floors[b]
                    feasible = (
                        (net_a_to_b >= 0 and avail_a >= net_a_to_b)
                        or (net_a_to_b < 0 and avail_b >= -net_a_to_b)
                    )
                    if feasible:
                        vol = sum_ab + sum_ba
                        if vol > best_vol:
                            best_vol = vol
                            best_pair_batch = (sub_ab, sub_ba, net_a_to_b)

            if best_pair_batch is not None:
                sub_ab, sub_ba, net_a_to_b = best_pair_batch
                balances[a] -= net_a_to_b
                balances[b] += net_a_to_b
                for p in sub_ab + sub_ba:
                    settled.append(p)
                    settled_ids_in_round.add(p.payment_id)
                step += 1
                curr_drawdown = sum(
                    max(ZERO, opening[bic] - balances[bic]) for bic in balances
                )
                peak_system_drawdown = max(peak_system_drawdown, curr_drawdown)
                audit_trail.append(
                    {
                        "step": step,
                        "action": "BILATERAL_OFFSET",
                        "bank_a": a,
                        "bank_b": b,
                        "gross_volume": round(best_vol, 2),
                        "net_settlement": round(abs(net_a_to_b), 2),
                        "payments_cleared": [p.payment_id for p in sub_ab + sub_ba],
                    }
                )
                progress = True

        if settled_ids_in_round:
            pending = [p for p in pending if p.payment_id not in settled_ids_in_round]

        # 2. Try settling global head-of-line payment if feasible
        if pending:
            head = pending[0]
            avail = balances[head.sender] - floors[head.sender]
            if avail >= head.amount:
                balances[head.sender] -= head.amount
                balances[head.receiver] += head.amount
                settled.append(head)
                pending.pop(0)
                step += 1
                curr_drawdown = sum(
                    max(ZERO, opening[bic] - balances[bic]) for bic in balances
                )
                peak_system_drawdown = max(peak_system_drawdown, curr_drawdown)
                audit_trail.append(
                    {
                        "step": step,
                        "action": "BILATERAL_GROSS_HEAD",
                        "payment_id": head.payment_id,
                        "sender": head.sender,
                        "receiver": head.receiver,
                        "amount": head.amount,
                    }
                )
                progress = True

    settled.sort(key=lambda p: p.sort_key)
    cycles = detect_payment_cycles(payments)
    return _build_settlement_result(
        engine="bilateral",
        banks=banks,
        all_payments=ordered,
        settled=settled,
        unsettled=pending,
        final_balances=balances,
        peak_liquidity_used=peak_system_drawdown,
        cycles=cycles,
        audit_trail=audit_trail,
    )


def _evaluate_subset_feasibility(
    avail_buffers: Dict[str, Decimal],
    payments: List[QueuedPayment],
    active_indices: Set[int],
) -> Tuple[bool, Dict[str, Decimal]]:
    """
    Compute net post-batch available buffer for each bank i:
    avail_i(S) = (B_i - F_i) + sum_{p in S, to=i} a_p - sum_{p in S, from=i} a_p
    Returns (is_feasible, post_avail_by_bank).
    """
    post_avail = dict(avail_buffers)
    for idx in active_indices:
        p = payments[idx]
        post_avail[p.sender] = post_avail.get(p.sender, ZERO) - p.amount
        post_avail[p.receiver] = post_avail.get(p.receiver, ZERO) + p.amount

    feasible = all(val >= 0 for val in post_avail.values())
    return feasible, post_avail


def settle_multilateral_lsm(
    banks: List[BankNode],
    payments: List[QueuedPayment],
) -> SettlementResult:
    """
    Engine 3: Multilateral Cycle-Netting LSM Solver (Liquidity-Saving Mechanism).

    Looks for the maximum-value subset of queued payments S <= Q such that for every bank i:
        B_i + sum_{p in S, to=i} a_p - sum_{p in S, from=i} a_p >= F_i
    This is a multi-constraint knapsack problem (NP-hard). The result is proven optimal only
    when the exact search runs to completion; the batch audit entry says which case applies.

    Algorithm:
    1. Detect directed payment cycles in Q for topological diagnostics.
    2. Iterative Constrained Subset Pruning: start with S = Q and iteratively prune
       outbound edges from the most liquidity-deficient bank until all banks satisfy
       their reserve floor constraint.
    3. Branch-and-Bound: exact search over include/exclude decisions for queues of up to
       EXACT_SEARCH_MAX_PAYMENTS payments, stopped after EXACT_SEARCH_MAX_NODES nodes.
    4. Atomic Batch Execution: settle all p in S* simultaneously in a single atomic batch.
    """
    _check_network(banks, payments)
    ordered = sorted(payments, key=lambda p: p.sort_key)
    n = len(ordered)
    avail_buffers: Dict[str, Decimal] = {
        b.bic: b.opening_balance - b.reserve_floor for b in banks
    }
    cycles = detect_payment_cycles(ordered)
    audit_trail: List[Dict[str, Any]] = []

    if cycles:
        audit_trail.append(
            {
                "step": 1,
                "action": "CYCLE_DETECTION",
                "cycles_found": len(cycles),
                "sample_cycles": [" -> ".join(c) for c in cycles[:5]],
            }
        )

    # Phase 1: Iterative Constrained Subset Pruning (Bech-Soramaki / Guntzer-Jungnickel)
    active: Set[int] = set(range(n))
    prune_step = 0
    while active:
        feasible, post_avail = _evaluate_subset_feasibility(avail_buffers, ordered, active)
        if feasible:
            break

        # Find the bank with the largest liquidity deficit (most negative post_avail)
        deficient_bic, deficit_val = min(post_avail.items(), key=lambda kv: kv[1])
        deficit_needed = -deficit_val

        # Outbound payments from deficient_bic currently in active set
        outbound_indices = [
            idx for idx in active if ordered[idx].sender == deficient_bic
        ]
        if not outbound_indices:
            break

        # Prefer removing lower-priority payments whose amount best covers deficit_needed
        def _prune_score(idx: int, deficit_needed: Decimal = deficit_needed) -> Tuple[int, int, Decimal, int]:
            p = ordered[idx]
            pr_rank = PRIORITY_RANK[p.priority]
            covers_deficit = 1 if p.amount >= deficit_needed else 0
            # Higher pr_rank (NORM=2 > URGT=0) pruned first; if covers_deficit, pick smallest overshoot
            overshoot = abs(p.amount - deficit_needed)
            return (-pr_rank, -covers_deficit, overshoot, -p.arrival_order)

        victim_idx = min(outbound_indices, key=_prune_score)
        active.remove(victim_idx)
        prune_step += 1
        victim = ordered[victim_idx]
        audit_trail.append(
            {
                "step": len(audit_trail) + 1,
                "action": "PRUNE_DEFICIENT_OUTBOUND",
                "deficient_bank": deficient_bic,
                "deficit_before_prune": round(deficit_needed, 2),
                "removed_payment_id": victim.payment_id,
                "removed_amount": victim.amount,
                "priority": victim.priority,
            }
        )

    # Phase 2: Greedy Re-insertion of any excluded payments that are now feasible
    excluded = [idx for idx in range(n) if idx not in active]
    for idx in excluded:
        cand = active | {idx}
        ok, _ = _evaluate_subset_feasibility(avail_buffers, ordered, cand)
        if ok:
            active = cand

    best_indices: Set[int] = set(active)
    best_volume: Decimal = sum(ordered[i].amount for i in best_indices)

    # Phase 3: Exact Branch-and-Bound for optimal subset S*
    node_visits = 0
    max_visits = EXACT_SEARCH_MAX_NODES
    if n <= EXACT_SEARCH_MAX_PAYMENTS:
        # Sort candidate indices by descending amount so branch-and-bound finds high-value subsets fast
        search_order = sorted(
            range(n),
            key=lambda i: (ordered[i].amount, -PRIORITY_RANK[ordered[i].priority]),
            reverse=True,
        )
        suffix_rem = [ZERO] * (n + 1)
        for pos in range(n - 1, -1, -1):
            suffix_rem[pos] = suffix_rem[pos + 1] + ordered[search_order[pos]].amount

        # Maximum possible inflow to each bank from remaining items
        rem_inflow: List[Dict[str, Decimal]] = [{} for _ in range(n + 1)]
        for b in banks:
            rem_inflow[n][b.bic] = ZERO
        for pos in range(n - 1, -1, -1):
            p = ordered[search_order[pos]]
            rem_inflow[pos] = dict(rem_inflow[pos + 1])
            rem_inflow[pos][p.receiver] = rem_inflow[pos].get(p.receiver, ZERO) + p.amount

        def _dfs(
            pos: int,
            curr_vol: Decimal,
            curr_avail: Dict[str, Decimal],
            chosen: List[int],
        ) -> None:
            nonlocal best_volume, best_indices, node_visits
            node_visits += 1
            if node_visits > max_visits:
                return

            if curr_vol + suffix_rem[pos] <= best_volume:
                return

            # Prune if any bank cannot recover its reserve floor even with all remaining inflows
            for bic, av in curr_avail.items():
                if av + rem_inflow[pos].get(bic, ZERO) < 0:
                    return

            if pos == n:
                if curr_vol > best_volume:
                    best_volume = curr_vol
                    best_indices = set(chosen)
                return

            idx = search_order[pos]
            p = ordered[idx]

            # Branch 1: Include payment idx
            curr_avail[p.sender] -= p.amount
            curr_avail[p.receiver] += p.amount
            chosen.append(idx)
            _dfs(pos + 1, curr_vol + p.amount, curr_avail, chosen)
            chosen.pop()
            curr_avail[p.sender] += p.amount
            curr_avail[p.receiver] -= p.amount

            # Branch 2: Exclude payment idx
            _dfs(pos + 1, curr_vol, curr_avail, chosen)

        _dfs(0, ZERO, dict(avail_buffers), [])

    if n > EXACT_SEARCH_MAX_PAYMENTS:
        search = "heuristic only: exact search skipped above 28 payments"
    elif node_visits > max_visits:
        search = "node budget reached: best subset found, not proven optimal"
    else:
        search = "exhaustive: proven optimal"

    # Phase 4: Execute Atomic Multilateral Batch Settlement
    final_balances: Dict[str, Decimal] = {b.bic: b.opening_balance for b in banks}
    settled: List[QueuedPayment] = []
    unsettled: List[QueuedPayment] = []

    for idx, p in enumerate(ordered):
        if idx in best_indices:
            settled.append(p)
            final_balances[p.sender] -= p.amount
            final_balances[p.receiver] += p.amount
        else:
            unsettled.append(p)

    # In an atomic multilateral LSM batch, all offsetting legs settle simultaneously;
    # the peak liquidity drawn from central bank reserves is the net batch drawdown.
    net_batch_drawdown = sum(
        max(ZERO, b.opening_balance - final_balances[b.bic]) for b in banks
    )

    audit_trail.append(
        {
            "step": len(audit_trail) + 1,
            "action": "ATOMIC_LSM_BATCH_SETTLED",
            "settled_count": len(settled),
            "settled_volume": round(best_volume, 2),
            "net_liquidity_drawn": round(net_batch_drawdown, 2),
            "gross_to_net_compression_ratio": round(
                best_volume / max(net_batch_drawdown, Decimal(1)), 2
            ),
            "search": search,
            "proven_optimal": search.startswith("exhaustive"),
        }
    )

    return _build_settlement_result(
        engine="multilateral_lsm",
        banks=banks,
        all_payments=ordered,
        settled=settled,
        unsettled=unsettled,
        final_balances=final_balances,
        peak_liquidity_used=net_batch_drawdown,
        cycles=cycles,
        audit_trail=audit_trail,
    )


def gross_sequential_peak(
    banks: List[BankNode],
    payments: List[QueuedPayment],
) -> Decimal:
    """
    Upper-bound liquidity need (Bech & Soramaki): the peak system-wide reserve drawdown
    required to settle `payments` one-by-one in FIFO priority/arrival order under pure
    gross RTGS with unlimited intraday credit (no offsetting, no floor stalls).

    For any payment set S, the atomic multilateral net drawdown of S is a point on this
    sequential path (its end state), so  net_drawdown(S) <= gross_sequential_peak(S).
    """
    opening: Dict[str, Decimal] = {b.bic: b.opening_balance for b in banks}
    balances: Dict[str, Decimal] = dict(opening)
    peak = ZERO
    for p in sorted(payments, key=lambda q: q.sort_key):
        balances[p.sender] = balances.get(p.sender, ZERO) - p.amount
        balances[p.receiver] = balances.get(p.receiver, ZERO) + p.amount
        draw = sum(max(ZERO, opening.get(bic, ZERO) - bal) for bic, bal in balances.items())
        peak = max(peak, draw)
    return peak


def compare_settlement_engines(
    banks: List[BankNode],
    payments: List[QueuedPayment],
) -> Dict[str, Any]:
    """Run FIFO Gross, Bilateral Offset, and Multilateral LSM on the same RTGS network."""
    fifo_res = settle_fifo(banks, payments)
    bilat_res = settle_bilateral(banks, payments)
    lsm_res = settle_multilateral_lsm(banks, payments)

    # Peak liquidity comparison on an identical payment set (apples-to-apples):
    # liquidity a FIFO gross RTGS would need to settle exactly what the LSM settled,
    # versus the LSM's atomic net batch drawdown.
    fifo_gross_peak_same_set = gross_sequential_peak(banks, lsm_res.settled_payments)
    if fifo_gross_peak_same_set > 0:
        peak_cut_vs_fifo_pct = float(
            (fifo_gross_peak_same_set - lsm_res.peak_liquidity_used)
            / fifo_gross_peak_same_set
        ) * 100.0
    else:
        peak_cut_vs_fifo_pct = 0.0

    return {
        "fifo": fifo_res.to_dict(),
        "bilateral": bilat_res.to_dict(),
        "multilateral_lsm": lsm_res.to_dict(),
        "comparison": {
            "fifo_volume_cleared_pct": round(fifo_res.volume_clearing_ratio * 100.0, 2),
            "bilateral_volume_cleared_pct": round(bilat_res.volume_clearing_ratio * 100.0, 2),
            "lsm_volume_cleared_pct": round(lsm_res.volume_clearing_ratio * 100.0, 2),
            "lsm_vs_fifo_volume_gain_pct": round(
                (lsm_res.volume_clearing_ratio - fifo_res.volume_clearing_ratio) * 100.0, 2
            ),
            "fifo_peak_liquidity_used": round(fifo_res.peak_liquidity_used, 2),
            "fifo_gross_peak_same_set": round(fifo_gross_peak_same_set, 2),
            "lsm_peak_liquidity_used": round(lsm_res.peak_liquidity_used, 2),
            "peak_liquidity_reduction_vs_fifo_pct": round(peak_cut_vs_fifo_pct, 2),
            "peak_liquidity_reduction_vs_gross_pct": round(
                lsm_res.liquidity_savings_vs_gross_pct, 2
            ),
            "zero_floor_violations": (
                fifo_res.floor_violations == 0
                and bilat_res.floor_violations == 0
                and lsm_res.floor_violations == 0
            ),
        },
    }
