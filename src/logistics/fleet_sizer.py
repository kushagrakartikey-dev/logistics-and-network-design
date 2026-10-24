"""Fleet sizing and vehicle-mix optimization across demand levels.

For each demand multiplier (0.5x ... 3.0x of the representative day):

1. **Current fleet** - the existing 25 vehicles are routed (CVRPTW) to measure
   time-window compliance, unassigned orders and cost.
2. **Optimized fleet** - a candidate pool per depot and vehicle type is routed
   with each vehicle carrying its daily fixed cost in the objective, so the
   solver selects the cost-minimising fleet size *and* mix (fleet-size-and-mix
   VRP). The vehicles actually used form the recommended fleet. A scenario is
   feasible when compliance exceeds the target (default 95%).
3. **TCO** - recommended fleets are annualised: ownership (equivalent annual
   cost of capital, insurance, maintenance) vs operating lease, plus fuel and
   driver labour from the routed kilometres and hours.

Scenarios that miss the target even with the full pool are reported as
infeasible rather than silently relaxed.

Run: ``python -m src.logistics.fleet_sizer``
"""
from __future__ import annotations

import argparse
import math
from typing import Any

import numpy as np
import pandas as pd

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import load_json, save_json, write_table
from src.shared.logging_utils import get_logger

from .common import VEHICLE_TYPES, DataBundle, load_bundle
from .data_generator import vehicle_row
from .vrp_optimizer import load_calibration, orders_for_day, plan_day, representative_day, summarize

log = get_logger(__name__)


def scale_orders(bundle: DataBundle, day: pd.Timestamp, multiplier: float, seed: int) -> pd.DataFrame:
    """Order set at ``multiplier`` x the representative day.

    Below 1.0 a random subset is kept; above 1.0 extra orders are drawn from
    all non-event weekday history (same segment/depot mix), with new IDs.
    """
    rng = np.random.default_rng(seed + int(round(multiplier * 1000)))
    base = orders_for_day(bundle, day)
    n = int(round(multiplier * len(base)))
    if n <= len(base):
        return base.sample(n=n, random_state=int(rng.integers(1 << 31))).reset_index(drop=True)
    flags = bundle.calendar.groupby("date").agg(any_event=("is_event", "any"), dow=("dow", "first"))
    normal_days = flags[(~flags["any_event"]) & (flags["dow"] < 5)].index
    pool = bundle.orders[bundle.orders["order_date"].isin(normal_days) & (bundle.orders["order_date"] != day)]
    extra = pool.sample(n=n - len(base), replace=len(pool) < n - len(base),
                        random_state=int(rng.integers(1 << 31))).copy()
    extra["order_id"] = [f"SIM-{c}-{multiplier:.2f}-{i:05d}" for i, c in enumerate(extra["city_id"])]
    extra["order_date"] = day
    return pd.concat([base, extra], ignore_index=True)


def candidate_pool(cfg: dict[str, Any], depots: pd.DataFrame, multiplier: float) -> pd.DataFrame:
    """Candidate vehicles per depot/type: ceil(current x max(1, m) x pool_factor), at least 2."""
    pf = float(cfg["fleet_sizing"]["pool_factor"])
    alloc = cfg["fleet"]["allocation_by_role"]
    rows, n = [], 0
    for d in depots.itertuples():
        for vtype in VEHICLE_TYPES:
            k = max(2, int(math.ceil(int(alloc[d.role].get(vtype, 0)) * max(1.0, multiplier) * pf)))
            for _ in range(k):
                n += 1
                rows.append(vehicle_row(cfg, f"POOL-{d.city_id}-{n:04d}", vtype, d.depot_id, d.city_id))
    return pd.DataFrame(rows)


def equivalent_annual_cost(capex: float, residual: float, rate: float, years: int) -> float:
    """Annualised capital cost: (capex - PV(residual)) x capital recovery factor."""
    crf = rate / (1.0 - (1.0 + rate) ** -years)
    return (capex - residual / (1.0 + rate) ** years) * crf


def fleet_tco(cfg: dict[str, Any], routes: pd.DataFrame, wage_index: float = 1.0) -> pd.DataFrame:
    """Annual TCO by vehicle type for a routed fleet (own vs lease); labour at the city wage level."""
    vt = cfg["fleet"]["vehicle_types"]
    days = float(cfg["fleet"]["operating_days_per_year"])
    wacc = float(cfg["network"]["wacc"])
    admin = float(cfg["fleet_sizing"]["leasing_admin_pct"])
    fuel_share = float(cfg["fleet"]["fuel_share_of_cost_per_km"])
    rows = []
    for vtype, grp in routes.groupby("vehicle_type"):
        p = vt[vtype]
        count = len(grp)
        km = float(grp["road_km"].sum()) * days
        hours = float(grp["duration_min"].sum()) / 60.0 * days
        fuel = km * p["cost_per_km"] * fuel_share
        maint = km * p["maintenance_per_km"]
        labour = hours * p["driver_wage_per_hour"] * wage_index
        eac = equivalent_annual_cost(p["acquisition_cost"], p["acquisition_cost"] * p["residual_value_pct"], wacc,
                                     int(p["useful_life_years"])) * count
        insurance = p["insurance_per_year"] * count
        own = eac + insurance + maint + fuel + labour
        lease = p["lease_per_month"] * 12 * count * (1 + admin) + fuel + labour
        rows.append({"vehicle_type": vtype, "vehicles": count, "annual_km": round(km), "annual_driver_hours": round(hours),
                     "capital_eac": round(eac), "insurance": round(insurance), "maintenance": round(maint),
                     "fuel": round(fuel), "labour": round(labour), "tco_own": round(own), "tco_lease": round(lease),
                     "preferred": "own" if own <= lease else "lease",
                     "km_per_vehicle_day": round(float(grp["road_km"].mean()), 1),
                     "time_util_pct": round(float(grp["time_util_pct"].mean()), 1),
                     "load_util_pct": round(float(grp["load_util_pct"].mean()), 1)})
    return pd.DataFrame(rows)


def demand_level_frequency(bundle: DataBundle, day: pd.Timestamp, levels: list[float]) -> dict[float, int]:
    """Number of history days whose volume is nearest each demand level."""
    daily = bundle.orders.groupby("order_date").size()
    ratio = daily / float(daily.loc[day])
    nearest = ratio.apply(lambda r: min(levels, key=lambda m: abs(m - r)))
    return {m: int((nearest == m).sum()) for m in levels}


def forecast_recommendation(cfg: dict[str, Any], levels: list[float]) -> dict[str, Any] | None:
    """Map the ML 14-day volume forecast (if validated) onto a fleet scenario."""
    path = resolve_path(cfg, "tables_dir") / "ml_demand_forecast.json"
    if not path.exists():
        return None
    fc = load_json(path)
    if not fc.get("model_beats_baseline"):
        return {"used": False, "reason": "forecast model did not beat seasonal-naive baseline"}
    peak = float(fc["next_14d_peak_multiplier"])
    level = min((m for m in levels if m >= peak), default=max(levels))
    return {"used": True, "forecast_peak_multiplier": round(peak, 3), "scenario_to_plan": level,
            "forecast_horizon": fc.get("horizon")}


def run(cfg: dict[str, Any], bundle: DataBundle | None = None, multipliers: list[float] | None = None) -> dict[str, Any]:
    """Evaluate every demand level with the current and the optimized fleet, per city."""
    bundle = bundle or load_bundle(cfg)
    fs = cfg["fleet_sizing"]
    target = float(fs["compliance_target"])
    levels = [float(m) for m in (multipliers or fs["demand_multipliers"])]
    calib = load_calibration(cfg)
    day = representative_day(bundle)
    seed = int(cfg["project"]["seed"])
    freq = demand_level_frequency(bundle, day, levels)
    spo = float(cfg["routing"].get("seconds_per_order", 0.2))
    scen_rows, mix_rows, tco_rows, route_rows = [], [], [], []
    for m in levels:
        orders = scale_orders(bundle, day, m, seed)
        tl = int(min(fs["time_limit_s"], max(3, math.ceil(len(orders) / len(bundle.depots) * spo))))
        tl_cur = int(min(fs.get("current_time_limit_s", tl), tl))
        cur = plan_day(orders, bundle.vehicles, bundle.depots, cfg, calib, time_limit_s=tl_cur)
        pool = candidate_pool(cfg, bundle.depots, m)
        opt = plan_day(orders, pool, bundle.depots, cfg, calib, time_limit_s=tl)
        for cid in ["ALL"] + list(cfg["cities"]):
            sel = (lambda df: df) if cid == "ALL" else (lambda df, c=cid: df[df["city_id"] == c])
            o_c = sel(orders)
            cm = summarize("current", sel(cur.routes), sel(cur.stops), sel(cur.unassigned), len(o_c))
            om = summarize("optimized", sel(opt.routes), sel(opt.stops), sel(opt.unassigned), len(o_c))
            used = sel(opt.routes)
            wage = 1.0 if cid == "ALL" else float(cfg["cities"][cid]["wage_index"])
            tco = fleet_tco(cfg, used, wage) if not used.empty else pd.DataFrame()
            mix = used.groupby("vehicle_type").size().reindex(list(VEHICLE_TYPES), fill_value=0)
            feasible = om["tw_compliance"] > target
            scen_rows.append({
                "city_id": cid, "demand_multiplier": m, "orders": len(o_c), "total_kg": round(float(o_c["weight_kg"].sum()), 0),
                "history_days_at_level": freq.get(m, 0),
                "current_fleet_size": int(len(bundle.vehicles) if cid == "ALL" else (bundle.vehicles["city_id"] == cid).sum()),
                "current_fleet_vehicles_used": cm["vehicles_used"], "current_fleet_compliance": cm["tw_compliance"],
                "current_fleet_unassigned": cm["unassigned"], "current_fleet_cost": cm["last_mile_cost"],
                "current_fleet_meets_target": cm["tw_compliance"] > target,
                "recommended_fleet": int(len(used)), "rec_vans": int(mix["VAN_2T"]), "rec_tempos": int(mix["TEMPO_1T"]),
                "rec_two_wheelers": int(mix["TWO_WHEELER"]), "rec_compliance": om["tw_compliance"],
                "rec_unassigned": om["unassigned"], "rec_daily_cost": om["last_mile_cost"],
                "rec_cost_per_order": om["cost_per_order"], "rec_road_km": om["road_km"],
                "rec_time_util_pct": om["avg_time_util_pct"], "rec_load_util_pct": om["avg_load_util_pct"],
                "feasible": feasible,
                "annual_tco_own": int(tco["tco_own"].sum()) if not tco.empty else 0,
                "annual_tco_lease": int(tco["tco_lease"].sum()) if not tco.empty else 0,
                "status": "feasible" if feasible else f"infeasible: compliance {om['tw_compliance']:.1%} <= {target:.0%} with candidate pool",
            })
            if cid != "ALL" and not tco.empty:
                tco.insert(0, "demand_multiplier", m)
                tco.insert(0, "city_id", cid)
                tco_rows.append(tco)
        for (cid, depot_id, vtype), g2 in opt.routes.groupby(["city_id", "depot_id", "vehicle_type"]):
            mix_rows.append({"demand_multiplier": m, "city_id": cid, "depot_id": depot_id, "vehicle_type": vtype, "vehicles": len(g2)})
        r = opt.routes.copy()
        r.insert(0, "demand_multiplier", m)
        route_rows.append(r)
        log.info("x%.2f | %d orders | current fleet %.1f%% (%d unassigned) | recommended %d veh %.1f%% %s",
                 m, len(orders), cur.metrics["tw_compliance"] * 100, cur.metrics["unassigned"], len(opt.routes),
                 opt.metrics["tw_compliance"] * 100, "OK" if opt.metrics["tw_compliance"] > target else "INFEASIBLE")
    scen = pd.DataFrame(scen_rows)
    names = {k: v["name"] for k, v in cfg["cities"].items()} | {"ALL": "All cities"}
    scen.insert(1, "city", scen["city_id"].map(names))
    tdir = resolve_path(cfg, "tables_dir")
    write_table(scen, tdir / "fleet_scenarios.parquet", csv_copy=True)
    write_table(pd.DataFrame(mix_rows), tdir / "fleet_mix.parquet", csv_copy=True)
    write_table(pd.concat(tco_rows, ignore_index=True), tdir / "fleet_tco.parquet", csv_copy=True)
    write_table(pd.concat(route_rows, ignore_index=True), tdir / "fleet_routes.parquet")
    nat = scen[scen["city_id"] == "ALL"]
    rec = forecast_recommendation(cfg, levels)
    summary = {"representative_day": str(day.date()), "target": target, "levels": levels,
               "feasible_levels": nat.loc[nat["feasible"], "demand_multiplier"].tolist(),
               "infeasible": scen.loc[~scen["feasible"], ["city_id", "demand_multiplier", "status"]].to_dict(orient="records"),
               "current_fleet_max_level_meeting_target": {
                   cid: (float(g.loc[g["current_fleet_meets_target"], "demand_multiplier"].max())
                         if g["current_fleet_meets_target"].any() else None)
                   for cid, g in scen.groupby("city_id")},
               "forecast_link": rec}
    save_json(summary, tdir / "fleet_summary.json")
    return {"scenarios": scen, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    parser.add_argument("--multipliers", type=float, nargs="*", default=None)
    args = parser.parse_args()
    run(load_config(args.config), multipliers=args.multipliers)


if __name__ == "__main__":
    main()
