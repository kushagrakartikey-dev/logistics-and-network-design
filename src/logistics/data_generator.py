"""Operational dataset builder for the multi-state last-mile and network scenario.

Five metro clusters - Mumbai MMR (Maharashtra), Delhi NCR (Delhi, Haryana,
Uttar Pradesh), Bengaluru (Karnataka), Chennai (Tamil Nadu) and Kolkata
(West Bengal) - each with three depots, 500 addresses, a 25-vehicle fleet and
MFC candidate sites. Builds every table the analytical modules consume — depots, vehicle master,
delivery addresses, a 90-day festive-season order book, telematics trip logs,
warehouse SKU master, 10,000 storage slots, pick history, MFC candidate sites
and staffing inputs — from ``config/logistics.yaml`` with a fixed seed, then
validates each table against its data contract.

Run: ``python -m src.logistics.data_generator``
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd

from src.shared.config import ConfigError, get_by_path, load_config, resolve_path
from src.shared.geo import haversine_km
from src.shared.logging_utils import get_logger
from src.shared.validation import DataValidationError, check_foreign_key

from .common import CONTRACTS, SEGMENTS, DataBundle, traffic_factor

log = get_logger(__name__)


# =========================================================================== masters
def build_depots(cfg: dict[str, Any]) -> pd.DataFrame:
    """Depot master for every city cluster."""
    rows = []
    for cid, c in cfg["cities"].items():
        for d in c["depots"]:
            rows.append({**d, "city_id": cid, "city": c["name"], "state": c["state"],
                         "zone": c["depot_zone"][d["role"]], "open_min": 420, "close_min": 1290})
    return pd.DataFrame(rows)


def build_vehicle_types(cfg: dict[str, Any]) -> pd.DataFrame:
    """One row per vehicle type with economics and operating limits (Mumbai wage basis)."""
    rows = []
    for vtype, p in cfg["fleet"]["vehicle_types"].items():
        rows.append({
            "vehicle_type": vtype, "label": p["label"], "fleet_count_per_city": p["count"],
            "capacity_kg": p["capacity_kg"], "speed_kmph": p["speed_kmph"], "cost_per_km": p["cost_per_km"],
            "fixed_cost_per_day": p["fixed_cost_per_day"], "driver_wage_per_hour": p["driver_wage_per_hour"],
            "max_route_minutes": p["max_route_minutes"], "restricted_zones": ";".join(p.get("restricted_zones", [])),
            "max_order_kg": p.get("max_order_kg", p["capacity_kg"]), "acquisition_cost": p["acquisition_cost"],
            "lease_per_month": p["lease_per_month"], "maintenance_per_km": p["maintenance_per_km"],
            "insurance_per_year": p["insurance_per_year"], "useful_life_years": p["useful_life_years"],
            "residual_value_pct": p["residual_value_pct"],
        })
    return pd.DataFrame(rows)


def vehicle_row(cfg: dict[str, Any], vehicle_id: str, vtype: str, depot_id: str, city_id: str) -> dict[str, Any]:
    """Vehicle master record with the city's wage level applied to driver cost."""
    p = cfg["fleet"]["vehicle_types"][vtype]
    wage = float(cfg["cities"][city_id]["wage_index"])
    return {"vehicle_id": vehicle_id, "vehicle_type": vtype, "depot_id": depot_id, "city_id": city_id,
            "capacity_kg": p["capacity_kg"], "speed_kmph": p["speed_kmph"], "cost_per_km": p["cost_per_km"],
            "fixed_cost_per_day": p["fixed_cost_per_day"],
            "driver_wage_per_hour": round(p["driver_wage_per_hour"] * wage, 2),
            "max_route_minutes": p["max_route_minutes"], "restricted_zones": ";".join(p.get("restricted_zones", [])),
            "max_order_kg": p.get("max_order_kg", p["capacity_kg"])}


def build_vehicles(cfg: dict[str, Any], depots: pd.DataFrame) -> pd.DataFrame:
    """Each city's 25 vehicles allocated to its depots by role."""
    vtypes = cfg["fleet"]["vehicle_types"]
    alloc = cfg["fleet"]["allocation_by_role"]
    for vtype, p in vtypes.items():
        allocated = sum(r.get(vtype, 0) for r in alloc.values())
        if allocated != p["count"]:
            raise DataValidationError(f"allocation_by_role for {vtype} = {allocated}, per-city count = {p['count']}")
    rows = []
    for cid in cfg["cities"]:
        n = 0
        for d in depots[depots["city_id"] == cid].itertuples():
            for vtype in vtypes:
                for _ in range(alloc[d.role].get(vtype, 0)):
                    n += 1
                    rows.append(vehicle_row(cfg, f"VEH-{cid}-{n:03d}", vtype, d.depot_id, cid))
    return pd.DataFrame(rows)


def build_addresses(cfg: dict[str, Any], rng: np.random.Generator) -> pd.DataFrame:
    """500 delivery addresses per city scattered around real locality anchors in five zones."""
    per_city = int(cfg["addresses"]["count_per_city"])
    seg_names = list(cfg["addresses"]["segment_mix"].keys())
    seg_p = np.array(list(cfg["addresses"]["segment_mix"].values()), dtype=float)
    seg_p /= seg_p.sum()
    default_sigma = float(cfg["geography"]["anchor_sigma_deg"])
    rows = []
    for cid, c in cfg["cities"].items():
        bbox = c["bbox"]
        zones = list(c["zones"].keys())
        shares = np.array([c["zones"][z]["share"] for z in zones], dtype=float)
        counts = np.floor(shares / shares.sum() * per_city).astype(int)
        counts[np.argmax(shares)] += per_city - counts.sum()
        n = 0
        for zone, count in zip(zones, counts):
            z = c["zones"][zone]
            sigma = float(z.get("sigma_deg", default_sigma))
            for _ in range(count):
                name, alat, alon = z["anchors"][rng.integers(len(z["anchors"]))]
                lat = float(np.clip(alat + rng.normal(0, sigma), bbox["lat_min"], bbox["lat_max"]))
                lon = float(np.clip(alon + rng.normal(0, sigma), bbox["lon_min"], bbox["lon_max"]))
                n += 1
                rows.append({"address_id": f"ADR-{cid}-{n:04d}", "city_id": cid, "city": c["name"], "state": z["state"],
                             "zone": zone, "locality": name, "lat": round(lat, 6), "lon": round(lon, 6),
                             "segment": str(rng.choice(seg_names, p=seg_p)),
                             "demand_weight": float(rng.lognormal(0.0, 0.6))})
    df = pd.DataFrame(rows)
    df["demand_weight"] = df["demand_weight"] / df.groupby(["city_id", "segment"])["demand_weight"].transform("sum")
    return df


def build_calendar(cfg: dict[str, Any], rng: np.random.Generator) -> pd.DataFrame:
    """City x day calendar: weekday effects, national + regional events, rain days."""
    cal = cfg["calendar"]
    dates = pd.date_range(cal["start_date"], periods=int(cal["days"]), freq="D")
    diwali = pd.Timestamp(cal["diwali_date"])
    frames = []
    for cid, c in cfg["cities"].items():
        df = pd.DataFrame({"date": dates})
        df["city_id"] = cid
        df["dow"] = df["date"].dt.dayofweek
        df["day_name"] = df["date"].dt.day_name()
        df["b2c_mult"] = [cal["weekday_factor"]["b2c"][d] for d in df["dow"]]
        df["b2b_mult"] = [cal["weekday_factor"]["b2b"][d] for d in df["dow"]]
        df["traffic_mult"] = 1.0
        df["event"] = ""
        for ev in list(cal["events"]) + list(c.get("events", [])):
            mask = (df["date"] >= pd.Timestamp(ev["start"])) & (df["date"] <= pd.Timestamp(ev["end"]))
            df.loc[mask, "b2c_mult"] *= ev["b2c"]
            df.loc[mask, "b2b_mult"] *= ev["b2b"]
            df.loc[mask, "traffic_mult"] *= ev["traffic"]
            prev = df.loc[mask, "event"]
            df.loc[mask, "event"] = np.where(prev == "", ev["name"], prev + " + " + ev["name"])
        p_rain = df["date"].dt.month.map(c["rain_probability_by_month"]).fillna(0.0).to_numpy()
        df["rain"] = rng.random(len(df)) < p_rain
        df["days_to_diwali"] = (diwali - df["date"]).dt.days
        df["is_event"] = df["event"] != ""
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _nearest_stocking_depot(addresses: pd.DataFrame, depots: pd.DataFrame, roles: dict[str, list[str]]) -> pd.Series:
    out = {}
    for row in addresses.itertuples():
        cands = depots[(depots["city_id"] == row.city_id) & (depots["role"].isin(roles[row.segment]))]
        d = haversine_km(row.lat, row.lon, cands["lat"].to_numpy(), cands["lon"].to_numpy())
        out[row.address_id] = cands["depot_id"].iloc[int(np.argmin(d))]
    return pd.Series(out)


def build_orders(cfg: dict[str, Any], addresses: pd.DataFrame, depots: pd.DataFrame,
                 calendar: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """90-day order book per city with windows, weight, lines, priority, service and intake times."""
    ocfg = cfg["orders"]
    base = float(ocfg["base_daily_orders"])
    fulfil = _nearest_stocking_depot(addresses, depots, ocfg["stocking_roles"])
    lo, hi = ocfg["intake_window_min"]
    rows = []
    for cid, c in cfg["cities"].items():
        weight_city = float(c["volume_weight"])
        by_seg = {s: addresses[(addresses["city_id"] == cid) & (addresses["segment"] == s)] for s in SEGMENTS}
        n = 0
        for day in calendar[calendar["city_id"] == cid].itertuples():
            for seg in SEGMENTS:
                pool = by_seg[seg]
                if pool.empty:
                    continue
                mult = day.b2c_mult if seg == "ECOM_B2C" else day.b2b_mult
                k = int(rng.poisson(base * weight_city * ocfg["segment_order_share"][seg] * mult))
                if k == 0:
                    continue
                idx = rng.choice(len(pool), size=k, p=pool["demand_weight"].to_numpy())
                w = ocfg["weight_kg"][seg]
                weights = np.clip(rng.lognormal(np.log(w["median"]), w["sigma"], k), w["min"], w["max"])
                lines = 1 + rng.poisson(max(ocfg["lines"][seg] - 1.0, 0.05), k)
                express = rng.random(k) < ocfg["express_share"][seg]
                peak = np.where(rng.random(k) < 0.55, rng.normal(630, 75, k), rng.normal(900, 90, k))
                intake = np.clip(peak, lo, hi)
                win_idx = rng.random(k)
                for j in range(k):
                    a = pool.iloc[int(idx[j])]
                    key = f"{seg}_EXPRESS" if express[j] and f"{seg}_EXPRESS" in ocfg["time_windows"] else seg
                    windows = ocfg["time_windows"][key]
                    tw = windows[int(win_idx[j] * len(windows))]
                    if seg == "ECOM_B2C":
                        service, stype = ocfg["service_minutes"]["ECOM_B2C"], "B2C_PARCEL"
                    else:
                        service = ocfg["service_minutes"]["B2B_BASE"] + ocfg["service_minutes"]["B2B_PER_KG"] * weights[j]
                        stype = {"FMCG_RETAIL": "B2B_CARTON", "MODERN_RETAIL": "B2B_PALLET", "AUTOMOTIVE": "JIT_PARTS"}[seg]
                    n += 1
                    rows.append({
                        "order_id": f"ORD-{cid}-{n:06d}", "order_date": day.date, "city_id": cid, "state": a["state"],
                        "address_id": a["address_id"], "zone": a["zone"], "lat": a["lat"], "lon": a["lon"],
                        "segment": seg, "service_type": stype, "priority": "EXPRESS" if express[j] else "STANDARD",
                        "weight_kg": round(float(weights[j]), 2), "lines": int(lines[j]),
                        "tw_start_min": int(tw[0]), "tw_end_min": int(tw[1]), "service_min": round(float(service), 2),
                        "depot_id": fulfil[a["address_id"]], "intake_min": round(float(intake[j]), 1), "event": day.event,
                    })
    df = pd.DataFrame(rows)
    df["order_date"] = pd.to_datetime(df["order_date"])
    return df


def build_trip_logs(cfg: dict[str, Any], addresses: pd.DataFrame, depots: pd.DataFrame,
                    calendar: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Telematics trip legs with observed travel minutes (city congestion and road effects included)."""
    tcfg = cfg["telematics"]
    vt = cfg["fleet"]["vehicle_types"]
    bands = cfg["traffic"]["bands"]
    vtypes = list(vt.keys())
    vprob = np.array([vt[v]["count"] for v in vtypes], dtype=float)
    vprob /= vprob.sum()
    per_day = int(tcfg["trip_logs_per_day"])
    rows = []
    for cid, c in cfg["cities"].items():
        circ = {z: c["zones"][z]["circuity"] for z in c["zones"]}
        nodes = pd.concat([
            depots.loc[depots["city_id"] == cid, ["depot_id", "zone", "lat", "lon"]].rename(columns={"depot_id": "node_id"}),
            addresses.loc[addresses["city_id"] == cid, ["address_id", "zone", "lat", "lon"]].rename(columns={"address_id": "node_id"}),
        ], ignore_index=True)
        n_dep = int((depots["city_id"] == cid).sum())
        lat, lon = nodes["lat"].to_numpy(), nodes["lon"].to_numpy()
        zone = nodes["zone"].to_numpy()
        n = 0
        for day in calendar[calendar["city_id"] == cid].itertuples():
            o = rng.integers(len(nodes), size=per_day)
            d = rng.integers(n_dep, len(nodes), size=per_day)
            dep = rng.uniform(420, 1260, per_day)
            vtype = rng.choice(vtypes, size=per_day, p=vprob)
            noise = rng.lognormal(0.0, tcfg["noise_sigma"], per_day)
            hav = haversine_km(lat[o], lon[o], lat[d], lon[d])
            for j in np.where((o != d) & (hav >= 0.3))[0]:
                road = hav[j] * (circ[zone[o[j]]] + circ[zone[d[j]]]) / 2.0
                speed = vt[vtype[j]]["speed_kmph"] * tcfg["vehicle_speed_factor"][vtype[j]]
                base = road / speed * 60.0 * float(traffic_factor(dep[j], bands)) * day.traffic_mult * c["congestion"]
                if day.rain:
                    base *= tcfg["rain_travel_factor"]
                n += 1
                rows.append({"leg_id": f"LEG-{cid}-{n:06d}", "trip_date": day.date, "city_id": cid,
                             "vehicle_type": vtype[j], "origin_id": nodes.at[int(o[j]), "node_id"],
                             "origin_zone": zone[o[j]], "dest_id": nodes.at[int(d[j]), "node_id"], "dest_zone": zone[d[j]],
                             "depart_min": round(float(dep[j]), 1), "haversine_km": round(float(hav[j]), 3),
                             "rain": bool(day.rain), "event_traffic": float(day.traffic_mult),
                             "actual_minutes": round(float(base * noise[j]), 2)})
    df = pd.DataFrame(rows)
    df["trip_date"] = pd.to_datetime(df["trip_date"])
    return df


def build_skus(cfg: dict[str, Any], rng: np.random.Generator) -> pd.DataFrame:
    """SKU master: family, unit weight and latent popularity."""
    w = cfg["warehouse"]
    n, fam = int(w["sku_count"]), int(w["families"])
    family = rng.integers(fam, size=n)
    fam_pop = rng.lognormal(0.0, 1.0, fam)
    popularity = fam_pop[family] * rng.pareto(1.6, n) + 0.01
    heavy = rng.random(n) < 0.14
    weight = np.where(heavy, rng.uniform(10.5, 28.0, n), np.clip(rng.lognormal(np.log(1.1), 0.7, n), 0.05, 9.8))
    return pd.DataFrame({"sku_id": [f"SKU-{i + 1:05d}" for i in range(n)], "family": family.astype(int),
                         "weight_kg": np.round(weight, 2), "popularity": popularity,
                         "category_code": [f"C{f:03d}" for f in family]})


def build_slots(cfg: dict[str, Any]) -> pd.DataFrame:
    """10,000 rack slots with coordinates, zone (A/B/C by travel rank) and level."""
    w = cfg["warehouse"]
    rows = []
    for aisle in range(1, w["aisles"] + 1):
        x = (aisle - 0.5) * w["aisle_pitch_m"]
        for side in ("L", "R"):
            for bay in range(1, w["bays_per_side"] + 1):
                y = w["front_offset_m"] + (bay - 0.5) * w["bay_width_m"]
                for level in range(1, w["levels"] + 1):
                    rows.append({"slot_id": f"A{aisle:02d}-{side}-B{bay:02d}-L{level}", "aisle": aisle, "side": side,
                                 "bay": bay, "level": level, "x_m": round(x, 2), "y_m": round(y, 2),
                                 "travel_m": round(x + y, 2)})
    df = pd.DataFrame(rows)
    loc = df.groupby(["aisle", "side", "bay"])["travel_m"].first().sort_values(kind="mergesort")
    zc = w["zone_slot_counts"]
    per_loc = w["levels"]
    a_n, b_n = zc["A"] // per_loc, zc["B"] // per_loc
    zone_map = {}
    for rank, key in enumerate(loc.index):
        zone_map[key] = "A" if rank < a_n else ("B" if rank < a_n + b_n else "C")
    df["zone"] = [zone_map[(a, s, b)] for a, s, b in zip(df["aisle"], df["side"], df["bay"])]
    df["heavy_ok"] = df["level"].isin(w["heavy_allowed_levels"])
    if len(df) != sum(zc.values()):
        raise DataValidationError(f"Slot count {len(df)} != zone total {sum(zc.values())}")
    return df


def build_pick_lines(cfg: dict[str, Any], skus: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Pick history per city DC with family co-occurrence and regional assortment preferences.

    Each pick order draws a product family by (regional) family popularity; 70%
    of its lines come from that family, 30% from the whole range. The last
    third of each city's orders is flagged ``is_holdout`` for out-of-sample
    layout evaluation.
    """
    w = cfg["warehouse"]
    fam = skus["family"].to_numpy()
    sku_ids = skus["sku_id"].to_numpy()
    fids = np.unique(fam)
    members = {f: np.where(fam == f)[0] for f in fids}
    frames = []
    for cid, c in cfg["cities"].items():
        regional = rng.lognormal(0.0, 0.35, int(fam.max()) + 1)          # regional taste by family
        pop = skus["popularity"].to_numpy() * regional[fam]
        p_all = np.cumsum(pop / pop.sum())
        fam_pop = pd.Series(pop).groupby(fam).sum()
        fam_cum = np.cumsum((fam_pop / fam_pop.sum()).to_numpy())
        mem_cum = {f: np.cumsum(pop[m] / pop[m].sum()) for f, m in members.items()}
        n_orders = int(round(int(w["pick_orders"]) * float(c["volume_weight"])))
        n_lines = 1 + rng.poisson(3.0, n_orders)
        fam_sel = fids[np.minimum(np.searchsorted(fam_cum, rng.random(n_orders)), len(fids) - 1)]
        holdout_from = int(0.67 * n_orders)
        oid, sid, hold = [], [], []
        for o in range(n_orders):
            f = fam_sel[o]
            m, cm = members[f], mem_cum[f]
            u, v, r = rng.random(n_lines[o]), rng.random(n_lines[o]), rng.random(n_lines[o])
            chosen = set()
            for i in range(n_lines[o]):
                if u[i] < 0.7:
                    chosen.add(int(m[min(np.searchsorted(cm, v[i]), len(m) - 1)]))
                else:
                    chosen.add(int(min(np.searchsorted(p_all, r[i]), len(pop) - 1)))
            for s_idx in sorted(chosen):
                oid.append(f"PO-{cid}-{o + 1:06d}")
                sid.append(sku_ids[s_idx])
                hold.append(o >= holdout_from)
        frames.append(pd.DataFrame({"pick_order_id": oid, "city_id": cid, "sku_id": sid,
                                    "qty": 1 + rng.poisson(0.6, len(oid)), "is_holdout": hold}))
    return pd.concat(frames, ignore_index=True)


def build_candidate_sites(cfg: dict[str, Any]) -> pd.DataFrame:
    """MFC candidate sites per city with rent, wage-adjusted opex and capacity."""
    net = cfg["network"]
    rows = []
    for cid, c in cfg["cities"].items():
        for k, (name, lat, lon, rent) in enumerate(c["candidate_sites"], start=1):
            annual_rent = rent * net["mfc_area_sqft"] * 12
            rows.append({"site_id": f"MFC-{cid}-{k:02d}", "city_id": cid, "city": c["name"], "name": name,
                         "lat": lat, "lon": lon, "rent_psf_month": rent, "area_sqft": net["mfc_area_sqft"],
                         "annual_rent_inr": annual_rent, "capex_inr": net["mfc_capex_inr"],
                         "annual_fixed_opex_inr": annual_rent + net["mfc_staff_cost_per_year"] * c["wage_index"]
                         + net["mfc_utilities_per_year"],
                         "capacity_orders_day": net["mfc_capacity_orders_per_day"]})
    return pd.DataFrame(rows)


def build_staffing(cfg: dict[str, Any]) -> pd.DataFrame:
    """Shift staffing inputs for the operations simulation."""
    s = cfg["simulation"]
    return pd.DataFrame([{"role": r, "baseline_heads": s["baseline_staffing"][r],
                          "cost_per_shift_inr": s["staffing_cost_per_shift"][r],
                          "min_heads": s["staffing_search_bounds"][r][0], "max_heads": s["staffing_search_bounds"][r][1]}
                         for r in s["baseline_staffing"]])


# =========================================================================== orchestration
def generate_all(cfg: dict[str, Any]) -> DataBundle:
    """Build every table with independent, seeded random streams."""
    seed = int(cfg["project"]["seed"])
    streams = np.random.SeedSequence(seed).spawn(8)
    rng = [np.random.default_rng(s) for s in streams]
    depots = build_depots(cfg)
    addresses = build_addresses(cfg, rng[0])
    calendar = build_calendar(cfg, rng[1])
    orders = build_orders(cfg, addresses, depots, calendar, rng[2])
    trip_logs = build_trip_logs(cfg, addresses, depots, calendar, rng[3])
    skus = build_skus(cfg, rng[4])
    bundle = DataBundle(
        depots=depots, vehicles=build_vehicles(cfg, depots), vehicle_types=build_vehicle_types(cfg), addresses=addresses,
        calendar=calendar, orders=orders, trip_logs=trip_logs, skus=skus, slots=build_slots(cfg),
        pick_lines=build_pick_lines(cfg, skus, rng[5]), candidate_sites=build_candidate_sites(cfg),
        staffing=build_staffing(cfg),
    )
    return bundle


def validate_bundle(bundle: DataBundle, cfg: dict[str, Any]) -> list[str]:
    """Run table contracts plus cross-table and business-rule checks."""
    issues: list[str] = []
    for name, contract in CONTRACTS.items():
        issues += contract.validate(getattr(bundle, name))
    o, a = bundle.orders, bundle.addresses
    issues += check_foreign_key(o, "address_id", a, "address_id", "orders->addresses")
    issues += check_foreign_key(o, "depot_id", bundle.depots, "depot_id", "orders->depots")
    issues += check_foreign_key(bundle.vehicles, "depot_id", bundle.depots, "depot_id", "vehicles->depots")
    issues += check_foreign_key(bundle.pick_lines, "sku_id", bundle.skus, "sku_id", "pick_lines->skus")
    if (o["tw_end_min"] <= o["tw_start_min"]).any():
        issues.append("orders: time window end <= start")
    max_cap = bundle.vehicles["capacity_kg"].max()
    if (o["weight_kg"] > max_cap).any():
        issues.append("orders: weight exceeds largest vehicle capacity")
    if len(bundle.slots) != sum(cfg["warehouse"]["zone_slot_counts"].values()):
        issues.append("slots: slot template size differs from zone totals")
    for cid, c in cfg["cities"].items():
        a_c = a[a["city_id"] == cid]
        b = c["bbox"]
        out = ~a_c["lat"].between(b["lat_min"], b["lat_max"]) | ~a_c["lon"].between(b["lon_min"], b["lon_max"])
        if out.any():
            issues.append(f"addresses: {int(out.sum())} outside {cid} bounding box")
        if len(bundle.vehicles[bundle.vehicles["city_id"] == cid]) != sum(v["count"] for v in cfg["fleet"]["vehicle_types"].values()):
            issues.append(f"vehicles: {cid} fleet size mismatch")
    cross = o.merge(bundle.depots[["depot_id", "city_id"]], on="depot_id", suffixes=("", "_dep"))
    if (cross["city_id"] != cross["city_id_dep"]).any():
        issues.append("orders: fulfilled from a depot in another city")
    heavy = int((bundle.skus["weight_kg"] > cfg["warehouse"]["heavy_threshold_kg"]).sum())
    if heavy > int(bundle.slots["heavy_ok"].sum()):
        issues.append("skus: more heavy SKUs than floor/lower-shelf slots")
    if len(bundle.skus) > len(bundle.slots):
        issues.append("skus: more SKUs than slots")
    if len(bundle.calendar) != int(cfg["calendar"]["days"]) * len(cfg["cities"]):
        issues.append("calendar: day count mismatch")
    return issues


def master_register(cfg: dict[str, Any]) -> pd.DataFrame:
    """Master data register: parameter, value, unit, rationale, dependent modules."""
    rows = []
    for item in cfg["register"]:
        try:
            value = get_by_path(cfg, item["key"])
        except ConfigError:
            if item["key"].startswith("cities."):      # city not loaded in this run
                continue
            raise
        if isinstance(value, list) and value and isinstance(value[0], dict):
            value = "; ".join(f"{b.get('name', '')} {b.get('start', '')}-{b.get('end', '')}: x{b.get('factor', '')}" for b in value)
        elif isinstance(value, (list, dict)):
            value = str(value)
        rows.append({"parameter": item["key"], "value": value, "unit": item["unit"],
                     "rationale": item["rationale"], "dependent_modules": ", ".join(item["modules"])})
    return pd.DataFrame(rows)


def data_profile(bundle: DataBundle) -> dict[str, Any]:
    """Headline profile of the dataset for logging and reports."""
    o = bundle.orders
    daily = o.groupby("order_date").size()
    return {"cities": int(o["city_id"].nunique()), "states": int(bundle.addresses["state"].nunique()),
            "orders": len(o), "days": int(daily.size), "avg_orders_per_day": round(float(daily.mean()), 1),
            "peak_day": str(daily.idxmax().date()), "peak_orders": int(daily.max()),
            "addresses": len(bundle.addresses), "vehicles": len(bundle.vehicles), "slots": len(bundle.slots),
            "skus": len(bundle.skus), "trip_legs": len(bundle.trip_logs), "candidate_sites": len(bundle.candidate_sites)}


def run(cfg: dict[str, Any]) -> DataBundle:
    """Generate, validate and persist the dataset plus the master register."""
    bundle = generate_all(cfg)
    issues = validate_bundle(bundle, cfg)
    if issues:
        raise DataValidationError("Dataset failed validation: " + " | ".join(issues))
    data_dir = resolve_path(cfg, "data_dir")
    bundle.save(data_dir)
    reg = master_register(cfg)
    reg.to_csv(resolve_path(cfg, "tables_dir") / "master_data_register.csv", index=False)
    log.info("Dataset written to %s | %s", data_dir, data_profile(bundle))
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run(load_config(args.config))


if __name__ == "__main__":
    main()
