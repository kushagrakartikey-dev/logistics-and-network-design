"""Slotting, discrete-event simulation and ML validation discipline."""
import numpy as np
import pandas as pd
import pytest

from src.logistics import ml_models as ml
from src.logistics import simulation_engine as sim
from src.logistics import warehouse_slotter as ws


@pytest.fixture(scope="module")
def picks(bundle):
    return bundle.pick_lines[bundle.pick_lines["city_id"] == "MUM"]


def test_optimized_layout_is_one_to_one_and_ergonomic(bundle, cfg, picks):
    freq = ws.estimate_velocity(picks, bundle.skus, 0.25)
    lay = ws.optimized_layout(bundle.skus, bundle.slots, freq, ws.affinity_clusters(picks, 42), cfg)
    assert lay["slot_id"].is_unique and len(lay) == len(bundle.skus)
    m = ws.evaluate_layout("o", lay, picks, bundle.skus, bundle.slots, cfg)
    assert m["heavy_slot_violations"] == 0


def test_velocity_slotting_beats_reverse_layout(bundle, cfg, picks):
    freq = ws.pick_frequency(picks, bundle.skus)
    good = ws.optimized_layout(bundle.skus, bundle.slots, freq, {}, cfg)
    bad = ws.optimized_layout(bundle.skus, bundle.slots, -freq, {}, cfg)
    mg = ws.evaluate_layout("g", good, picks, bundle.skus, bundle.slots, cfg, holdout=False)
    mb = ws.evaluate_layout("b", bad, picks, bundle.skus, bundle.slots, cfg, holdout=False)
    assert mg["travel_m"] < mb["travel_m"] and mg["picks_per_hour"] > mb["picks_per_hour"]


def test_walking_sequence_covers_all_slots(bundle):
    seq = ws.walking_sequence(bundle.slots)
    assert len(seq) == len(bundle.slots) and seq["slot_id"].is_unique


def _profile(n=120):
    rng = np.random.default_rng(3)
    return sim.DemandProfile("t", float(n), rng.uniform(420, 1080, 500), 1 + rng.poisson(3, 500),
                             np.array(["ECOM_B2C"] * 500))


def test_simulation_deterministic_and_bounded(cfg):
    inputs = sim.SimInputs(30.0, np.array([30.0, 45.0]), cfg)
    staff = {"pickers": 3, "packers": 2, "qc": 1, "loaders": 1}
    a, b = sim.simulate_day(_profile(), staff, inputs, 7), sim.simulate_day(_profile(), staff, inputs, 7)
    assert a["dispatched"] == b["dispatched"] and a["cycle_p95"] == b["cycle_p95"]
    assert a["dispatched"] <= a["orders"]
    assert all(0.0 <= a[f"util_{r}"] <= 1.0 for r in sim.ROLES)


def test_more_staff_does_not_slow_cycle(cfg):
    inputs = sim.SimInputs(30.0, np.array([30.0]), cfg)
    lean = {"pickers": 1, "packers": 1, "qc": 1, "loaders": 1}
    rich = {"pickers": 6, "packers": 5, "qc": 2, "loaders": 3}
    dl, pl = sim.monte_carlo("lean", _profile(), lean, inputs, 10, 1)
    dr, pr = sim.monte_carlo("rich", _profile(), rich, inputs, 10, 1)
    assert np.quantile(pr, 0.95) < np.quantile(pl, 0.95)
    assert sim.staffing_cost(rich, cfg) > sim.staffing_cost(lean, cfg)


def test_metrics_function():
    m = ml.metrics(np.array([10.0, 20.0]), np.array([12.0, 18.0]))
    assert m["MAE"] == 2.0 and m["RMSE"] == 2.0 and m["MAPE"] == pytest.approx(15.0)


def test_time_split_has_no_leakage():
    dates = pd.Series(pd.date_range("2025-09-01", periods=90))
    train, cutoff = ml.time_split(dates, 21)
    assert dates[train].max() < cutoff <= dates[~train].min() and (~train).sum() == 21


def test_travel_model_outputs(bundle, cfg):
    out = ml.train_travel_model(bundle, cfg)
    assert set(out["zone_factors"]) <= set(bundle.addresses["zone"])
    assert all(0.5 < v < 2.0 for v in out["zone_factors"].values())
    assert out["validation_rows"] > 0 and out["model"]["MAE"] > 0
