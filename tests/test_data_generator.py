"""Dataset generation, multi-state geography, data contracts and determinism."""
import pandas as pd

from src.logistics import data_generator as dg
from src.logistics.common import city_of, traffic_factor, travel_minutes


def test_bundle_passes_all_contracts(bundle, cfg):
    assert dg.validate_bundle(bundle, cfg) == []


def test_generation_is_deterministic(cfg):
    a, b = dg.generate_all(cfg), dg.generate_all(cfg)
    pd.testing.assert_frame_equal(a.orders, b.orders)
    pd.testing.assert_frame_equal(a.trip_logs, b.trip_logs)


def test_addresses_cover_cities_zones_and_states(bundle, cfg):
    a = bundle.addresses
    for cid, c in cfg["cities"].items():
        sub = a[a["city_id"] == cid]
        assert len(sub) == cfg["addresses"]["count_per_city"]
        assert set(sub["zone"]) == set(c["zones"])
        b = c["bbox"]
        assert sub["lat"].between(b["lat_min"], b["lat_max"]).all()
        assert sub["lon"].between(b["lon_min"], b["lon_max"]).all()
    assert {"Maharashtra", "West Bengal"} <= set(a["state"])


def test_full_config_spans_seven_states():
    from src.shared.config import load_config
    cfg = load_config()
    states = {z["state"] for c in cfg["cities"].values() for z in c["zones"].values()}
    assert {"Maharashtra", "Delhi", "Haryana", "Uttar Pradesh", "Karnataka", "Tamil Nadu", "West Bengal"} <= states


def test_each_city_has_25_vehicles_by_role(bundle, cfg):
    alloc = cfg["fleet"]["allocation_by_role"]
    for cid in cfg["cities"]:
        v = bundle.vehicles[bundle.vehicles["city_id"] == cid]
        assert len(v) == 25
        for role, mix in alloc.items():
            depot = bundle.depots[(bundle.depots["city_id"] == cid) & (bundle.depots["role"] == role)]["depot_id"].iloc[0]
            got = v[v["depot_id"] == depot]["vehicle_type"].value_counts().to_dict()
            assert {k: n for k, n in mix.items() if n} == got


def test_orders_ship_from_same_city_stocking_depot(bundle, cfg):
    o = bundle.orders.merge(bundle.depots[["depot_id", "city_id", "role"]], on="depot_id", suffixes=("", "_d"))
    assert (o["city_id"] == o["city_id_d"]).all()
    roles = cfg["orders"]["stocking_roles"]
    assert all(r in roles[s] for s, r in zip(o["segment"], o["role"]))
    assert all(city_of(x) == c for x, c in zip(o["order_id"].head(50), o["city_id"].head(50)))


def test_time_windows_inside_depot_hours(bundle):
    o = bundle.orders
    assert (o["tw_start_min"] < o["tw_end_min"]).all()
    assert (o["tw_start_min"] >= 420).all() and (o["tw_end_min"] <= 1290).all()


def test_regional_events_only_in_their_city(bundle):
    cal = bundle.calendar
    durga = cal[cal["event"].str.contains("Durga Puja")]
    assert set(durga["city_id"]) == {"KOL"}
    visarjan = cal[cal["event"].str.contains("Ganesh Visarjan")]
    assert set(visarjan["city_id"]) == {"MUM"}


def test_slot_template_zones_and_ergonomics(bundle, cfg):
    s = bundle.slots
    assert len(s) == 10_000
    assert s["zone"].value_counts().to_dict() == cfg["warehouse"]["zone_slot_counts"]
    assert set(s.loc[s["heavy_ok"], "level"]) == {1, 2}
    a_max = s.loc[s["zone"] == "A", "travel_m"].max()
    assert a_max <= s.loc[s["zone"] == "C", "travel_m"].min()


def test_master_register_has_values(cfg):
    reg = dg.master_register(cfg)
    loaded = [r for r in cfg["register"] if not r["key"].startswith("cities.") or r["key"].split(".")[1] in cfg["cities"]]
    assert len(reg) == len(loaded) and reg["value"].notna().all()


def test_traffic_bands_and_travel_time(cfg):
    bands = cfg["traffic"]["bands"]
    assert traffic_factor([360, 540, 720, 1080], bands).tolist() == [1.0, 1.8, 1.1, 2.2]
    assert float(travel_minutes(30.0, 30.0, 1080, bands)) == 60.0 * 2.2
