"""Capacitated Vehicle Routing with Time Windows (CVRPTW) for the multi-state network.

Design
------
* Each order ships from its nearest stocking depot and vehicles are depot-based,
  so the multi-depot problem decomposes exactly into one CVRPTW per depot.
* Distances: Haversine straight-line km (geo) -> estimated road km via the
  planning circuity factor. Straight-line and road km are reported separately.
* Travel time = road km / vehicle speed x traffic factor x zone calibration.
  The solver plans each arc with the slower traffic band of its origin and
  destination window midpoints and a duty-time buffer; the evaluator then
  replays every route with the factor at the *actual* departure minute. Both
  the OR-Tools plan and the nearest-neighbour baseline are scored by the same
  evaluator.
* Constraints: capacity, hard time windows, vehicle eligibility (capacity,
  2-wheeler parcel limit, restricted zones), service times, route duration.
  Orders that cannot be served are dropped through penalised disjunctions and
  reported explicitly; they are costed at the spot-outsourcing rate.

Run: ``python -m src.logistics.vrp_optimizer``
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from src.shared.config import load_config, resolve_path
from src.shared.geo import haversine_matrix
from src.shared.io_utils import load_json, save_json, write_table
from src.shared.logging_utils import get_logger
from src.shared.style import SERIES

from .common import DataBundle, load_bundle, traffic_factor

log = get_logger(__name__)
TIME_SCALE = 10          # solver time unit = 0.1 minute
COST_SCALE = 100         # solver cost unit = 0.01 INR
DIST_SCALE = 1000        # solver distance unit = metre


# =========================================================================== data structures
@dataclass
class PlanResult:
    """Evaluated routing plan (optimized or baseline)."""

    plan: str
    routes: pd.DataFrame
    stops: pd.DataFrame
    unassigned: pd.DataFrame
    metrics: dict[str, Any]
    solver_status: dict[str, str] = field(default_factory=dict)


@dataclass
class DepotProblem:
    """Node-level arrays for one depot's CVRPTW."""

    depot: pd.Series
    orders: pd.DataFrame          # one row per order node (node index = position + 1)
    vehicles: pd.DataFrame
    straight_km: np.ndarray       # (N+1)x(N+1), node 0 = depot
    road_km: np.ndarray
    zone_factor: np.ndarray       # per node (destination) travel-time calibration


# =========================================================================== helpers
def load_calibration(cfg: dict[str, Any]) -> dict[str, float]:
    """Zone travel-time calibration from the ML module (1.0 when unavailable)."""
    if not cfg["routing"].get("use_ml_travel_calibration", False):
        return {}
    path = resolve_path(cfg, "tables_dir") / "ml_travel_calibration.json"
    if not path.exists():
        log.warning("ML travel calibration not found (%s); planning with uncalibrated travel times", path.name)
        return {}
    return {k: float(v) for k, v in load_json(path)["zone_factors"].items()}


def representative_day(bundle: DataBundle) -> pd.Timestamp:
    """Median-volume weekday (Tue-Thu) with no calendar event in any city."""
    cal = bundle.calendar
    daily = bundle.orders.groupby("order_date").size()
    flags = cal.groupby("date").agg(any_event=("is_event", "any"), dow=("dow", "first"))
    cand = flags[(~flags["any_event"]) & (flags["dow"].isin([1, 2, 3]))].index.to_series()
    vols = daily.reindex(cand)
    target = vols.median()
    return pd.Timestamp((vols - target).abs().idxmin())


def orders_for_day(bundle: DataBundle, day: pd.Timestamp) -> pd.DataFrame:
    return bundle.orders[bundle.orders["order_date"] == day].reset_index(drop=True)


def eligible(order: pd.Series, veh: pd.Series) -> bool:
    """Vehicle eligibility: capacity, single-parcel limit and zone restrictions."""
    if order["weight_kg"] > veh["capacity_kg"] or order["weight_kg"] > veh["max_order_kg"]:
        return False
    restricted = [z for z in str(veh["restricted_zones"]).split(";") if z]
    return order["zone"] not in restricted


def build_depot_problem(orders: pd.DataFrame, depot: pd.Series, vehicles: pd.DataFrame,
                        cfg: dict[str, Any], calib: dict[str, float]) -> DepotProblem:
    """Assemble distance matrices and node attributes for one depot."""
    lats = np.concatenate([[depot["lat"]], orders["lat"].to_numpy()])
    lons = np.concatenate([[depot["lon"]], orders["lon"].to_numpy()])
    straight = haversine_matrix(lats, lons)
    road = straight * float(cfg["geography"]["planning_circuity"])
    zones = [depot.get("zone", "")] + orders["zone"].tolist()
    zf = np.array([calib.get(z, 1.0) for z in zones], dtype=float)
    zf[0] = 1.0
    return DepotProblem(depot=depot, orders=orders.reset_index(drop=True), vehicles=vehicles.reset_index(drop=True),
                        straight_km=straight, road_km=road, zone_factor=zf)


def _plan_minutes(p: DepotProblem) -> np.ndarray:
    """Planned clock minute at each node used to pick the traffic band in the solver."""
    mid = ((p.orders["tw_start_min"] + p.orders["tw_end_min"]) / 2.0).to_numpy()
    return np.concatenate([[float(p.depot["open_min"])], mid])


def _solver_time_matrix(p: DepotProblem, speed: float, bands: list[dict[str, Any]]) -> list[list[int]]:
    """Integer transit matrix (0.1 min) = service at origin + travel with planned traffic."""
    plan = _plan_minutes(p)
    service = np.concatenate([[0.0], p.orders["service_min"].to_numpy()])
    # Conservative planning band: the slower of the origin and destination
    # planned clock times, so replayed routes rarely overrun windows or duty.
    f = traffic_factor(plan, bands)
    tf = np.maximum(f[:, None], f[None, :])
    tf[0, :] = f
    travel = p.road_km / speed * 60.0 * tf * p.zone_factor[None, :]
    mat = (service[:, None] + travel) * TIME_SCALE
    np.fill_diagonal(mat, 0.0)
    return np.rint(mat).astype(int).tolist()


# =========================================================================== OR-Tools solve
def solve_depot(p: DepotProblem, cfg: dict[str, Any], time_limit_s: int | None = None) -> tuple[dict[str, list[str]], str]:
    """Solve one depot CVRPTW. Returns {vehicle_id: [order_id, ...]} and solver status."""
    if p.orders.empty or p.vehicles.empty:
        return {}, "EMPTY"
    rcfg = cfg["routing"]
    bands = cfg["traffic"]["bands"]
    objective = rcfg.get("objective", "cost")
    n_nodes = len(p.orders) + 1
    n_veh = len(p.vehicles)
    manager = pywrapcp.RoutingIndexManager(n_nodes, n_veh, 0)
    routing = pywrapcp.RoutingModel(manager)

    vtypes = p.vehicles["vehicle_type"].tolist()
    time_cb: dict[str, int] = {}
    cost_cb: dict[str, int] = {}
    for vt in sorted(set(vtypes)):
        row = p.vehicles[p.vehicles["vehicle_type"] == vt].iloc[0]
        time_cb[vt] = routing.RegisterTransitMatrix(_solver_time_matrix(p, float(row["speed_kmph"]), bands))
        if objective == "cost":
            cmat = np.rint(p.road_km * float(row["cost_per_km"]) * COST_SCALE).astype(int).tolist()
        elif objective == "distance":
            cmat = np.rint(p.road_km * DIST_SCALE).astype(int).tolist()
        else:
            cmat = _solver_time_matrix(p, float(row["speed_kmph"]), bands)
        cost_cb[vt] = routing.RegisterTransitMatrix(cmat)
    for v, vt in enumerate(vtypes):
        routing.SetArcCostEvaluatorOfVehicle(cost_cb[vt], v)

    # Time dimension (minutes since midnight x TIME_SCALE)
    horizon = 1440 * TIME_SCALE
    routing.AddDimensionWithVehicleTransits([time_cb[vt] for vt in vtypes], 240 * TIME_SCALE, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    open_t = int(p.depot["open_min"] * TIME_SCALE)
    close_t = int(p.depot["close_min"] * TIME_SCALE)
    for i, o in enumerate(p.orders.itertuples(), start=1):
        time_dim.CumulVar(manager.NodeToIndex(i)).SetRange(int(o.tw_start_min * TIME_SCALE), int(o.tw_end_min * TIME_SCALE))
    for v in range(n_veh):
        veh = p.vehicles.iloc[v]
        time_dim.CumulVar(routing.Start(v)).SetRange(open_t, close_t)
        time_dim.CumulVar(routing.End(v)).SetRange(open_t, close_t)
        duty = float(veh["max_route_minutes"]) * (1.0 - float(rcfg.get("duty_buffer_pct", 0.0)))
        time_dim.SetSpanUpperBoundForVehicle(int(duty * TIME_SCALE), v)
        if objective == "cost":
            wage_per_unit = float(veh["driver_wage_per_hour"]) / 60.0 / TIME_SCALE * COST_SCALE
            time_dim.SetSpanCostCoefficientForVehicle(max(1, int(round(wage_per_unit))), v)
            routing.SetFixedCostOfVehicle(int(veh["fixed_cost_per_day"] * COST_SCALE), v)
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.Start(v)))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(routing.End(v)))

    # Capacity dimension (kg, rounded up)
    demands = [0] + [int(math.ceil(w)) for w in p.orders["weight_kg"]]
    dem_cb = routing.RegisterUnaryTransitVector(demands)
    routing.AddDimensionWithVehicleCapacity(dem_cb, 0, [int(c) for c in p.vehicles["capacity_kg"]], True, "Capacity")

    # Eligibility and optional visits
    penalty = int(rcfg["drop_penalty"] * COST_SCALE) if objective == "cost" else int(rcfg["drop_penalty"])
    for i in range(1, n_nodes):
        idx = manager.NodeToIndex(i)
        order = p.orders.iloc[i - 1]
        allowed = [v for v in range(n_veh) if eligible(order, p.vehicles.iloc[v])]
        routing.VehicleVar(idx).SetValues([-1] + allowed)
        routing.AddDisjunction([idx], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = getattr(routing_enums_pb2.FirstSolutionStrategy, rcfg["first_solution"])
    params.local_search_metaheuristic = getattr(routing_enums_pb2.LocalSearchMetaheuristic, rcfg["metaheuristic"])
    params.time_limit.seconds = int(time_limit_s or rcfg["time_limit_s"])
    solution = routing.SolveWithParameters(params)
    status = routing.status()
    status_name = {0: "NOT_SOLVED", 1: "SUCCESS", 2: "FAIL", 3: "FAIL_TIMEOUT", 4: "INVALID",
                   5: "INFEASIBLE", 6: "OPTIMAL", 7: "FEASIBLE"}.get(int(status), str(status))
    if solution is None:
        return {}, status_name
    routes: dict[str, list[str]] = {}
    for v in range(n_veh):
        idx = routing.Start(v)
        seq: list[str] = []
        idx = solution.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            seq.append(p.orders.iloc[manager.IndexToNode(idx) - 1]["order_id"])
            idx = solution.Value(routing.NextVar(idx))
        if seq:
            routes[p.vehicles.iloc[v]["vehicle_id"]] = seq
    return routes, status_name


# =========================================================================== baseline
def nearest_neighbor_depot(p: DepotProblem, cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Window-aware nearest-neighbour dispatch (the current manual practice).

    Pass 1 (Solomon-style): each vehicle in turn repeatedly visits the eligible
    order with the smallest *effective distance* = road km + waiting time
    expressed as km at vehicle speed, among orders it can still reach before
    the window closes. Pass 2: remaining orders are appended to the vehicle
    with the nearest last stop that still has capacity and duty time; these are
    delivered late. Anything left is reported unassigned. Capacity,
    eligibility and duty limits are always respected.
    """
    bands = cfg["traffic"]["bands"]
    unserved = set(range(len(p.orders)))
    open_min = float(p.depot["open_min"])
    close_min = float(p.depot["close_min"])
    state: dict[int, dict[str, Any]] = {}

    def arrive(v: int, j: int, cur: int, t: float) -> float:
        veh = p.vehicles.iloc[v]
        d = p.road_km[cur, j + 1]
        return t + float(d / veh["speed_kmph"] * 60.0 * traffic_factor(t, bands)) * p.zone_factor[j + 1]

    def back_ok(v: int, j: int, start: float, depart_depot: float) -> bool:
        veh = p.vehicles.iloc[v]
        o = p.orders.iloc[j]
        fin = start + o["service_min"]
        back = fin + float(p.road_km[j + 1, 0] / veh["speed_kmph"] * 60.0 * traffic_factor(fin, bands))
        return back - depart_depot <= veh["max_route_minutes"] and back <= close_min

    for v in range(len(p.vehicles)):
        veh = p.vehicles.iloc[v]
        st = {"seq": [], "load": 0.0, "cur": 0, "t": open_min, "start": None}
        while True:
            best, best_score, best_start = None, float("inf"), 0.0
            for j in unserved:
                o = p.orders.iloc[j]
                if st["load"] + o["weight_kg"] > veh["capacity_kg"] or not eligible(o, veh):
                    continue
                t0 = st["t"]
                if st["start"] is None:   # leave the depot to reach this stop at window opening
                    tt = arrive(v, j, 0, o["tw_start_min"]) - o["tw_start_min"]
                    t0 = max(open_min, o["tw_start_min"] - tt)
                arr = arrive(v, j, st["cur"], t0)
                if arr > o["tw_end_min"]:
                    continue
                start = max(arr, o["tw_start_min"])
                dep0 = t0 if st["start"] is None else st["start"]
                if not back_ok(v, j, start, dep0):
                    continue
                score = p.road_km[st["cur"], j + 1] + (start - arr) * veh["speed_kmph"] / 60.0
                if score < best_score:
                    best, best_score, best_start = j, score, start
                    best_t0 = t0
            if best is None:
                break
            if st["start"] is None:
                st["start"] = best_t0
            o = p.orders.iloc[best]
            st["t"] = best_start + o["service_min"]
            st["load"] += o["weight_kg"]
            st["cur"] = best + 1
            st["seq"].append(best)
            unserved.discard(best)
        state[v] = st

    # Pass 2: late deliveries appended to the nearest vehicle with spare capacity and duty time
    for j in sorted(unserved, key=lambda k: p.orders.iloc[k]["tw_end_min"]):
        o = p.orders.iloc[j]
        best_v, best_d = None, float("inf")
        for v, st in state.items():
            veh = p.vehicles.iloc[v]
            if st["load"] + o["weight_kg"] > veh["capacity_kg"] or not eligible(o, veh):
                continue
            dep0 = st["start"] if st["start"] is not None else open_min
            arr = arrive(v, j, st["cur"], st["t"])
            if not back_ok(v, j, max(arr, o["tw_start_min"]), dep0):
                continue
            d = p.road_km[st["cur"], j + 1]
            if d < best_d:
                best_v, best_d = v, d
        if best_v is None:
            continue
        st = state[best_v]
        arr = arrive(best_v, j, st["cur"], st["t"])
        st["start"] = st["start"] if st["start"] is not None else open_min
        st["t"] = max(arr, o["tw_start_min"]) + o["service_min"]
        st["load"] += o["weight_kg"]
        st["cur"] = j + 1
        st["seq"].append(j)
    return {p.vehicles.iloc[v]["vehicle_id"]: [p.orders.iloc[j]["order_id"] for j in st["seq"]]
            for v, st in state.items() if st["seq"]}


# =========================================================================== evaluator
def evaluate_routes(plan: str, routes: dict[str, list[str]], orders: pd.DataFrame, vehicles: pd.DataFrame,
                    depots: pd.DataFrame, cfg: dict[str, Any], calib: dict[str, float]) -> PlanResult:
    """Replay routes with departure-time-dependent traffic and compute cost and compliance.

    Departure from the depot is scheduled so the first stop is reached at its
    window opening (never before depot opening). Arrivals after the window end
    are late; service starts at max(arrival, window start).
    """
    bands = cfg["traffic"]["bands"]
    circ = float(cfg["geography"]["planning_circuity"])
    o_idx = orders.set_index("order_id")
    v_idx = vehicles.set_index("vehicle_id")
    d_idx = depots.set_index("depot_id")
    route_rows, stop_rows = [], []
    served: set[str] = set()

    def leg(lat1: float, lon1: float, lat2: float, lon2: float, depart: float, speed: float, zone: str) -> tuple[float, float, float]:
        s = float(haversine_matrix([lat1], [lon1], [lat2], [lon2])[0, 0])
        r = s * circ
        tt = r / speed * 60.0 * float(traffic_factor(depart, bands)) * calib.get(zone, 1.0)
        return s, r, tt

    for vid, seq in routes.items():
        if not seq:
            continue
        veh = v_idx.loc[vid]
        dep = d_idx.loc[veh["depot_id"]]
        speed = float(veh["speed_kmph"])
        first = o_idx.loc[seq[0]]
        # schedule departure to reach first window opening
        _, _, tt0 = leg(dep["lat"], dep["lon"], first["lat"], first["lon"], first["tw_start_min"], speed, first["zone"])
        t = max(float(dep["open_min"]), float(first["tw_start_min"]) - tt0)
        start_t = t
        lat, lon = dep["lat"], dep["lon"]
        load = straight_tot = road_tot = drive = wait = 0.0
        late_stops = 0
        for k, oid in enumerate(seq, start=1):
            o = o_idx.loc[oid]
            s_km, r_km, tt = leg(lat, lon, o["lat"], o["lon"], t, speed, o["zone"])
            arrival = t + tt
            start_service = max(arrival, float(o["tw_start_min"]))
            late = max(0.0, arrival - float(o["tw_end_min"]))
            w = start_service - arrival
            depart = start_service + float(o["service_min"])
            load += float(o["weight_kg"])
            straight_tot += s_km
            road_tot += r_km
            drive += tt
            wait += w
            late_stops += int(late > 0)
            stop_rows.append({"plan": plan, "city_id": veh["city_id"], "depot_id": veh["depot_id"], "vehicle_id": vid,
                              "vehicle_type": veh["vehicle_type"],
                              "seq": k, "order_id": oid, "segment": o["segment"], "zone": o["zone"], "lat": o["lat"],
                              "lon": o["lon"], "weight_kg": o["weight_kg"], "tw_start_min": o["tw_start_min"],
                              "tw_end_min": o["tw_end_min"], "arrival_min": round(arrival, 1),
                              "service_start_min": round(start_service, 1), "depart_min": round(depart, 1),
                              "wait_min": round(w, 1), "late_min": round(late, 1), "on_time": late <= 0.0,
                              "leg_straight_km": round(s_km, 3), "leg_road_km": round(r_km, 3),
                              "leg_travel_min": round(tt, 1)})
            served.add(oid)
            lat, lon, t = o["lat"], o["lon"], depart
        s_km, r_km, tt = leg(lat, lon, dep["lat"], dep["lon"], t, speed, dep.get("zone", ""))
        straight_tot += s_km
        road_tot += r_km
        drive += tt
        end_t = t + tt
        duration = end_t - start_t
        cost_fixed = float(veh["fixed_cost_per_day"])
        cost_km = road_tot * float(veh["cost_per_km"])
        cost_driver = duration / 60.0 * float(veh["driver_wage_per_hour"])
        route_rows.append({"plan": plan, "city_id": veh["city_id"], "depot_id": veh["depot_id"], "vehicle_id": vid,
                              "vehicle_type": veh["vehicle_type"],
                           "stops": len(seq), "load_kg": round(load, 1), "capacity_kg": float(veh["capacity_kg"]),
                           "load_util_pct": round(load / float(veh["capacity_kg"]) * 100, 1),
                           "straight_km": round(straight_tot, 2), "road_km": round(road_tot, 2),
                           "start_min": round(start_t, 1), "end_min": round(end_t, 1), "duration_min": round(duration, 1),
                           "drive_min": round(drive, 1), "wait_min": round(wait, 1),
                           "time_util_pct": round(duration / float(veh["max_route_minutes"]) * 100, 1),
                           "duration_violation": duration > float(veh["max_route_minutes"]) + 1e-6,
                           "capacity_violation": load > float(veh["capacity_kg"]) + 1e-6, "late_stops": late_stops,
                           "cost_fixed": round(cost_fixed, 2), "cost_km": round(cost_km, 2),
                           "cost_driver": round(cost_driver, 2), "cost_total": round(cost_fixed + cost_km + cost_driver, 2)})
    routes_df = pd.DataFrame(route_rows)
    stops_df = pd.DataFrame(stop_rows)
    un = orders[~orders["order_id"].isin(served)].copy()
    out_cost = cfg["routing"]["outsourcing_cost"]
    un["outsourcing_cost"] = un["segment"].map(out_cost).astype(float)
    un["reason"] = [_unassigned_reason(r, vehicles[vehicles["depot_id"] == r["depot_id"]]) for _, r in un.iterrows()]
    un["plan"] = plan
    metrics = summarize(plan, routes_df, stops_df, un, len(orders))
    return PlanResult(plan=plan, routes=routes_df, stops=stops_df,
                      unassigned=un[["plan", "order_id", "city_id", "depot_id", "segment", "zone", "lat", "lon", "weight_kg",
                                     "tw_start_min", "tw_end_min", "outsourcing_cost", "reason"]], metrics=metrics)


def _unassigned_reason(order: pd.Series, depot_vehicles: pd.DataFrame) -> str:
    if depot_vehicles.empty:
        return "no vehicles at depot"
    if not any(eligible(order, v) for _, v in depot_vehicles.iterrows()):
        return "no eligible vehicle (weight/zone)"
    return "capacity/time-window/duration limits"


def summarize(plan: str, routes: pd.DataFrame, stops: pd.DataFrame, unassigned: pd.DataFrame, n_orders: int) -> dict[str, Any]:
    """Plan-level KPIs. Compliance counts unassigned orders as non-compliant."""
    fleet_cost = float(routes["cost_total"].sum()) if not routes.empty else 0.0
    out_cost = float(unassigned["outsourcing_cost"].sum()) if not unassigned.empty else 0.0
    on_time = int(stops["on_time"].sum()) if not stops.empty else 0
    served = int(len(stops))
    return {
        "plan": plan, "orders": n_orders, "served": served, "unassigned": int(len(unassigned)),
        "on_time": on_time, "late": served - on_time,
        "tw_compliance": round(on_time / n_orders, 4) if n_orders else 0.0,
        "on_time_of_served": round(on_time / served, 4) if served else 0.0,
        "vehicles_used": int(len(routes)), "straight_km": round(float(routes["straight_km"].sum()), 1) if served else 0.0,
        "road_km": round(float(routes["road_km"].sum()), 1) if served else 0.0,
        "route_hours": round(float(routes["duration_min"].sum()) / 60.0, 2) if served else 0.0,
        "fleet_cost": round(fleet_cost, 0), "outsourcing_cost": round(out_cost, 0),
        "last_mile_cost": round(fleet_cost + out_cost, 0),
        "cost_per_order": round((fleet_cost + out_cost) / n_orders, 2) if n_orders else 0.0,
        "avg_load_util_pct": round(float(routes["load_util_pct"].mean()), 1) if served else 0.0,
        "avg_time_util_pct": round(float(routes["time_util_pct"].mean()), 1) if served else 0.0,
        "duration_violations": int(routes["duration_violation"].sum()) if served else 0,
        "capacity_violations": int(routes["capacity_violation"].sum()) if served else 0,
    }


# =========================================================================== day orchestration
def plan_day(orders: pd.DataFrame, vehicles: pd.DataFrame, depots: pd.DataFrame, cfg: dict[str, Any],
             calib: dict[str, float], time_limit_s: int | None = None, method: str = "ortools") -> PlanResult:
    """Plan all depots for a day with OR-Tools (``ortools``) or the baseline (``nearest``)."""
    all_routes: dict[str, list[str]] = {}
    status: dict[str, str] = {}
    for _, depot in depots.iterrows():
        dep_orders = orders[orders["depot_id"] == depot["depot_id"]]
        dep_veh = vehicles[vehicles["depot_id"] == depot["depot_id"]]
        prob = build_depot_problem(dep_orders, depot, dep_veh, cfg, calib)
        if method == "ortools":
            tl = time_limit_s or int(min(cfg["routing"]["time_limit_s"],
                                         max(5, len(dep_orders) * float(cfg["routing"].get("seconds_per_order", 0.2)))))
            routes, st = solve_depot(prob, cfg, tl)
        else:
            routes, st = nearest_neighbor_depot(prob, cfg), "HEURISTIC"
        status[depot["depot_id"]] = st
        all_routes.update(routes)
    name = "optimized" if method == "ortools" else "baseline"
    res = evaluate_routes(name, all_routes, orders, vehicles, depots, cfg, calib)
    res.solver_status = status
    return res


def city_metrics(res: PlanResult, orders: pd.DataFrame) -> pd.DataFrame:
    """Plan KPIs per city (same definitions as the national summary)."""
    rows = []
    for cid, o in orders.groupby("city_id"):
        r = res.routes[res.routes["city_id"] == cid] if not res.routes.empty else res.routes
        s_ = res.stops[res.stops["city_id"] == cid] if not res.stops.empty else res.stops
        u = res.unassigned[res.unassigned["city_id"] == cid]
        rows.append({"city_id": cid, **summarize(res.plan, r, s_, u, len(o))})
    return pd.DataFrame(rows)


def compare(opt: PlanResult, base: PlanResult) -> dict[str, Any]:
    """Optimized vs baseline deltas, all computed from evaluated plans."""
    b, o = base.metrics, opt.metrics

    def pct(new: float, old: float) -> float:
        return round((old - new) / old * 100.0, 2) if old else 0.0

    return {"last_mile_cost_reduction_pct": pct(o["last_mile_cost"], b["last_mile_cost"]),
            "fleet_cost_reduction_pct": pct(o["fleet_cost"], b["fleet_cost"]),
            "road_km_reduction_pct": pct(o["road_km"], b["road_km"]),
            "route_hours_reduction_pct": pct(o["route_hours"], b["route_hours"]),
            "compliance_gain_pts": round((o["tw_compliance"] - b["tw_compliance"]) * 100.0, 2),
            "vehicles_saved": b["vehicles_used"] - o["vehicles_used"],
            "baseline_cost": b["last_mile_cost"], "optimized_cost": o["last_mile_cost"]}


# =========================================================================== maps
def route_map(results: list[PlanResult], depots: pd.DataFrame, path: Any, title: str) -> None:
    """Colour-coded Folium map (one layer per plan, one colour per vehicle)."""
    import folium

    m = folium.Map(location=[float(depots["lat"].mean()), float(depots["lon"].mean())], zoom_start=11,
                   tiles="OpenStreetMap", control_scale=True)
    for res in results:
        layer = folium.FeatureGroup(name=f"{res.plan.title()} routes", show=res.plan == "optimized")
        for i, (vid, grp) in enumerate(res.stops.groupby("vehicle_id", sort=True)):
            grp = grp.sort_values("seq")
            dep = depots.set_index("depot_id").loc[grp["depot_id"].iloc[0]]
            pts = [(dep["lat"], dep["lon"])] + list(zip(grp["lat"], grp["lon"])) + [(dep["lat"], dep["lon"])]
            color = "#" + SERIES[i % len(SERIES)]
            folium.PolyLine(pts, color=color, weight=3, opacity=0.85,
                            tooltip=f"{vid} ({grp['vehicle_type'].iloc[0]}) - {len(grp)} stops").add_to(layer)
            for r in grp.itertuples():
                folium.CircleMarker((r.lat, r.lon), radius=3, color=color, fill=True, fill_opacity=0.9,
                                    tooltip=f"{r.order_id} | {r.segment} | seq {r.seq} | "
                                            f"{'on time' if r.on_time else f'late {r.late_min:.0f} min'}").add_to(layer)
        layer.add_to(m)
        if not res.unassigned.empty:
            ulayer = folium.FeatureGroup(name=f"{res.plan.title()} unassigned", show=False)
            for r in res.unassigned.itertuples():
                folium.CircleMarker((r.lat, r.lon), radius=5, color="#C2413B", fill=True,
                                    tooltip=f"Unassigned {r.order_id}: {r.reason}").add_to(ulayer)
            ulayer.add_to(m)
    for d in depots.itertuples():
        folium.Marker((d.lat, d.lon), tooltip=f"{d.name} ({d.role})",
                      icon=folium.Icon(color="black", icon="home")).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.get_root().html.add_child(folium.Element(
        f"<div style='position:fixed;top:10px;left:50px;z-index:9999;background:white;padding:6px 10px;"
        f"font-family:Arial;font-size:13px;border:1px solid #CFD6DF'><b>{title}</b></div>"))
    m.save(str(path))


# =========================================================================== run
def run(cfg: dict[str, Any], bundle: DataBundle | None = None) -> dict[str, Any]:
    """Plan the representative day with OR-Tools and the baseline; export results and maps."""
    bundle = bundle or load_bundle(cfg)
    calib = load_calibration(cfg)
    rep = cfg["routing"].get("representative_day", "auto")
    day = representative_day(bundle) if rep == "auto" else pd.Timestamp(rep)
    orders = orders_for_day(bundle, day)
    log.info("Routing %s: %d orders, %d vehicles, calibration=%s", day.date(), len(orders), len(bundle.vehicles),
             {k: round(v, 3) for k, v in calib.items()} or "none")
    base = plan_day(orders, bundle.vehicles, bundle.depots, cfg, calib, method="nearest")
    opt = plan_day(orders, bundle.vehicles, bundle.depots, cfg, calib, method="ortools")
    comp = compare(opt, base)
    log.info("Baseline %s | Optimized %s | Delta %s", base.metrics, opt.metrics, comp)

    tdir = resolve_path(cfg, "tables_dir")
    write_table(pd.concat([base.routes, opt.routes]), tdir / "routes_summary.parquet", csv_copy=True)
    write_table(pd.concat([base.stops, opt.stops]), tdir / "route_stops.parquet", csv_copy=True)
    write_table(pd.concat([base.unassigned, opt.unassigned]), tdir / "unassigned_orders.parquet", csv_copy=True)
    cm = pd.concat([city_metrics(base, orders), city_metrics(opt, orders)], ignore_index=True)
    names = {k: v["name"] for k, v in cfg["cities"].items()}
    cm.insert(1, "city", cm["city_id"].map(names))
    write_table(cm, tdir / "routing_city_summary.parquet", csv_copy=True)
    by_city = {}
    for cid in cm["city_id"].unique():
        bm = cm[(cm["city_id"] == cid) & (cm["plan"] == "baseline")].iloc[0]
        om = cm[(cm["city_id"] == cid) & (cm["plan"] == "optimized")].iloc[0]
        by_city[cid] = {"baseline_cost": float(bm["last_mile_cost"]), "optimized_cost": float(om["last_mile_cost"]),
                        "last_mile_cost_reduction_pct": round((bm["last_mile_cost"] - om["last_mile_cost"]) / bm["last_mile_cost"] * 100, 2),
                        "fleet_cost_reduction_pct": round((bm["fleet_cost"] - om["fleet_cost"]) / bm["fleet_cost"] * 100, 2),
                        "baseline_compliance": float(bm["tw_compliance"]), "optimized_compliance": float(om["tw_compliance"])}
    summary = {"day": str(day.date()), "orders": len(orders), "calibration": calib,
               "baseline": base.metrics, "optimized": opt.metrics, "comparison": comp, "by_city": by_city,
               "solver_status": opt.solver_status, "objective": cfg["routing"]["objective"],
               "time_limit_s": cfg["routing"]["time_limit_s"]}
    save_json(summary, tdir / "routing_summary.json")
    mdir = resolve_path(cfg, "maps_dir")
    for cid in cfg["cities"]:
        sub = [PlanResult(r.plan, r.routes[r.routes["city_id"] == cid], r.stops[r.stops["city_id"] == cid],
                          r.unassigned[r.unassigned["city_id"] == cid], r.metrics) for r in (opt, base)]
        route_map(sub, bundle.depots[bundle.depots["city_id"] == cid], mdir / f"route_map_{cid}.html",
                  f"{names[cid]} routes {day.date()} - optimized vs manual baseline")
    return {"summary": summary, "optimized": opt, "baseline": base}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    parser.add_argument("--time-limit", type=int, default=None)
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.time_limit:
        cfg["routing"]["time_limit_s"] = args.time_limit
    run(cfg)


if __name__ == "__main__":
    main()
