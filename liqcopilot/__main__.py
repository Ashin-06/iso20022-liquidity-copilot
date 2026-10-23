"""
CLI entrypoint for liqcopilot (`python -m liqcopilot bench` | `python -m liqcopilot serve --port 8013`).
"""

from __future__ import annotations

import argparse
import json
import sys
import uvicorn

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
    serve_p.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve_p.add_argument("--port", type=int, default=8013, help="Bind port (default: 8013)")

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

    if args.command == "serve":
        uvicorn.run("liqcopilot.api:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.command == "stress":
        report = simulate_bcbs248_stress(scenario=args.scenario)
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    # Default or "bench" command
    data = run_all_benchmarks()
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(format_benchmark_report(data))

    assertions = data["summary"]["assertions"]
    if not all(assertions.values()):
        print(f"Benchmark assertions failed: {assertions}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
