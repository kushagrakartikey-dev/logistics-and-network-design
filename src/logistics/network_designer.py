"""Micro-fulfilment network design per city: p-median over candidate MFC sites.

Each metro cluster (Mumbai: 15 candidates; Delhi NCR, Bengaluru, Chennai,
Kolkata: 10 each) is solved independently, then cities are ranked for MFC
investment by the parcel volume at which their best configuration breaks even.

Model
-----
The city's three existing facilities (DC, MFC, hub) stay open. For p = 1..5 the p-median MILP (PuLP, HiGHS/CBC) chooses p new MFC
sites minimising demand-weighted road distance for MFC-eligible demand
(e-commerce parcels), with MFC daily capacity enforced:

    min  sum_i sum_j w_i d_ij x_ij
    s.t. sum_j x_ij = 1                      every demand point i
         x_ij <= y_j                         candidate j
         sum_j y_j = p                       candidates only
         sum_i q_i x_ij <= Cap_j y_j         candidate j (orders/day)
         y binary, x binary (single sourcing: each address served by one facility)

Economics (all from model outputs)
* Last-mile variable cost per order-km is calibrated from the optimized
  routing plan (km + driver-time cost / sum of depot-to-stop road km).
* Annual savings = variable cost reduction from shorter last-mile distance.
* Costs = MFC capex (year 0), rent + staff + utilities, and line-haul
  replenishment from Bhiwandi DC.
* NPV over 5 years at 12% WACC with demand growth; no terminal value.
* Two-hour SLA coverage = share of eligible demand whose travel time from its
  assigned facility (at the SLA traffic factor) plus processing <= 120 min.

Run: ``python -m src.logistics.network_designer``
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd
import pulp

from src.shared.config import load_config, resolve_path
from src.shared.geo import haversine_matrix
from src.shared.io_utils import save_json, write_table
from src.shared.logging_utils import get_logger
from src.shared.solvers import get_mip_solver, new_var, solve

from .common import DataBundle, load_bundle

log = get_logger(__name__)


def demand_points(bundle: DataBundle, cfg: dict[str, Any], city_id: str) -> pd.DataFrame:
    """Average daily MFC-eligible orders per address in one city over the 90-day history."""
    seg = set(cfg["network"]["eligible_segments"])
    days = bundle.orders["order_date"].nunique()
    o = bundle.orders[bundle.orders["segment"].isin(seg) & (bundle.orders["city_id"] == city_id)]
    q = o.groupby("address_id").size() / days
    d = bundle.addresses.set_index("address_id").loc[q.index, ["zone", "lat", "lon"]].copy()
    d["orders_per_day"] = q
    return d.reset_index()


def calibrate_cost_per_order_km(cfg: dict[str, Any], bundle: DataBundle, city_id: str) -> dict[str, float]:
    """Variable last-mile cost per order-km from the city's optimized routing plan."""
    tdir = resolve_path(cfg, "tables_dir")
    rpath, spath = tdir / "routes_summary.parquet", tdir / "route_stops.parquet"
    if not (rpath.exists() and spath.exists()):
        raise FileNotFoundError("Routing outputs not found; run vrp_optimizer before network_designer")
    routes = pd.read_parquet(rpath)
    stops = pd.read_parquet(spath)
    routes = routes[(routes["plan"] == "optimized") & (routes["city_id"] == city_id)]
    stops = stops[(stops["plan"] == "optimized") & (stops["city_id"] == city_id)]
    dep = bundle.depots.set_index("depot_id")
    circ = float(cfg["geography"]["planning_circuity"])
    d = haversine_matrix(stops["lat"], stops["lon"], dep.loc[stops["depot_id"], "lat"], dep.loc[stops["depot_id"], "lon"])
    order_km = float(np.diag(d).sum() * circ)
    variable = float(routes["cost_km"].sum() + routes["cost_driver"].sum())
    return {"variable_cost_inr": variable, "order_km": order_km, "cost_per_order_km": variable / order_km,
            "fixed_cost_inr": float(routes["cost_fixed"].sum()), "orders": int(len(stops))}


def solve_p_median(dist: np.ndarray, weight: np.ndarray, qty: np.ndarray, n_existing: int, p: int,
                   capacity: np.ndarray | None, time_limit: int = 120) -> tuple[list[int], np.ndarray, str]:
    """Solve the p-median with fixed existing facilities. Returns chosen candidate idx, assignment, status.

    ``dist`` is demand x facilities with existing facilities in the first
    ``n_existing`` columns and candidates after.
    """
    n_i, n_j = dist.shape
    cand = range(n_existing, n_j)
    prob = pulp.LpProblem("p_median", pulp.LpMinimize)
    y = {j: new_var(prob, f"y_{j}", 0, 1, "Binary") for j in cand}
    x = {(i, j): new_var(prob, f"x_{i}_{j}", 0, 1, "Binary") for i in range(n_i) for j in range(n_j)}
    prob += pulp.lpSum(weight[i] * dist[i, j] * x[i, j] for i in range(n_i) for j in range(n_j))
    for i in range(n_i):
        prob += pulp.lpSum(x[i, j] for j in range(n_j)) == 1
        for j in cand:
            prob += x[i, j] <= y[j]
    prob += pulp.lpSum(y.values()) == p
    if capacity is not None:
        for j in cand:
            prob += pulp.lpSum(qty[i] * x[i, j] for i in range(n_i)) <= capacity[j - n_existing] * y[j]
    status = solve(prob, get_mip_solver(time_limit, pure_integer=True))
    if status != "Optimal":
        raise RuntimeError(f"p-median p={p} not solved to optimality: {status}")
    chosen = [j - n_existing for j in cand if (y[j].value() or 0) > 0.5]
    assign = np.array([[x[i, j].value() or 0.0 for j in range(n_j)] for i in range(n_i)])
    return chosen, assign, status


def npv(cashflows: list[float], rate: float) -> float:
    """NPV with the first cash flow at t=0."""
    return float(sum(cf / (1.0 + rate) ** t for t, cf in enumerate(cashflows)))


def scenario_economics(base_km_day: float, new_km_day: float, n_sites: int, sites: pd.DataFrame, linehaul_km: float,
                       cpk: float, cfg: dict[str, Any], wacc: float | None = None, capex_pct: float = 0.0,
                       growth: float | None = None, var_pct: float = 0.0) -> dict[str, Any]:
    """Five-year cash flows for opening ``sites`` (savings grow with demand)."""
    net = cfg["network"]
    days = float(cfg["fleet"]["operating_days_per_year"])
    wacc = float(net["wacc"] if wacc is None else wacc)
    growth = float(net["demand_growth"] if growth is None else growth)
    capex = float(sites["capex_inr"].sum()) * (1 + capex_pct)
    opex = float(sites["annual_fixed_opex_inr"].sum())
    linehaul = linehaul_km * 2 * float(net["linehaul_trips_per_day"]) * float(net["linehaul_cost_per_km"]) * days
    saving_y1 = (base_km_day - new_km_day) * cpk * (1 + var_pct) * days
    flows = [-capex]
    for t in range(1, int(net["horizon_years"]) + 1):
        flows.append(saving_y1 * (1 + growth) ** (t - 1) - opex - linehaul)
    value = npv(flows, wacc)
    cum = np.cumsum(flows)
    payback = next((t for t, c in enumerate(cum) if c >= 0 and t > 0), None)
    return {"capex": capex, "annual_opex": opex, "annual_linehaul": linehaul, "annual_saving_y1": saving_y1,
            "cashflows": flows, "npv": value, "payback_year": payback}


def break_even_multiplier(eco: dict[str, Any], cfg: dict[str, Any], wacc: float | None = None) -> float | None:
    """Demand multiplier on today's parcel volume at which the configuration's NPV is zero.

    Last-mile savings scale linearly with eligible volume (same sites and
    assignment); capex, fixed opex and line-haul do not.
    """
    net = cfg["network"]
    r = float(net["wacc"] if wacc is None else wacc)
    g = float(net["demand_growth"])
    years = range(1, int(net["horizon_years"]) + 1)
    pv_cost = eco["capex"] + sum((eco["annual_opex"] + eco["annual_linehaul"]) / (1 + r) ** t for t in years)
    pv_saving = sum(eco["annual_saving_y1"] * (1 + g) ** (t - 1) / (1 + r) ** t for t in years)
    return float(pv_cost / pv_saving) if pv_saving > 0 else None


def run_city(cfg: dict[str, Any], bundle: DataBundle, city_id: str) -> dict[str, Any]:
    """Solve p = 1..5 for one city, evaluate SLA and NPV, run sensitivities."""
    net = cfg["network"]
    circ = float(cfg["geography"]["planning_circuity"])
    dem = demand_points(bundle, cfg, city_id)
    sites = bundle.candidate_sites[bundle.candidate_sites["city_id"] == city_id].reset_index(drop=True)
    depots = bundle.depots[bundle.depots["city_id"] == city_id].reset_index(drop=True)
    fac = pd.concat([depots[["depot_id", "name", "lat", "lon"]].rename(columns={"depot_id": "facility_id"}).assign(kind="existing"),
                     sites[["site_id", "name", "lat", "lon"]].rename(columns={"site_id": "facility_id"}).assign(kind="candidate")],
                    ignore_index=True)
    road = haversine_matrix(dem["lat"], dem["lon"], fac["lat"], fac["lon"]) * circ
    w = dem["orders_per_day"].to_numpy()
    cal = calibrate_cost_per_order_km(cfg, bundle, city_id)
    cpk = cal["cost_per_order_km"]
    # Baseline: each eligible order served from its current fulfilment depot (orders data)
    o = bundle.orders[bundle.orders["segment"].isin(net["eligible_segments"]) & (bundle.orders["city_id"] == city_id)]
    cur_dep = o.groupby("address_id")["depot_id"].agg(lambda s: s.mode().iat[0])
    dep_col = {d: k for k, d in enumerate(depots["depot_id"])}
    base_d = np.array([road[i, dep_col[cur_dep[a]]] for i, a in enumerate(dem["address_id"])])
    base_km_day = float((w * base_d).sum())
    speed, tf = float(net["sla_speed_kmph"]), float(net["sla_traffic_factor"])
    sla_limit = float(net["sla_minutes"]) - float(net["sla_processing_minutes"])

    def sla(dist_assigned: np.ndarray) -> float:
        return float((w * (dist_assigned / speed * 60.0 * tf <= sla_limit)).sum() / w.sum())

    bhw = depots.set_index("role").loc["DC"]
    lh = haversine_matrix([bhw["lat"]], [bhw["lon"]], sites["lat"], sites["lon"])[0] * circ
    rows, assign_rows, flows_rows = [], [], []
    rows.append({"p": 0, "sites": "", "site_names": "Current network", "weighted_km_per_day": round(base_km_day, 1),
                 "avg_km_per_order": round(base_km_day / w.sum(), 2), "sla_2h_coverage": round(sla(base_d), 4),
                 "capex": 0.0, "annual_opex": 0.0, "annual_linehaul": 0.0, "annual_saving_y1": 0.0,
                 "npv": 0.0, "payback_year": None, "solver_status": "n/a", "mfc_orders_per_day": 0.0,
                 "break_even_volume_multiplier": None, "break_even_parcels_per_day": None,
                 "break_even_within_capacity": None})
    results: dict[int, dict[str, Any]] = {}
    for p in net["p_values"]:
        chosen, assign, status = solve_p_median(road, w, w, len(depots), int(p),
                                                sites["capacity_orders_day"].to_numpy(dtype=float))
        dist_assigned = (assign * road).sum(axis=1)
        km_day = float((w * dist_assigned).sum())
        chosen_sites = sites.iloc[chosen]
        eco = scenario_economics(base_km_day, km_day, len(chosen), chosen_sites, float(lh[chosen].sum()), cpk, cfg)
        mfc_load = assign[:, len(depots):] * w[:, None]
        be = break_even_multiplier(eco, cfg)
        cap_total = float(chosen_sites["capacity_orders_day"].sum())
        rows.append({"p": int(p), "sites": ",".join(chosen_sites["site_id"]), "site_names": ", ".join(chosen_sites["name"]),
                     "weighted_km_per_day": round(km_day, 1), "avg_km_per_order": round(km_day / w.sum(), 2),
                     "sla_2h_coverage": round(sla(dist_assigned), 4), "capex": eco["capex"], "annual_opex": eco["annual_opex"],
                     "annual_linehaul": round(eco["annual_linehaul"]), "annual_saving_y1": round(eco["annual_saving_y1"]),
                     "npv": round(eco["npv"]), "payback_year": eco["payback_year"], "solver_status": status,
                     "mfc_orders_per_day": round(float(mfc_load.sum()), 1),
                     "break_even_volume_multiplier": round(be, 2) if be else None,
                     "break_even_parcels_per_day": round(be * float(w.sum())) if be else None,
                     "break_even_within_capacity": bool(be and float(mfc_load.sum()) * be <= cap_total)})
        for t, cf in enumerate(eco["cashflows"]):
            flows_rows.append({"p": int(p), "year": t, "cash_flow": round(cf), "discount_factor": round(1 / (1 + net["wacc"]) ** t, 6)})
        top = assign.argmax(axis=1)
        for i in range(len(dem)):
            assign_rows.append({"p": int(p), "address_id": dem.at[i, "address_id"], "zone": dem.at[i, "zone"],
                                "facility_id": fac.at[top[i], "facility_id"], "road_km": round(road[i, top[i]], 3),
                                "orders_per_day": round(float(w[i]), 4)})
        results[int(p)] = {"chosen": chosen, "eco": eco, "km_day": km_day, "lh": float(lh[chosen].sum())}
        log.info("%s p=%d sites=%s avg km/order %.2f SLA %.1f%% NPV INR %.2f cr", city_id, p, list(chosen_sites["name"]),
                 km_day / w.sum(), sla(dist_assigned) * 100, eco["npv"] / 1e7)
    scen = pd.DataFrame(rows)
    best_p = int(scen.loc[scen["p"] > 0].sort_values("npv", ascending=False).iloc[0]["p"])
    best_npv = float(scen.loc[scen["p"] == best_p, "npv"].iloc[0])
    best_row = scen.loc[scen["p"] == best_p].iloc[0]
    if best_npv > 0:
        recommendation = f"Open p={best_p} MFC(s): {best_row['site_names']}"
    else:
        recommendation = (f"Defer new MFCs: no configuration has positive NPV at today's {w.sum():.0f} parcels/day. "
                          f"Least-negative option p={best_p} ({best_row['site_names']}) breaks even at "
                          f"{best_row['break_even_volume_multiplier']:.1f}x volume "
                          f"(~{best_row['break_even_parcels_per_day']:.0f} parcels/day).")
    # Sensitivity on the recommended p (one-way) and site-level economics
    sens_rows = []
    r = results[best_p]
    chosen_sites = sites.iloc[r["chosen"]]
    for lever, values in net["sensitivity"].items():
        for v in values:
            kw = {"wacc": None, "capex_pct": 0.0, "growth": None, "var_pct": 0.0}
            key = {"wacc": "wacc", "capex_pct": "capex_pct", "growth": "growth", "variable_cost_pct": "var_pct"}[lever]
            kw[key] = v
            e = scenario_economics(base_km_day, r["km_day"], len(r["chosen"]), chosen_sites, r["lh"], cpk, cfg, **kw)
            sens_rows.append({"p": best_p, "lever": lever, "value": v, "npv": round(e["npv"])})
    site_tbl = sites.copy()
    site_tbl["linehaul_road_km"] = np.round(lh, 2)
    site_tbl["selected_in_p"] = [",".join(str(p) for p in results if k in results[p]["chosen"]) for k in range(len(sites))]
    tag = lambda df: df.assign(city_id=city_id)  # noqa: E731
    log.info("%s recommendation: %s (NPV INR %.2f cr)", city_id, recommendation, best_npv / 1e7)
    return {"scenarios": tag(scen), "assign": tag(pd.DataFrame(assign_rows)), "flows": tag(pd.DataFrame(flows_rows)),
            "sens": tag(pd.DataFrame(sens_rows)), "sites": site_tbl, "dem": dem, "fac": fac, "results": results,
            "best_p": best_p, "summary": {"calibration": cal, "baseline_km_per_day": base_km_day,
                                          "eligible_orders_per_day": float(w.sum()), "best_p": best_p,
                                          "best_npv": best_npv, "recommendation": recommendation,
                                          "baseline_sla_2h": float(scen.loc[scen["p"] == 0, "sla_2h_coverage"].iloc[0])}}


def run(cfg: dict[str, Any], bundle: DataBundle | None = None, cities: list[str] | None = None) -> dict[str, Any]:
    """Network design for every city plus a cross-state investment ranking."""
    bundle = bundle or load_bundle(cfg)
    out = {cid: run_city(cfg, bundle, cid) for cid in (cities or list(cfg["cities"]))}
    tdir = resolve_path(cfg, "tables_dir")
    cat = lambda key: pd.concat([r[key] for r in out.values()], ignore_index=True)  # noqa: E731
    scen = cat("scenarios")
    write_table(scen, tdir / "network_scenarios.parquet", csv_copy=True)
    write_table(cat("assign"), tdir / "network_assignments.parquet", csv_copy=True)
    write_table(cat("flows"), tdir / "network_cashflows.parquet", csv_copy=True)
    write_table(cat("sens"), tdir / "network_sensitivity.parquet", csv_copy=True)
    write_table(cat("sites"), tdir / "network_sites.parquet", csv_copy=True)
    rank_rows = []
    for cid, r in out.items():
        c = cfg["cities"][cid]
        best = r["scenarios"][r["scenarios"]["p"] == r["best_p"]].iloc[0]
        full_sla = r["scenarios"][(r["scenarios"]["p"] > 0) & (r["scenarios"]["sla_2h_coverage"] >= 0.999)]
        rank_rows.append({"city_id": cid, "city": c["name"], "state": c["state"],
                          "parcels_per_day": round(r["summary"]["eligible_orders_per_day"], 1),
                          "baseline_sla_2h": r["summary"]["baseline_sla_2h"], "best_p": r["best_p"],
                          "best_sites": best["site_names"], "best_npv": best["npv"],
                          "break_even_parcels_per_day": best["break_even_parcels_per_day"],
                          "break_even_multiplier": best["break_even_volume_multiplier"],
                          "min_p_full_sla": int(full_sla["p"].min()) if not full_sla.empty else None,
                          "cost_per_order_km": round(r["summary"]["calibration"]["cost_per_order_km"], 2),
                          "recommendation": r["summary"]["recommendation"]})
    ranking = pd.DataFrame(rank_rows).sort_values("break_even_multiplier", na_position="last").reset_index(drop=True)
    ranking.insert(0, "investment_priority", range(1, len(ranking) + 1))
    write_table(ranking, tdir / "network_city_ranking.parquet", csv_copy=True)
    summary = {"by_city": {cid: r["summary"] for cid, r in out.items()},
               "ranking": ranking.to_dict(orient="records")}
    save_json(summary, tdir / "network_summary.json")
    mdir = resolve_path(cfg, "maps_dir")
    for cid, r in out.items():
        network_map(r["dem"], r["fac"], r["results"], r["best_p"], r["sites"], mdir / f"network_design_map_{cid}.html")
    return {"scenarios": scen, "ranking": ranking, "summary": summary}


def network_map(dem: pd.DataFrame, fac: pd.DataFrame, results: dict[int, dict[str, Any]], best_p: int,
                sites: pd.DataFrame, path: Any) -> None:
    """Folium map: demand points, candidate sites, existing depots and the selected network per p."""
    import folium

    m = folium.Map(location=[float(dem["lat"].mean()), float(dem["lon"].mean())], zoom_start=11,
                   tiles="OpenStreetMap", control_scale=True)
    dl = folium.FeatureGroup(name="Parcel demand (orders/day)")
    for r in dem.itertuples():
        folium.CircleMarker((r.lat, r.lon), radius=2 + 6 * r.orders_per_day ** 0.5, color="#6B7A90", weight=0,
                            fill=True, fill_opacity=0.45, tooltip=f"{r.address_id}: {r.orders_per_day:.2f}/day").add_to(dl)
    dl.add_to(m)
    for r in fac[fac["kind"] == "existing"].itertuples():
        folium.Marker((r.lat, r.lon), tooltip=f"Existing: {r.name}", icon=folium.Icon(color="black", icon="home")).add_to(m)
    for p, res in sorted(results.items()):
        layer = folium.FeatureGroup(name=f"p = {p} selected MFCs", show=p == best_p)
        for k in res["chosen"]:
            s = sites.iloc[k]
            folium.Marker((s["lat"], s["lon"]), tooltip=f"p={p}: {s['name']} ({s['site_id']})",
                          icon=folium.Icon(color="green", icon="star")).add_to(layer)
        layer.add_to(m)
    cl = folium.FeatureGroup(name="All candidate sites", show=True)
    for s in sites.itertuples():
        folium.CircleMarker((s.lat, s.lon), radius=6, color="#E3A21A", fill=False, weight=2,
                            tooltip=f"Candidate {s.name}: rent INR {s.rent_psf_month}/sqft/month").add_to(cl)
    cl.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()

