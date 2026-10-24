"""Shared utilities: geo maths, configuration, validation, solver shim, Command Center contract."""
import numpy as np
import pandas as pd
import pulp
import pytest
from geopy.distance import great_circle

from src.shared.command_center import publish_contract, read_contract, upstream_status
from src.shared.config import ConfigError, get_by_path, load_config, set_by_path
from src.shared.geo import haversine_km, haversine_matrix
from src.shared.solvers import get_mip_solver, new_var, solve
from src.shared.validation import ColumnRule, TableContract, check_foreign_key


def test_haversine_matches_geopy_across_states():
    # Mumbai CSMT -> Kolkata Howrah station, and Delhi CP -> Bengaluru MG Road
    pairs = [((18.9398, 72.8355), (22.5836, 88.3426)), ((28.6315, 77.2167), (12.9750, 77.6060))]
    for a, b in pairs:
        assert haversine_km(*a, *b) == pytest.approx(great_circle(a, b).km, rel=1e-6)


def test_haversine_matrix_symmetric_zero_diagonal():
    lats, lons = np.array([19.0, 28.6, 13.0]), np.array([72.8, 77.2, 80.2])
    m = haversine_matrix(lats, lons)
    assert np.allclose(m, m.T) and np.allclose(np.diag(m), 0.0)
    assert m[0, 1] > 1000  # Mumbai-Delhi > 1,000 km


def test_config_paths_and_overrides():
    cfg = load_config(overrides={"routing.time_limit_s": 7})
    assert get_by_path(cfg, "routing.time_limit_s") == 7
    assert set(cfg["cities"]) == {"MUM", "DEL", "BLR", "CHE", "KOL"}
    set_by_path(cfg, "new.nested.key", 3)
    assert cfg["new"]["nested"]["key"] == 3
    with pytest.raises(ConfigError):
        get_by_path(cfg, "routing.does_not_exist")


def test_register_keys_resolve():
    cfg = load_config()
    for item in cfg["register"]:
        get_by_path(cfg, item["key"])


def test_table_contract_detects_violations():
    contract = TableContract("t", [ColumnRule("id", "string", unique=True), ColumnRule("x", "numeric", min=0, max=10),
                                   ColumnRule("missing")])
    df = pd.DataFrame({"id": ["a", "a"], "x": [-1.0, None]})
    issues = " | ".join(contract.validate(df))
    for frag in ("missing column", "null", "below 0", "duplicate"):
        assert frag in issues


def test_foreign_key_check():
    child = pd.DataFrame({"k": [1, 2, 3]})
    parent = pd.DataFrame({"k": [1, 2]})
    assert check_foreign_key(child, "k", parent, "k", "fk")
    assert not check_foreign_key(parent, "k", child, "k", "fk")


def test_solver_shim_solves_small_integer_program():
    prob = pulp.LpProblem("t", pulp.LpMinimize)
    x = new_var(prob, "x", 0, 1, "Binary")
    y = new_var(prob, "y", 0, 5, "Integer")
    prob += 3 * x + y
    prob += x + y >= 2
    assert solve(prob, get_mip_solver(10, pure_integer=True)) == "Optimal"
    assert x.value() == pytest.approx(0) and y.value() == pytest.approx(2)


def test_command_center_contract_roundtrip(tmp_path):
    assert read_contract(tmp_path, "inventory") is None
    publish_contract(tmp_path, "logistics", {"kpi": 1}, {"lanes": []})
    data = read_contract(tmp_path, "logistics")
    assert data["kpis"]["kpi"] == 1 and data["module"] == "logistics"
    assert upstream_status(tmp_path)["logistics"] == "available"
    assert upstream_status(tmp_path)["inventory"] == "not run"


def test_save_json_is_strict(tmp_path):
    import json
    import math

    from src.shared.io_utils import save_json
    p = save_json({"a": float("nan"), "b": [np.float64("inf"), 1.5]}, tmp_path / "x.json")
    data = json.loads(p.read_text(), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert data == {"a": None, "b": [None, 1.5]} and not math.isnan(data["b"][1])
