"""Command Center integration for Output 6.

Turns logistics results into the interfaces other portfolio outputs consume,
and consumes theirs when they exist:

* Routing -> inbound delay: lane-level transit time and lateness statistics
  (mean, standard deviation in days) for Output 5's safety-stock formula
  SS = Z x sqrt(LT x sigma_d^2 + mu_d^2 x sigma_LT^2). The logistics side alone
  quantifies the change in the lead-time variance term (sigma_LT^2).
* Fleet sizing -> TCO: delivered cost per order and per kg by city for
  Output 4's TCO freight component.
* Warehouse slotting -> throughput: picks per hour per city DC.
* Demand forecast -> workload: next-14-day orders, pick lines, picker-hours.
* Network design -> economics: per-city MFC NPV, break-even and SLA coverage.

Upstream contracts are read with ``read_contract``; when Output 5 has not been
run, the safety-stock impact is reported as "awaiting upstream" - never
estimated from invented demand data.

Run: ``python -m src.logistics.integration``
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from src.shared.command_center import publish_contract, read_contract, upstream_status
from src.shared.config import load_config, resolve_path
from src.shared.io_utils import load_json, save_json, write_table
from src.shared.logging_utils import get_logger

log = get_logger(__name__)
MIN_PER_DAY = 1440.0


def lane_lead_times(stops: pd.DataFrame, routes: pd.DataFrame) -> pd.DataFrame:
    """Depot -> zone transit and lateness statistics per plan."""
    st = stops.merge(routes[["plan", "vehicle_id", "start_min"]], on=["plan", "vehicle_id"])
    st["transit_min"] = st["arrival_min"] - st["start_min"]
    st["deviation_min"] = st["arrival_min"] - st["tw_end_min"]          # >0 = late vs promise
    g = st.groupby(["plan", "city_id", "depot_id", "zone"])
    out = g.agg(stops=("order_id", "size"), mean_transit_min=("transit_min", "mean"),
                sd_transit_min=("transit_min", "std"), on_time_rate=("on_time", "mean"),
                mean_late_min=("late_min", "mean"), sd_deviation_min=("deviation_min", "std")).reset_index()
    out["sd_lead_time_days"] = (out["sd_deviation_min"].fillna(0.0) / MIN_PER_DAY).round(6)
    return out.round(4)


def lead_time_variance_change(lanes: pd.DataFrame) -> pd.DataFrame:
    """Stop-weighted sigma_LT^2 by city: baseline vs optimized (logistics-only effect)."""
    rows = []
    for cid, g in lanes.groupby("city_id"):
        var = {p: float(np.average(gp["sd_lead_time_days"] ** 2, weights=gp["stops"]))
               for p, gp in g.groupby("plan")}
        b, o = var.get("baseline", np.nan), var.get("optimized", np.nan)
        rows.append({"city_id": cid, "sigma_lt2_baseline_days2": b, "sigma_lt2_optimized_days2": o,
                     "sigma_lt2_change_pct": round((o - b) / b * 100, 2) if b else np.nan})
    return pd.DataFrame(rows)


def delivered_cost(routes: pd.DataFrame, stops: pd.DataFrame) -> pd.DataFrame:
    """Optimized delivered cost per order and per kg by city (TCO freight component)."""
    r = routes[routes["plan"] == "optimized"].groupby("city_id")["cost_total"].sum()
    s = stops[stops["plan"] == "optimized"].groupby("city_id").agg(orders=("order_id", "size"), kg=("weight_kg", "sum"))
    df = s.join(r).reset_index()
    df["cost_per_order"] = (df["cost_total"] / df["orders"]).round(2)
    df["cost_per_kg"] = (df["cost_total"] / df["kg"]).round(3)
    return df


def workload_forecast(cfg: dict[str, Any], tdir: Any, wh: dict[str, Any]) -> pd.DataFrame:
    """Next-14-day orders, pick lines and picker-hours per city from the validated forecast."""
    fpath = tdir / "ml_demand_forecast.parquet"
    if not fpath.exists():
        return pd.DataFrame()
    fc = pd.read_parquet(fpath).groupby(["city_id", "date"])["forecast"].sum().reset_index()
    data_dir = resolve_path(cfg, "data_dir")
    orders = pd.read_parquet(data_dir / "orders.parquet", columns=["city_id", "lines"])
    lines_per_order = orders.groupby("city_id")["lines"].mean()
    fc["pick_lines"] = fc["forecast"] * fc["city_id"].map(lines_per_order)
    pph = {cid: v["optimized"]["picks_per_hour"] for cid, v in wh["by_city"].items()}
    fc["picker_hours"] = fc["pick_lines"] / fc["city_id"].map(pph)
    return fc.round(2)


def health_score(routing: dict[str, Any], wh: dict[str, Any], sim: pd.DataFrame, fleet: pd.DataFrame,
                 cfg: dict[str, Any]) -> dict[str, Any]:
    """0-100 logistics health index from measured outputs (weights documented in METHODOLOGY.md).

    Components are scaled so that shortfalls and operational risk show up:
    * delivery: 0 at 90% in-window, 100 at 99%, scaled down by the share of routes with duty overruns;
    * last-mile cost: fleet-cost reduction / 22% (top of the target range);
    * warehouse: throughput gain / 30% (top of the target range);
    * DC cycle: mean of (cities meeting P95 with the optimized roster) and (cities meeting it at Diwali
      with today's roster) - readiness, not just the best case;
    * fleet scalability: share of demand levels the *current* fleet serves at the compliance target.
    """
    clip = lambda x: float(min(max(x, 0.0), 1.0))  # noqa: E731
    opt = routing["optimized"]
    overrun = opt["duration_violations"] / max(1, opt["vehicles_used"])
    delivery = clip((opt["tw_compliance"] - 0.90) / 0.09) * (1 - overrun)
    cost = clip(routing["comparison"]["fleet_cost_reduction_pct"] / 22.0)
    thr = clip(wh["national"]["throughput_gain_pct"] / 30.0)
    o = sim[sim["scenario"] == "Optimized staffing"]["meets_p95_target"].mean() if not sim.empty else 0.0
    d = sim[sim["scenario"] == "Diwali peak - baseline staffing"]["meets_p95_target"].mean() if not sim.empty else 0.0
    dc = clip((float(o) + float(d)) / 2)
    cities = fleet[fleet["city_id"] != "ALL"] if not fleet.empty else fleet
    scal = clip(float(cities["current_fleet_meets_target"].mean())) if not cities.empty else 0.0
    parts = {"delivery_compliance": (delivery, 0.30), "last_mile_cost": (cost, 0.20), "warehouse_throughput": (thr, 0.15),
             "dc_readiness": (dc, 0.20), "fleet_scalability": (scal, 0.15)}
    score = sum(v * w for v, w in parts.values()) * 100
    return {"score": round(score, 1), "components": {k: round(v * 100, 1) for k, (v, _) in parts.items()},
            "weights": {k: w for k, (_, w) in parts.items()}}


def alerts(routing: dict[str, Any], sim: pd.DataFrame, fleet: pd.DataFrame, net: dict[str, Any],
           routes: pd.DataFrame) -> list[dict[str, Any]]:
    """Business alerts ranked by estimated daily INR impact (where measurable)."""
    out = []
    for r in sim[(sim["scenario"] == "Diwali peak - baseline staffing") & (~sim["meets_p95_target"])].itertuples():
        out.append({"severity": "high", "area": "DC operations", "city_id": r.city_id,
                    "message": f"Diwali volume at current roster: P95 cycle {r.pooled_cycle_p95:.0f} min, "
                               f"backlog P50 {r.backlog_p50:.0f} orders", "impact_orders": float(r.backlog_p50)})
    if not fleet.empty:
        cur_fail = fleet[(fleet["city_id"] != "ALL") & (~fleet["current_fleet_meets_target"])]
        for cid, g in cur_fail.groupby("city_id"):
            m = float(g["demand_multiplier"].min())
            out.append({"severity": "medium", "area": "Fleet", "city_id": cid,
                        "message": f"Current 25-vehicle fleet falls below the compliance target from {m:g}x demand",
                        "impact_orders": float(g.loc[g["demand_multiplier"] == m, "current_fleet_unassigned"].iloc[0])})
    viol = routes[(routes["plan"] == "optimized") & routes["duration_violation"]]
    for cid, g in viol.groupby("city_id"):
        out.append({"severity": "low", "area": "Routing", "city_id": cid,
                    "message": f"{len(g)} optimized route(s) exceed driver duty under replayed traffic (overtime)",
                    "impact_orders": 0.0})
    for cid, s in net["by_city"].items():
        if s["best_npv"] <= 0:
            out.append({"severity": "info", "area": "Network", "city_id": cid,
                        "message": s["recommendation"], "impact_orders": 0.0})
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(out, key=lambda a: (order[a["severity"]], -a["impact_orders"]))


def safety_stock_impact(cc_dir: Any, var_change: pd.DataFrame) -> dict[str, Any]:
    """Apply sigma_LT^2 change to Output 5 SKU stats when its contract exists."""
    inv = read_contract(cc_dir, "inventory")
    if inv is None:
        return {"status": "awaiting upstream", "detail": "Output 5 (Inventory Optimizer) contract not found; "
                "lane sigma_LT published for it to consume"}
    skus = pd.DataFrame(inv.get("interfaces", {}).get("sku_policy", []))
    need = {"sku_id", "city_id", "z", "lead_time_days", "mu_daily_demand", "sd_daily_demand"}
    if skus.empty or not need <= set(skus.columns):
        return {"status": "upstream contract missing fields", "required": sorted(need)}
    v = var_change.set_index("city_id")
    skus = skus[skus["city_id"].isin(v.index)].copy()
    for plan, col in (("baseline", "sigma_lt2_baseline_days2"), ("optimized", "sigma_lt2_optimized_days2")):
        s2 = skus["city_id"].map(v[col])
        skus[f"ss_{plan}"] = skus["z"] * np.sqrt(skus["lead_time_days"] * skus["sd_daily_demand"] ** 2
                                                 + skus["mu_daily_demand"] ** 2 * s2)
    return {"status": "computed", "skus": len(skus),
            "safety_stock_units_baseline": float(skus["ss_baseline"].sum()),
            "safety_stock_units_optimized": float(skus["ss_optimized"].sum())}


def run(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build the Command Center view and publish the logistics contract."""
    tdir = resolve_path(cfg, "tables_dir")
    cc_dir = resolve_path(cfg, "command_center_dir")
    routing = load_json(tdir / "routing_summary.json")
    routes = pd.read_parquet(tdir / "routes_summary.parquet")
    stops = pd.read_parquet(tdir / "route_stops.parquet")
    wh = load_json(tdir / "warehouse_summary.json")
    net = load_json(tdir / "network_summary.json")
    sim = pd.read_parquet(tdir / "simulation_summary.parquet")
    fpath = tdir / "fleet_scenarios.parquet"
    fleet = pd.read_parquet(fpath) if fpath.exists() else pd.DataFrame()
    tco_path = tdir / "fleet_tco.parquet"
    tco = pd.read_parquet(tco_path) if tco_path.exists() else pd.DataFrame()
    lanes = lane_lead_times(stops, routes)
    var_change = lead_time_variance_change(lanes)
    cost = delivered_cost(routes, stops)
    work = workload_forecast(cfg, tdir, wh)
    health = health_score(routing, wh, sim, fleet, cfg)
    alert_list = alerts(routing, sim, fleet, net, routes)
    ss = safety_stock_impact(cc_dir, var_change)
    write_table(lanes, tdir / "cc_lane_lead_times.parquet", csv_copy=True)
    write_table(var_change, tdir / "cc_lead_time_variance.parquet", csv_copy=True)
    write_table(cost, tdir / "cc_delivered_cost.parquet", csv_copy=True)
    if not work.empty:
        write_table(work, tdir / "cc_workload_forecast.parquet", csv_copy=True)
    write_table(pd.DataFrame(alert_list), tdir / "cc_alerts.parquet", csv_copy=True)
    tco_1x = tco[tco["demand_multiplier"] == 1.0] if not tco.empty else tco
    kpis = {"health_score": health["score"], "tw_compliance": routing["optimized"]["tw_compliance"],
            "fleet_cost_reduction_pct": routing["comparison"]["fleet_cost_reduction_pct"],
            "last_mile_cost_reduction_pct": routing["comparison"]["last_mile_cost_reduction_pct"],
            "pick_travel_reduction_pct": wh["national"]["travel_reduction_pct"],
            "throughput_gain_pct": wh["national"]["throughput_gain_pct"],
            "cities": list(cfg["cities"]), "network_priority_city": net["ranking"][0]["city_id"]}
    interfaces = {"lane_lead_times": lanes[lanes["plan"] == "optimized"].to_dict(orient="records"),
                  "lead_time_variance_change": var_change.to_dict(orient="records"),
                  "delivered_cost_by_city": cost.to_dict(orient="records"),
                  "fleet_tco_1x": tco_1x.to_dict(orient="records"),
                  "dc_picks_per_hour": {c: v["optimized"]["picks_per_hour"] for c, v in wh["by_city"].items()},
                  "workload_forecast": work.assign(date=work["date"].astype(str)).to_dict(orient="records") if not work.empty else [],
                  "network_ranking": net["ranking"]}
    publish_contract(cc_dir, "logistics", kpis, interfaces,
                     notes=[f"Safety-stock linkage: {ss['status']}"])
    view = {"health": health, "alerts": alert_list, "upstream": upstream_status(cc_dir), "safety_stock": ss, "kpis": kpis}
    save_json(view, tdir / "command_center_view.json")
    log.info("Command Center | health %.1f | %d alerts | upstream %s | safety stock: %s",
             health["score"], len(alert_list), view["upstream"], ss["status"])
    return view


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()
