"""
CLI entrypoint for liqcopilot: `bench`, `stress`, `serve` (settings: LIQCOPILOT_* environment
variables) and `migrate` (apply database migrations and exit).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .bench import format_benchmark_report, run_all_benchmarks
from .stress import simulate_bcbs248_stress


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="liqcopilot",
        description="ISO 20022 RTGS Liquidity Copilot & Intraday Stress Simulator",
    )
    sub = parser.add_subparsers(dest="command")

    bench_p = sub.add_parser("bench", help="Run 8-scenario RTGS gridlock & stress benchmark")
    bench_p.add_argument("--json", action="store_true", help="Output JSON benchmark summary")

    serve_p = sub.add_parser("serve", help="Start FastAPI server")
    serve_p.add_argument("--host", help="Bind host (default LIQCOPILOT_HOST or 127.0.0.1; others need API keys)")
    serve_p.add_argument("--port", type=int, help="Bind port (default LIQCOPILOT_PORT or 8013)")
    sub.add_parser("migrate", help="Apply database migrations to LIQCOPILOT_DATABASE_URL")

    stress_p = sub.add_parser("stress", help="Run BCBS 248 intraday liquidity stress scenario")
    stress_p.add_argument(
        "--scenario",
        default="normal",
        choices=[
            "normal",
            "counterparty_default",
            "liquidity_hoarding",
            "collateral_haircut_shock",
        ],
    )

    args = parser.parse_args(argv)
    # The report uses an em dash, which a cp1252 Windows console prints as garbage
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.command in ("serve", "migrate"):
        from . import db, ops

        for name, val in (("HOST", getattr(args, "host", None)), ("PORT", getattr(args, "port", None))):
            if val is not None:
                os.environ[f"LIQCOPILOT_{name}"] = str(val)
        try:
            settings = ops.load_settings()  # fail here, before uvicorn, with a readable message
        except ops.ConfigError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return 2
        if args.command == "migrate":
            print(*(db.migrate(db.engine(settings.database_url)) or ["up to date"]), sep="\n")
            return 0
        import uvicorn

        ops.setup_logging(settings.log_level)
        uvicorn.run("liqcopilot.api:app", host=settings.host, port=settings.port, log_config=None, access_log=False)
        return 0

    if args.command == "stress":
        report = simulate_bcbs248_stress(scenario=args.scenario)
        print(json.dumps(report.to_dict(), indent=2, default=str))  # money is Decimal
        return 0

    # Default or "bench" command
    data = run_all_benchmarks()
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(format_benchmark_report(data))

    assertions = data["summary"]["assertions"]
    if not all(assertions.values()):
        print(f"Benchmark assertions failed: {assertions}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
