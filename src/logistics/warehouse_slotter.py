"""Velocity- and affinity-based slotting of 10,000 storage locations.

Each city DC uses the same 10,000-slot template with its own (regional) pick
history. Layout: 50 aisles x 2 sides x 20 bays x 5 levels, dispatch at the front-left
corner. Zones A/B/C are the nearest 2,000 / next 4,000 / last 4,000 slots by
rectilinear travel distance. Levels 1-2 are floor / approved lower shelves.

Method
------
1. Pick velocity per SKU is learned from the *training* part of pick history
   (first two-thirds of pick orders) with empirical-Bayes shrinkage toward the
   product-family rate; the shrinkage weight is chosen by internal validation
   inside the training window. Performance is measured on the held-out last
   third, so the reported improvement is out-of-sample.
2. Co-pick affinity: a NetworkX graph of SKU pairs picked in the same order is
   clustered (Louvain) into affinity clusters.
3. Velocity classes fill zones A -> B -> C in pick-frequency order. Inside a
   zone, clusters are placed contiguously along the aisle walking sequence so
   co-picked SKUs sit next to each other; heavier-than-10-kg SKUs may only take
   level 1-2 slots (hard constraint), and light fast movers prefer waist-height
   level 2-3.
4. Baseline = current practice: SKUs slotted by category code in receiving
   order, ignoring velocity and weight.

Picker travel per batch uses the return routing policy (enter each required
aisle to the deepest bay and back) - a standard, conservative policy.

Run: ``python -m src.logistics.warehouse_slotter``
"""
from __future__ import annotations

import argparse
from itertools import combinations
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import save_json, write_table
from src.shared.logging_utils import get_logger

from .common import DataBundle, load_bundle

log = get_logger(__name__)


# =========================================================================== analysis inputs
def pick_frequency(pick_lines: pd.DataFrame, skus: pd.DataFrame) -> pd.Series:
    """Training-period picks per SKU (0 for SKUs never picked)."""
    train = pick_lines[~pick_lines["is_holdout"]]
    return train.groupby("sku_id").size().reindex(skus["sku_id"], fill_value=0).astype(float)


def estimate_velocity(pick_lines: pd.DataFrame, skus: pd.DataFrame, k: float) -> pd.Series:
    """Empirical-Bayes velocity: shrink each SKU's picks toward its family mean by weight ``k``."""
    raw = pick_frequency(pick_lines, skus)
    fam = skus.set_index("sku_id")["family"]
    mu = raw.groupby(fam).transform("mean")
    return (raw + k * mu) / (1.0 + k)


def select_shrinkage(pick_lines: pd.DataFrame, skus: pd.DataFrame, slots: pd.DataFrame, clusters: dict[str, int],
                     cfg: dict[str, Any]) -> tuple[float, pd.DataFrame]:
    """Pick the shrinkage weight by internal validation inside the training history.

    Training orders are split 75/25 in time; each candidate ``k`` builds a layout
    from the first part and is scored on travel for the second. The holdout set
    used for reported results is never touched.
    """
    train = pick_lines[~pick_lines["is_holdout"]].copy()
    ids = train["pick_order_id"].drop_duplicates().tolist()
    cut = set(ids[int(0.75 * len(ids)):])
    train["is_holdout"] = train["pick_order_id"].isin(cut)
    rows = []
    for k in cfg["warehouse"].get("velocity_shrinkage_grid", [0.0]):
        lay = optimized_layout(skus, slots, estimate_velocity(train, skus, float(k)), clusters, cfg)
        m = evaluate_layout("cv", lay, train, skus, slots, cfg)
        rows.append({"shrinkage_k": float(k), "validation_travel_m": m["travel_m"], "validation_pph": m["picks_per_hour"]})
    grid = pd.DataFrame(rows)
    best = float(grid.loc[grid["validation_travel_m"].idxmin(), "shrinkage_k"])
    return best, grid


def affinity_clusters(pick_lines: pd.DataFrame, seed: int, min_weight: int = 2) -> dict[str, int]:
    """Louvain clusters on the co-pick graph (edges = pairs picked together >= min_weight times)."""
    train = pick_lines[~pick_lines["is_holdout"]]
    counts: dict[tuple[str, str], int] = {}
    for _, grp in train.groupby("pick_order_id")["sku_id"]:
        items = sorted(set(grp))
        for a, b in combinations(items, 2):
            counts[(a, b)] = counts.get((a, b), 0) + 1
    g = nx.Graph()
    g.add_weighted_edges_from((a, b, w) for (a, b), w in counts.items() if w >= min_weight)
    if g.number_of_edges() == 0:
        return {}
    comms = nx.community.louvain_communities(g, weight="weight", seed=seed)
    return {sku: i for i, comm in enumerate(comms) for sku in comm}


def walking_sequence(slots: pd.DataFrame) -> pd.DataFrame:
    """Slots ordered along a serpentine walk: aisle, then bay (snaking), then level."""
    s = slots.copy()
    s["bay_seq"] = np.where(s["aisle"] % 2 == 1, s["bay"], -s["bay"])
    return s.sort_values(["aisle", "bay_seq", "side", "level"]).drop(columns="bay_seq")


# =========================================================================== layouts
def baseline_layout(skus: pd.DataFrame, slots: pd.DataFrame) -> pd.DataFrame:
    """Current practice: category code then SKU code, filled in walking sequence."""
    order = skus.sort_values(["category_code", "sku_id"])["sku_id"].to_numpy()
    seq = walking_sequence(slots)["slot_id"].to_numpy()[: len(order)]
    return pd.DataFrame({"sku_id": order, "slot_id": seq})


def optimized_layout(skus: pd.DataFrame, slots: pd.DataFrame, freq: pd.Series, clusters: dict[str, int],
                     cfg: dict[str, Any]) -> pd.DataFrame:
    """Velocity zoning + affinity contiguity + ergonomic weight constraint."""
    w = cfg["warehouse"]
    heavy_kg = float(w["heavy_threshold_kg"])
    sk = skus.set_index("sku_id").copy()
    sk["freq"] = freq
    sk["cluster"] = [clusters.get(s, -1 - i) for i, s in enumerate(sk.index)]
    sk["heavy"] = sk["weight_kg"] > heavy_kg
    # Velocity ranking assigns each SKU a zone by slot capacity
    ranked = sk.sort_values(["freq", "weight_kg"], ascending=[False, True])
    zone_cap = {z: int((slots["zone"] == z).sum()) for z in ("A", "B", "C")}
    zone_of: dict[str, str] = {}
    pos = 0
    for z in ("A", "B", "C"):
        take = ranked.index[pos: pos + zone_cap[z]]
        for s in take:
            zone_of[s] = z
        pos += len(take)
    sk["zone"] = pd.Series(zone_of)
    assignment: dict[str, str] = {}
    leftovers: list[str] = []
    for z in ("A", "B", "C"):
        zs = walking_sequence(slots[slots["zone"] == z])
        heavy_slots = list(zs.loc[zs["heavy_ok"], "slot_id"])
        # light SKUs prefer waist height (level 2-3), then 1, then 4-5
        pref = {2: 0, 3: 1, 1: 2, 4: 3, 5: 4}
        zs_light = zs.assign(pref=zs["level"].map(pref))
        members = sk[sk["zone"] == z]
        # clusters ordered by their total velocity; inside a cluster by velocity
        cl_rank = members.groupby("cluster")["freq"].sum().sort_values(ascending=False)
        members = members.assign(cl_rank=members["cluster"].map({c: i for i, c in enumerate(cl_rank.index)}))
        members = members.sort_values(["cl_rank", "freq"], ascending=[True, False])
        heavy = members[members["heavy"]].index.tolist()
        used: set[str] = set()
        for s, slot in zip(heavy, heavy_slots):
            assignment[s] = slot
            used.add(slot)
        if len(heavy) > len(heavy_slots):
            leftovers += heavy[len(heavy_slots):]
        light = members[~members["heavy"]].index.tolist()
        # contiguous cluster blocks: walk locations in sequence, fill by preferred level within each location
        free = zs_light[~zs_light["slot_id"].isin(used)]
        reach = float(w.get("reach_penalty_m", 0.0))
        if reach > 0:
            key = free["travel_m"] + reach * (free["level"] >= 4)
            ordered_slots = free.assign(key=key).sort_values(["key", "pref"], kind="mergesort")["slot_id"].tolist()
        else:
            ordered_slots = []
            for _, grp in free.groupby(["aisle", "side", "bay"], sort=False):
                ordered_slots += grp.sort_values("pref")["slot_id"].tolist()
        for s, slot in zip(light, ordered_slots):
            assignment[s] = slot
        if len(light) > len(ordered_slots):
            leftovers += light[len(ordered_slots):]
    if leftovers:   # overflow into any remaining compatible slot, nearest first
        free = slots[~slots["slot_id"].isin(set(assignment.values()))].sort_values("travel_m")
        for s in leftovers:
            ok = free["heavy_ok"] if sk.at[s, "heavy"] else pd.Series(True, index=free.index)
            slot = free[ok].iloc[0]["slot_id"]
            assignment[s] = slot
            free = free[free["slot_id"] != slot]
    return pd.DataFrame({"sku_id": list(assignment.keys()), "slot_id": list(assignment.values())})


# =========================================================================== evaluation
def evaluate_layout(name: str, layout: pd.DataFrame, pick_lines: pd.DataFrame, skus: pd.DataFrame,
                    slots: pd.DataFrame, cfg: dict[str, Any], holdout: bool = True) -> dict[str, Any]:
    """Batch picking simulation with return routing on the held-out pick orders."""
    w = cfg["warehouse"]
    heavy_kg = float(w["heavy_threshold_kg"])
    lines = pick_lines[pick_lines["is_holdout"] == holdout] if holdout is not None else pick_lines
    loc = layout.merge(slots, on="slot_id").merge(skus[["sku_id", "weight_kg"]], on="sku_id").set_index("sku_id")
    lines = lines.join(loc[["aisle", "x_m", "y_m", "level", "weight_kg"]], on="sku_id")
    order_ids = lines["pick_order_id"].drop_duplicates().tolist()
    batch_size = int(w["batch_size"])
    batch_of = {oid: i // batch_size for i, oid in enumerate(order_ids)}
    lines["batch"] = lines["pick_order_id"].map(batch_of)
    front = float(w["front_offset_m"])
    dist = 0.0
    for _, b in lines.groupby("batch"):
        per_aisle = b.groupby("aisle")["y_m"].max()
        max_x = float(b["x_m"].max())
        dist += 2.0 * max_x + float((2.0 * (per_aisle - front + w["bay_width_m"] / 2.0)).sum()) + 2.0 * front
    n_lines = len(lines)
    n_batches = int(lines["batch"].nunique())
    upper = lines["level"] >= 4
    heavy_bad = (lines["weight_kg"] > heavy_kg) & ~lines["level"].isin(w["heavy_allowed_levels"])
    handling_s = (n_lines * w["pick_seconds_base"] + float(upper.sum()) * w["pick_seconds_upper_levels"]
                  + float(heavy_bad.sum()) * w["pick_seconds_heavy_upper"])
    walk_s = dist / float(w["walk_speed_mps"])
    total_s = walk_s + handling_s + n_batches * w["tour_setup_seconds"]
    pph = n_lines / (total_s / 3600.0)
    sku_heavy = loc["weight_kg"] > heavy_kg
    violations = int((sku_heavy & ~loc["level"].isin(w["heavy_allowed_levels"])).sum())
    return {"layout": name, "pick_lines": n_lines, "batches": n_batches, "travel_m": round(dist, 1),
            "travel_m_per_line": round(dist / n_lines, 2), "walk_hours": round(walk_s / 3600, 2),
            "handling_hours": round(handling_s / 3600, 2), "total_hours": round(total_s / 3600, 2),
            "picks_per_hour": round(pph, 1),
            "labour_cost_per_1000_picks": round(1000.0 / pph * float(w["picker_wage_per_hour"]), 1),
            "heavy_slot_violations": violations, "upper_level_pick_share": round(float(upper.mean()), 4)}


def zone_heatmap(layout: pd.DataFrame, freq: pd.Series, slots: pd.DataFrame) -> pd.DataFrame:
    """Pick intensity per (aisle, bay) location, both sides and levels combined."""
    df = layout.merge(slots[["slot_id", "aisle", "bay"]], on="slot_id")
    df["picks"] = df["sku_id"].map(freq).fillna(0.0)
    return df.groupby(["aisle", "bay"])["picks"].sum().reset_index()


def run_city(cfg: dict[str, Any], bundle: DataBundle, city_id: str) -> dict[str, Any]:
    """Slot one city DC (shared 10,000-slot template, city-specific pick history)."""
    seed = int(cfg["project"]["seed"])
    picks = bundle.pick_lines[bundle.pick_lines["city_id"] == city_id]
    clusters = affinity_clusters(picks, seed)
    k_best, cv_grid = select_shrinkage(picks, bundle.skus, bundle.slots, clusters, cfg)
    freq = estimate_velocity(picks, bundle.skus, k_best)
    base = baseline_layout(bundle.skus, bundle.slots)
    opt = optimized_layout(bundle.skus, bundle.slots, freq, clusters, cfg)
    if opt["slot_id"].duplicated().any() or len(opt) != len(bundle.skus):
        raise RuntimeError(f"{city_id}: optimized layout is not a one-to-one SKU-slot assignment")
    mb = evaluate_layout("baseline", base, picks, bundle.skus, bundle.slots, cfg)
    mo = evaluate_layout("optimized", opt, picks, bundle.skus, bundle.slots, cfg)
    # Perfect-information bound: same method fed the SKU master's long-run demand index
    bound = optimized_layout(bundle.skus, bundle.slots, bundle.skus.set_index("sku_id")["popularity"], clusters, cfg)
    mbound = evaluate_layout("perfect_information", bound, picks, bundle.skus, bundle.slots, cfg)
    pct = lambda new, old: round((old - new) / old * 100, 2)  # noqa: E731
    comp = {"travel_reduction_pct": pct(mo["travel_m"], mb["travel_m"]),
            "throughput_gain_pct": round((mo["picks_per_hour"] / mb["picks_per_hour"] - 1) * 100, 2),
            "labour_cost_reduction_pct": pct(mo["labour_cost_per_1000_picks"], mb["labour_cost_per_1000_picks"]),
            "perfect_info_travel_reduction_pct": pct(mbound["travel_m"], mb["travel_m"]),
            "perfect_info_throughput_gain_pct": round((mbound["picks_per_hour"] / mb["picks_per_hour"] - 1) * 100, 2),
            "clusters": int(len(set(clusters.values()))), "clustered_skus": len(clusters),
            "velocity_shrinkage_k": k_best, "evaluation": "held-out pick orders (last third of history)"}
    wage = float(cfg["cities"][city_id]["wage_index"])
    for m in (mb, mo, mbound):
        m["labour_cost_per_1000_picks"] = round(m["labour_cost_per_1000_picks"] * wage, 1)
    assign = (opt.rename(columns={"slot_id": "optimized_slot"})
              .merge(base.rename(columns={"slot_id": "baseline_slot"}), on="sku_id")
              .merge(bundle.skus[["sku_id", "family", "weight_kg"]], on="sku_id"))
    assign["train_picks"] = assign["sku_id"].map(freq)
    assign["cluster"] = assign["sku_id"].map(clusters).fillna(-1).astype(int)
    assign = assign.merge(bundle.slots[["slot_id", "zone", "level"]].rename(
        columns={"slot_id": "optimized_slot", "zone": "opt_zone", "level": "opt_level"}), on="optimized_slot")
    hb = zone_heatmap(base, freq, bundle.slots).rename(columns={"picks": "baseline_picks"})
    ho = zone_heatmap(opt, freq, bundle.slots).rename(columns={"picks": "optimized_picks"})
    zone_tbl = assign.groupby("opt_zone").agg(skus=("sku_id", "size"), picks=("train_picks", "sum")).reset_index()
    zone_tbl["pick_share"] = (zone_tbl["picks"] / zone_tbl["picks"].sum()).round(4)
    tag = lambda df: df.assign(city_id=city_id)  # noqa: E731
    log.info("%s slotting | travel -%.1f%% | throughput +%.1f%% | bound -%.1f%%", city_id,
             comp["travel_reduction_pct"], comp["throughput_gain_pct"], comp["perfect_info_travel_reduction_pct"])
    return {"baseline": mb, "optimized": mo, "perfect_information": mbound, "comparison": comp,
            "assign": tag(assign), "heatmap": tag(hb.merge(ho, on=["aisle", "bay"])), "zones": tag(zone_tbl),
            "perf": tag(pd.DataFrame([mb, mo, mbound])), "cv": tag(cv_grid)}


def run(cfg: dict[str, Any], bundle: DataBundle | None = None, cities: list[str] | None = None) -> dict[str, Any]:
    """Slot every city DC, evaluate out-of-sample and export results."""
    bundle = bundle or load_bundle(cfg)
    results = {cid: run_city(cfg, bundle, cid) for cid in (cities or list(cfg["cities"]))}
    tdir = resolve_path(cfg, "tables_dir")
    cat = lambda key: pd.concat([r[key] for r in results.values()], ignore_index=True)  # noqa: E731
    write_table(cat("assign"), tdir / "slot_assignments.parquet", csv_copy=True)
    write_table(cat("heatmap"), tdir / "warehouse_heatmap.parquet", csv_copy=True)
    write_table(cat("zones"), tdir / "warehouse_zone_summary.parquet", csv_copy=True)
    write_table(cat("perf"), tdir / "warehouse_performance.parquet", csv_copy=True)
    write_table(cat("cv"), tdir / "warehouse_shrinkage_cv.parquet", csv_copy=True)
    perf = cat("perf")
    tot = perf.groupby("layout")[["pick_lines", "travel_m", "total_hours"]].sum()
    national = {
        "travel_reduction_pct": round((1 - tot.at["optimized", "travel_m"] / tot.at["baseline", "travel_m"]) * 100, 2),
        "throughput_gain_pct": round(((tot.at["optimized", "pick_lines"] / tot.at["optimized", "total_hours"])
                                      / (tot.at["baseline", "pick_lines"] / tot.at["baseline", "total_hours"]) - 1) * 100, 2),
        "perfect_info_travel_reduction_pct": round((1 - tot.at["perfect_information", "travel_m"]
                                                    / tot.at["baseline", "travel_m"]) * 100, 2)}
    out = {"national": national,
           "by_city": {cid: {k: r[k] for k in ("baseline", "optimized", "perfect_information", "comparison")}
                       for cid, r in results.items()}}
    save_json(out, tdir / "warehouse_summary.json")
    log.info("National slotting | %s", national)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()
