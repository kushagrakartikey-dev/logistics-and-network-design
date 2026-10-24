"""Compact JSON payload for the dashboards (static GitHub Pages site and Dash app).

Everything the browser needs - routes as coordinate lists, fleet curves,
warehouse heat grids, simulation summaries, network configurations and the
cash-flow inputs for client-side NPV recalculation - in one file
(``dashboard/static/data/payload.json``).

Run: ``python -m src.logistics.reporting.payload``
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import load_json, save_json
from src.shared.logging_utils import get_logger

from ..common import load_bundle

log = get_logger(__name__)


def _r(x: Any, n: int = 2) -> Any:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), n)


def build(cfg: dict[str, Any]) -> dict[str, Any]:
    t = resolve_path(cfg, "tables_dir")
    b = load_bundle(cfg)
    rp = lambda n: pd.read_parquet(t / n)  # noqa: E731
    cities = list(cfg["cities"])
    routing = load_json(t / "routing_summary.json")
    cc = load_json(t / "command_center_view.json")
    whs = load_json(t / "warehouse_summary.json")
    dep = b.depots.set_index("depot_id")

    meta_cities = []
    for cid, c in cfg["cities"].items():
        a = b.addresses[b.addresses["city_id"] == cid]
        meta_cities.append({"id": cid, "name": c["name"],
                            "states": sorted(a["state"].unique().tolist()),
                            "center": [_r(a["lat"].mean(), 4), _r(a["lon"].mean(), 4)]})

    # ---------------- routing
    stops = rp("route_stops.parquet")
    rts = rp("routes_summary.parquet")
    routes: dict[str, dict[str, list]] = {c: {"baseline": [], "optimized": []} for c in cities}
    for (plan, vid), g in stops.sort_values("seq").groupby(["plan", "vehicle_id"]):
        d = dep.loc[g["depot_id"].iloc[0]]
        meta = rts[(rts["plan"] == plan) & (rts["vehicle_id"] == vid)].iloc[0]
        pts = [[_r(d["lat"], 4), _r(d["lon"], 4)]] + [[_r(a, 4), _r(o, 4)] for a, o in zip(g["lat"], g["lon"])]
        pts.append(pts[0])
        routes[g["city_id"].iloc[0]][plan].append({
            "v": vid, "type": meta["vehicle_type"], "depot": meta["depot_id"], "pts": pts, "stops": int(meta["stops"]),
            "late": int(meta["late_stops"]), "cost": _r(meta["cost_total"], 0), "km": _r(meta["road_km"], 1),
            "util": _r(meta["time_util_pct"], 1), "overrun": bool(meta["duration_violation"])})
    un = rp("unassigned_orders.parquet")
    unassigned = {c: {p: un[(un["city_id"] == c) & (un["plan"] == p)][["lat", "lon", "reason"]].round(4).values.tolist()
                      for p in ("baseline", "optimized")} for c in cities}
    rcs = rp("routing_city_summary.parquet")
    routing_kpis = {c: {p: {k: _r(v, 4) for k, v in rcs[(rcs["city_id"] == c) & (rcs["plan"] == p)].iloc[0].items()
                            if k in ("orders", "served", "unassigned", "tw_compliance", "vehicles_used", "road_km",
                                     "straight_km", "fleet_cost", "outsourcing_cost", "last_mile_cost", "cost_per_order",
                                     "avg_load_util_pct", "avg_time_util_pct", "duration_violations")}
                        for p in ("baseline", "optimized")} for c in cities}
    zone_on_time = (stops.groupby(["city_id", "plan", "zone"])["on_time"].mean().round(4).reset_index()
                    .groupby("city_id").apply(lambda g: g[["plan", "zone", "on_time"]].values.tolist(), include_groups=False).to_dict())
    depots = {c: b.depots[b.depots["city_id"] == c][["depot_id", "name", "role", "lat", "lon"]].values.tolist() for c in cities}

    # ---------------- fleet
    fs = rp("fleet_scenarios.parquet")
    fleet = {}
    for c in cities + ["ALL"]:
        g = fs[fs["city_id"] == c].sort_values("demand_multiplier")
        fleet[c] = [{"m": _r(r.demand_multiplier), "orders": int(r.orders), "cur_comp": _r(r.current_fleet_compliance, 4),
                     "cur_un": int(r.current_fleet_unassigned), "rec": int(r.recommended_fleet), "vans": int(r.rec_vans),
                     "tempos": int(r.rec_tempos), "tw": int(r.rec_two_wheelers), "rec_comp": _r(r.rec_compliance, 4),
                     "cpo": _r(r.rec_cost_per_order), "own": int(r.annual_tco_own), "lease": int(r.annual_tco_lease),
                     "feasible": bool(r.feasible), "days": int(r.history_days_at_level)} for r in g.itertuples()]

    # ---------------- warehouse
    hm = rp("warehouse_heatmap.parquet")
    wh = {}
    for c in cities:
        g = hm[hm["city_id"] == c]
        grids = {k: g.pivot_table(index="bay", columns="aisle", values=f"{k}_picks").fillna(0).round(1).values.tolist()
                 for k in ("baseline", "optimized")}
        bc = whs["by_city"][c]
        wh[c] = {"grid": grids, "comparison": bc["comparison"],
                 "perf": {k: {m: _r(v, 3) for m, v in bc[k].items() if not isinstance(v, str)}
                          for k in ("baseline", "optimized", "perfect_information")}}
    wh["national"] = whs["national"]

    # ---------------- simulation
    sm = rp("simulation_summary.parquet")
    hist = rp("simulation_cycle_hist.parquet")
    sim = {}
    for c in cities:
        rows = []
        for r in sm[sm["city_id"] == c].itertuples():
            h = hist[(hist["city_id"] == c) & (hist["scenario"] == r.scenario)].sort_values("bin_start_min")
            rows.append({"scenario": r.scenario, "staff": [int(r.staff_pickers), int(r.staff_packers), int(r.staff_qc),
                                                           int(r.staff_loaders)],
                         "cost": _r(r.daily_staff_cost, 0), "thr": [_r(r.throughput_p10, 0), _r(r.throughput_p50, 0),
                                                                    _r(r.throughput_p90, 0)],
                         "p95": [_r(r.cycle_p95_p10, 1), _r(r.cycle_p95_p50, 1), _r(r.cycle_p95_p90, 1)],
                         "pooled_p95": _r(r.pooled_cycle_p95, 1), "sla": _r(r.sla_compliance_p50, 4),
                         "backlog": _r(r.backlog_p50, 0), "util": [_r(r.util_pickers, 3), _r(r.util_packers, 3),
                                                                   _r(r.util_qc, 3), _r(r.util_loaders, 3)],
                         "bottleneck": r.bottleneck, "meets": bool(r.meets_p95_target),
                         "hist": h["orders"].astype(int).tolist()})
        sim[c] = rows

    # ---------------- network
    ns = rp("network_scenarios.parquet")
    sites = rp("network_sites.parquet")
    rank = rp("network_city_ranking.parquet")
    net = {}
    for c in cities:
        g = ns[ns["city_id"] == c].sort_values("p")
        net[c] = {"configs": [{"p": int(r.p), "sites": r.sites.split(",") if r.sites else [], "names": r.site_names,
                               "km": _r(r.avg_km_per_order), "sla": _r(r.sla_2h_coverage, 4), "capex": _r(r.capex, 0),
                               "opex": _r(r.annual_opex, 0), "linehaul": _r(r.annual_linehaul, 0),
                               "saving": _r(r.annual_saving_y1, 0), "npv": _r(r.npv, 0),
                               "be": _r(r.break_even_parcels_per_day, 0)} for r in g.itertuples()],
                  "sites": sites[sites["city_id"] == c][["site_id", "name", "lat", "lon", "rent_psf_month"]].values.tolist()}
    ranking = rank.drop(columns=["recommendation"]).to_dict(orient="records")
    for r, rec in zip(ranking, rank["recommendation"]):
        r["recommendation"] = rec

    # ---------------- demand
    daily = b.orders.groupby(["order_date", "city_id"]).size().unstack()[cities]
    demand = {"dates": [d.strftime("%Y-%m-%d") for d in daily.index], **{c: daily[c].astype(int).tolist() for c in cities}}

    return {
        "meta": {"generated": datetime.now(UTC).isoformat(timespec="seconds"), "day": routing["day"],
                 "cities": meta_cities, "wacc": cfg["network"]["wacc"], "growth": cfg["network"]["demand_growth"],
                 "horizon": cfg["network"]["horizon_years"], "target_cycle": cfg["simulation"]["cycle_time_target_min"],
                 "compliance_target": cfg["fleet_sizing"]["compliance_target"]},
        "kpis": {"national": {"baseline": routing["baseline"], "optimized": routing["optimized"],
                              "comparison": routing["comparison"]}, "health": cc["health"]},
        "routing": {"routes": routes, "unassigned": unassigned, "kpis": routing_kpis, "zones": zone_on_time, "depots": depots},
        "fleet": fleet, "warehouse": wh, "simulation": sim, "network": {"cities": net, "ranking": ranking},
        "alerts": cc["alerts"][:12], "demand": demand,
    }


def run(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = build(cfg)
    path = resolve_path(cfg, "static_payload")
    save_json(payload, path, compact=True)
    log.info("Dashboard payload written to %s (%.0f KB)", path, path.stat().st_size / 1024)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()
