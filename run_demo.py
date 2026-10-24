"""One-command runner for Output 6 - Logistics and Network Design.

    python run_demo.py --modules all
    python run_demo.py --modules data ml routing
    python run_demo.py --modules all --fast          # shorter solver budgets for a quick check
    python run_demo.py --modules all --export-reports --open-dashboard

Modules run in dependency order: data -> ml -> routing -> warehouse ->
network -> simulation -> fleet -> integration. ``--export-reports`` then builds
the BI star schema, the dashboard payload and the Excel workbook;
``--open-dashboard`` launches the local Dash app on http://127.0.0.1:8050. Each module can also be run on
its own, e.g. ``python -m src.logistics.vrp_optimizer``.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time

from src.shared.config import load_config
from src.shared.logging_utils import get_logger

log = get_logger("run_demo")

PIPELINE: list[tuple[str, str]] = [
    ("data", "src.logistics.data_generator"),
    ("ml", "src.logistics.ml_models"),
    ("routing", "src.logistics.vrp_optimizer"),
    ("warehouse", "src.logistics.warehouse_slotter"),
    ("network", "src.logistics.network_designer"),
    ("simulation", "src.logistics.simulation_engine"),
    ("fleet", "src.logistics.fleet_sizer"),
    ("integration", "src.logistics.integration"),
]
REPORTS: list[tuple[str, str]] = [
    ("bi", "src.logistics.reporting.bi_export"),
    ("payload", "src.logistics.reporting.payload"),
    ("excel", "src.logistics.reporting.excel_report"),
]
FAST = {"routing.time_limit_s": 8, "fleet_sizing.time_limit_s": 2, "simulation.replications": 40,
        "simulation.search_replications": 10, "fleet_sizing.demand_multipliers": [0.5, 1.0, 2.0]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modules", nargs="+", default=["all"], help="all or any of: " + ", ".join(n for n, _ in PIPELINE))
    parser.add_argument("--config", default=None)
    parser.add_argument("--fast", action="store_true", help="reduced solver budgets and replications")
    parser.add_argument("--export-reports", action="store_true", help="build BI tables, dashboard payload and Excel workbook")
    parser.add_argument("--open-dashboard", action="store_true", help="launch the local Dash app when done")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, overrides=FAST if args.fast else None)
    wanted = [n for n, _ in PIPELINE] if "all" in args.modules else args.modules
    unknown = set(wanted) - {n for n, _ in PIPELINE}
    if unknown:
        parser.error(f"unknown modules: {sorted(unknown)}")
    for name, path in PIPELINE:
        if name not in wanted:
            continue
        t0 = time.time()
        log.info("== %s ==", name)
        importlib.import_module(path).run(cfg)
        log.info("== %s done in %.0f s ==", name, time.time() - t0)
    if args.export_reports:
        for name, path in REPORTS:
            log.info("== report: %s ==", name)
            importlib.import_module(path).run(cfg)
    if args.open_dashboard:
        import webbrowser

        from dashboard.app import create_app
        log.info("Dash app on http://127.0.0.1:8050 (Ctrl+C to stop)")
        webbrowser.open("http://127.0.0.1:8050")
        create_app().run(debug=False, host="127.0.0.1", port=8050)
    return 0


if __name__ == "__main__":
    sys.exit(main())
