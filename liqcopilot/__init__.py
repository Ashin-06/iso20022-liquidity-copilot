"""
liqcopilot: Intraday Liquidity Stress Simulator & ISO 20022 RTGS Payment Gridlock Resolver.
"""

__version__ = "1.0.0"

from .iso20022 import (
    ISO20022Payment,
    ISO20022ValidationError,
    parse_iso20022_xml,
    generate_iso20022_xml,
    generate_pacs008_xml,
    generate_pacs009_xml,
    generate_camt054_xml,
    validate_bic_or_ifsc,
    validate_iso20022_payment,
)
from .gridlock import (
    BankNode,
    QueuedPayment,
    SettlementResult,
    detect_payment_cycles,
    settle_fifo,
    settle_bilateral,
    settle_multilateral_lsm,
    compare_settlement_engines,
)
from .stress import (
    BCBS248Report,
    RepoDelayOptimization,
    simulate_bcbs248_stress,
    optimize_collateral_vs_delay,
)
from .bench import (
    get_benchmark_scenarios,
    run_all_benchmarks,
    format_benchmark_report,
)

__all__ = [
    "ISO20022Payment",
    "ISO20022ValidationError",
    "parse_iso20022_xml",
    "generate_iso20022_xml",
    "generate_pacs008_xml",
    "generate_pacs009_xml",
    "generate_camt054_xml",
    "validate_bic_or_ifsc",
    "validate_iso20022_payment",
    "BankNode",
    "QueuedPayment",
    "SettlementResult",
    "detect_payment_cycles",
    "settle_fifo",
    "settle_bilateral",
    "settle_multilateral_lsm",
    "compare_settlement_engines",
    "BCBS248Report",
    "RepoDelayOptimization",
    "simulate_bcbs248_stress",
    "optimize_collateral_vs_delay",
    "get_benchmark_scenarios",
    "run_all_benchmarks",
    "format_benchmark_report",
]
