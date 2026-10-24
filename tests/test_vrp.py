"""Routing: eligibility, OR-Tools CVRPTW feasibility, evaluator arithmetic, baseline behaviour."""
import numpy as np
import pandas as pd
import pytest

from src.logistics import vrp_optimizer as vrp
from src.logistics.data_generator import vehicle_row


def _depot():
    return pd.Series({"depot_id": "DEP-MUM-MFC", "city_id": "MUM", "lat": 19.1176, "lon": 72.8697,
                      "open_min": 420, "close_min": 1290, "zone": "Andheri", "role": "MFC"})


def _orders(n=12, seed=1, zone="Andheri", kg=(2, 60)):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "order_id": [f"ORD-MUM-{i:06d}" for i in range(n)], "city_id": "MUM", "depot_id": "DEP-MUM-MFC",
        "zone": zone, "segment": "ECOM_B2C", "lat": 19.12 + rng.normal(0, 0.01, n), "lon": 72.86 + rng.normal(0, 0.01, n),
        "weight_kg": rng.uniform(*kg, n).round(1), "tw_start_min": 540, "tw_end_min": 1080, "service_min": 4.0})


def _vehicles(cfg, types):
    return pd.DataFrame([vehicle_row(cfg, f"VEH-MUM-{i:03d}", t, "DEP-MUM-MFC", "MUM") for i, t in enumerate(types)])


def test_eligibility_rules(cfg):
    van, bike = (pd.Series(vehicle_row(cfg, "v", t, "d", "MUM")) for t in ("VAN_2T", "TWO_WHEELER"))
    assert not vrp.eligible(pd.Series({"weight_kg": 50, "zone": "South Mumbai"}), van)      # restricted zone
    assert vrp.eligible(pd.Series({"weight_kg": 50, "zone": "Andheri"}), van)
    assert not vrp.eligible(pd.Series({"weight_kg": 16, "zone": "Andheri"}), bike)          # > single-parcel limit
    assert not vrp.eligible(pd.Series({"weight_kg": 50, "zone": "Central Kolkata"}), van)   # restriction in another state


def test_ortools_solution_is_feasible(cfg):
    orders, veh = _orders(), _vehicles(cfg, ["TEMPO_1T", "TEMPO_1T"])
    prob = vrp.build_depot_problem(orders, _depot(), veh, cfg, {})
    routes, status = vrp.solve_depot(prob, cfg, time_limit_s=3)
    assert status in {"SUCCESS", "OPTIMAL", "FEASIBLE"}
    served = [o for seq in routes.values() for o in seq]
    assert sorted(served) == sorted(orders["order_id"])          # every order exactly once
    res = vrp.evaluate_routes("optimized", routes, orders, veh, pd.DataFrame([_depot()]), cfg, {})
    assert not res.routes["capacity_violation"].any()
    assert res.metrics["unassigned"] == 0


def test_ineligible_orders_reported_unassigned(cfg):
    orders = _orders(n=6, kg=(30, 30))                          # 180 kg total
    veh = _vehicles(cfg, ["TWO_WHEELER"] * 6)                   # 40 kg each but 15 kg parcel limit
    prob = vrp.build_depot_problem(orders, _depot(), veh, cfg, {})
    routes, _ = vrp.solve_depot(prob, cfg, time_limit_s=2)
    assert routes == {}                                          # nothing eligible -> all dropped
    res = vrp.evaluate_routes("optimized", routes, orders, veh, pd.DataFrame([_depot()]), cfg, {})
    assert res.metrics["unassigned"] == 6
    assert set(res.unassigned["reason"]) == {"no eligible vehicle (weight/zone)"}


def test_evaluator_cost_arithmetic(cfg):
    orders = _orders(n=2)
    veh = _vehicles(cfg, ["TEMPO_1T"])
    res = vrp.evaluate_routes("x", {"VEH-MUM-000": list(orders["order_id"])}, orders, veh, pd.DataFrame([_depot()]), cfg, {})
    r = res.routes.iloc[0]
    v = veh.iloc[0]
    expected = v["fixed_cost_per_day"] + r["road_km"] * v["cost_per_km"] + r["duration_min"] / 60 * v["driver_wage_per_hour"]
    assert r["cost_total"] == pytest.approx(expected, abs=0.5)      # components rounded to 2 dp
    assert r["road_km"] == pytest.approx(r["straight_km"] * cfg["geography"]["planning_circuity"], abs=0.02)


def test_evaluator_flags_late_arrivals(cfg):
    orders = _orders(n=1)
    orders.loc[0, ["lat", "lon", "tw_start_min", "tw_end_min"]] = [19.28, 73.04, 425, 430]   # far, window just after opening
    veh = _vehicles(cfg, ["TEMPO_1T"])
    res = vrp.evaluate_routes("x", {"VEH-MUM-000": list(orders["order_id"])}, orders, veh, pd.DataFrame([_depot()]), cfg, {})
    assert res.stops["late_min"].iloc[0] > 0 and res.metrics["tw_compliance"] == 0.0


def test_nearest_neighbour_respects_capacity(cfg):
    orders = _orders(n=10, kg=(300, 300))
    veh = _vehicles(cfg, ["TEMPO_1T", "TEMPO_1T", "TEMPO_1T", "TEMPO_1T"])
    prob = vrp.build_depot_problem(orders, _depot(), veh, cfg, {})
    routes = vrp.nearest_neighbor_depot(prob, cfg)
    for seq in routes.values():
        assert orders.set_index("order_id").loc[seq, "weight_kg"].sum() <= 1000
    assert sum(len(s) for s in routes.values()) == 10


def test_compare_percentages():
    mk = lambda c, f, km, h, comp, v: vrp.PlanResult("p", pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),  # noqa: E731
        {"last_mile_cost": c, "fleet_cost": f, "road_km": km, "route_hours": h, "tw_compliance": comp, "vehicles_used": v})
    out = vrp.compare(mk(80, 80, 90, 9, 0.99, 8), mk(100, 90, 100, 10, 0.95, 10))
    assert out["last_mile_cost_reduction_pct"] == 20.0 and out["vehicles_saved"] == 2
    assert out["compliance_gain_pts"] == pytest.approx(4.0)
