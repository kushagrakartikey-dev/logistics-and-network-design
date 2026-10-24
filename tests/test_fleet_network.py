"""Fleet TCO/scaling and network design economics."""
import numpy as np
import pandas as pd
import pytest

from src.logistics import fleet_sizer as fs
from src.logistics import network_designer as nd


def test_equivalent_annual_cost_is_annuity_without_residual():
    r, n = 0.12, 5
    assert fs.equivalent_annual_cost(1_000_000, 0, r, n) == pytest.approx(1_000_000 * r / (1 - (1 + r) ** -n))
    assert fs.equivalent_annual_cost(1_000_000, 200_000, r, n) < fs.equivalent_annual_cost(1_000_000, 0, r, n)


def test_scale_orders_sizes_and_unique_ids(bundle):
    from src.logistics.vrp_optimizer import representative_day
    day = representative_day(bundle)
    base = (bundle.orders["order_date"] == day).sum()
    for m in (0.5, 2.0):
        o = fs.scale_orders(bundle, day, m, 42)
        assert len(o) == round(m * base) and o["order_id"].is_unique
        assert set(o["city_id"]) <= set(bundle.orders["city_id"])


def test_candidate_pool_minimum_per_type(cfg, bundle):
    pool = fs.candidate_pool(cfg, bundle.depots, 1.0)
    counts = pool.groupby(["depot_id", "vehicle_type"]).size()
    assert counts.min() >= 2 and len(counts) == len(bundle.depots) * 3
    assert pool["vehicle_id"].is_unique


def test_fleet_tco_components(cfg):
    routes = pd.DataFrame({"vehicle_type": ["VAN_2T", "TEMPO_1T"], "road_km": [100.0, 80.0], "duration_min": [480, 420],
                           "time_util_pct": [80, 70], "load_util_pct": [60, 50]})
    t = fs.fleet_tco(cfg, routes, wage_index=0.85).set_index("vehicle_type")
    days = cfg["fleet"]["operating_days_per_year"]
    assert t.at["VAN_2T", "annual_km"] == 100 * days
    assert t.at["VAN_2T", "labour"] == pytest.approx(8 * days * 230 * 0.85, rel=1e-3)
    assert set(t["preferred"]) <= {"own", "lease"}


def test_npv_known_value():
    assert nd.npv([-100, 60, 60], 0.10) == pytest.approx(-100 + 60 / 1.1 + 60 / 1.21)


def test_p_median_matches_brute_force():
    from itertools import combinations
    rng = np.random.default_rng(0)
    d = rng.uniform(1, 20, (12, 6))       # 1 existing + 5 candidates
    w = rng.uniform(0.5, 3, 12)
    chosen, assign, status = nd.solve_p_median(d, w, w, 1, 2, None)
    best = min(combinations(range(5), 2), key=lambda c: (w * d[:, [0] + [1 + k for k in c]].min(axis=1)).sum())
    assert status == "Optimal" and sorted(chosen) == sorted(best)
    assert np.allclose(assign.sum(axis=1), 1.0)


def test_p_median_capacity_respected():
    d = np.array([[10.0, 1.0, 9.0]] * 4)
    w = np.ones(4)
    _, assign, _ = nd.solve_p_median(d, w, w, 1, 1, capacity=np.array([2.0, 2.0]))
    assert (assign[:, 1:] * w[:, None]).sum(axis=0).max() <= 2.0 + 1e-6


def test_break_even_multiplier_zeroes_npv(cfg):
    sites = pd.DataFrame({"capex_inr": [6_500_000], "annual_fixed_opex_inr": [5_000_000]})
    eco = nd.scenario_economics(2000, 1400, 1, sites, 30.0, 8.0, cfg)
    k = nd.break_even_multiplier(eco, cfg)
    eco_k = nd.scenario_economics(2000 * k, 1400 * k, 1, sites, 30.0, 8.0, cfg)
    assert eco_k["npv"] == pytest.approx(0.0, abs=1.0)
