"""Discrete-event simulation of the DC order flow with SimPy.

Flow per order: intake -> wave release -> pick -> pack -> QC (5% sample)
-> dispatch/loading -> delivery leg. Resources: pickers, packers, QC
inspectors, loaders (two shifts, 06:00-22:00).

Stochastic inputs
* Order count ~ Poisson(scenario mean); intake times, order lines and segment
  mix are resampled from the order history of matching days.
* Pick: per line Normal(45 s, 10 s) handling + walking time per line from the
  optimized slotting layout (warehouse module output).
* Pack: Uniform(3, 8) min per order + 0.2 min per extra line.
* QC: Normal(4, 1) min for a 5% random sample. Dispatch: Uniform(2, 6) min.
* Delivery leg: sampled from the optimized routing plan's depot-to-stop times.

Scenarios: baseline staffing; optimized staffing (cheapest roster whose pooled
P95 intake-to-dispatch time meets the 2-hour target, found by bottleneck-driven
search and verified at full replications); Diwali peak at baseline staffing; Diwali peak with peak roster.
Each scenario runs 200 Monte Carlo replications.

Run: ``python -m src.logistics.simulation_engine``
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import simpy

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import load_json, save_json, write_table
from src.shared.logging_utils import get_logger

from .common import DataBundle, load_bundle
from .vrp_optimizer import representative_day

log = get_logger(__name__)
ROLES = ("pickers", "packers", "qc", "loaders")
SHIFTS_PER_DAY = 2


@dataclass
class DemandProfile:
    """Empirical order profile used to sample replications."""

    name: str
    mean_orders: float
    intake: np.ndarray
    lines: np.ndarray
    segments: np.ndarray


@dataclass
class SimInputs:
    """Fixed process parameters for every replication."""

    travel_s_per_line: float
    delivery_minutes: np.ndarray
    cfg: dict[str, Any]


def build_profiles(bundle: DataBundle, cfg: dict[str, Any], city_id: str) -> dict[str, DemandProfile]:
    """Normal-day and Diwali-peak profiles from the city's order history."""
    o = bundle.orders[bundle.orders["city_id"] == city_id]
    cal = bundle.calendar[bundle.calendar["city_id"] == city_id]
    rep = representative_day(bundle)
    normal_days = cal[(~cal["is_event"]) & (cal["dow"] < 5)]["date"]
    normal = o[o["order_date"].isin(normal_days)]
    peak_days = cal[cal["event"].str.contains("Diwali peak")]["date"]
    peak = o[o["order_date"].isin(peak_days)]
    rep_n = float((o["order_date"] == rep).sum())
    pf = cfg["simulation"]["diwali_peak_factor"]
    peak_mean = float(peak.groupby("order_date").size().max()) if pf == "auto" else rep_n * float(pf)

    def prof(name: str, df: pd.DataFrame, mean: float) -> DemandProfile:
        return DemandProfile(name, mean, df["intake_min"].to_numpy(), df["lines"].to_numpy(), df["segment"].to_numpy())

    return {"normal": prof("normal", normal, rep_n), "diwali": prof("diwali", peak, peak_mean)}


def build_inputs(bundle: DataBundle, cfg: dict[str, Any], city_id: str) -> SimInputs:
    """Walking time per line from the city's slotting and delivery legs from its routes (if available)."""
    tdir = resolve_path(cfg, "tables_dir")
    wcfg = cfg["warehouse"]
    wpath = tdir / "warehouse_summary.json"
    if wpath.exists():
        travel_m = float(load_json(wpath)["by_city"][city_id]["optimized"]["travel_m_per_line"])
    else:
        log.warning("Warehouse results missing; using front-zone average walking distance")
        travel_m = float(bundle.slots.nsmallest(2000, "travel_m")["travel_m"].mean() * 2)
    spath = tdir / "route_stops.parquet"
    if spath.exists():
        st = pd.read_parquet(spath)
        rt = pd.read_parquet(tdir / "routes_summary.parquet")[["plan", "vehicle_id", "start_min"]]
        st = st[(st["plan"] == "optimized") & (st["city_id"] == city_id)].merge(
            rt[rt["plan"] == "optimized"], on=["plan", "vehicle_id"])
        delivery = (st["arrival_min"] - st["start_min"]).clip(lower=5).to_numpy()
    else:
        log.warning("Routing results missing; delivery legs not simulated")
        delivery = np.array([0.0])
    return SimInputs(travel_s_per_line=travel_m / float(wcfg["walk_speed_mps"]), delivery_minutes=delivery, cfg=cfg)


def simulate_day(profile: DemandProfile, staffing: dict[str, int], inputs: SimInputs, seed: int) -> dict[str, Any]:
    """One replication of one operating day. Returns order-level and resource-level results."""
    s = inputs.cfg["simulation"]
    rng = np.random.default_rng(seed)
    env = simpy.Environment(initial_time=float(s["shift_start_min"]))
    res = {r: simpy.Resource(env, capacity=max(1, int(staffing[r]))) for r in ROLES}
    busy = {r: 0.0 for r in ROLES}
    waits = {r: [] for r in ROLES}
    n = int(rng.poisson(profile.mean_orders))
    idx = rng.integers(len(profile.intake), size=n)
    intake = np.sort(profile.intake[idx])
    lines = profile.lines[idx]
    wave = float(s["wave_interval_min"])
    start = float(s["shift_start_min"])
    end = float(s["shift_end_min"])
    records: list[dict[str, float]] = []

    def use(role: str, duration: float):
        t_req = env.now
        with res[role].request() as req:
            yield req
            waits[role].append(env.now - t_req)
            yield env.timeout(duration)
            busy[role] += duration

    def order(i: int):
        t0 = float(intake[i])
        yield env.timeout(max(0.0, t0 - env.now))
        release = start + np.ceil((env.now - start) / wave) * wave
        yield env.timeout(release - env.now)
        per_line = np.clip(rng.normal(s["pick_line_seconds_mean"], s["pick_line_seconds_sd"], lines[i]), 15, None)
        pick = float((per_line.sum() + lines[i] * inputs.travel_s_per_line) / 60.0)
        yield from use("pickers", pick)
        pack = float(rng.uniform(*s["pack_minutes"]) + 0.2 * (lines[i] - 1))
        yield from use("packers", pack)
        if rng.random() < s["qc_rate"]:
            yield from use("qc", float(max(1.0, rng.normal(s["qc_minutes_mean"], s["qc_minutes_sd"]))))
        yield from use("loaders", float(rng.uniform(*s["dispatch_minutes"])))
        dispatched = env.now
        delivered = dispatched + float(rng.choice(inputs.delivery_minutes))
        records.append({"intake": t0, "dispatched": dispatched, "cycle": dispatched - t0, "delivered": delivered,
                        "lead_time": delivered - t0, "lines": float(lines[i])})

    for i in range(n):
        env.process(order(i))
    env.run(until=end)
    df = pd.DataFrame(records)
    done = len(df)
    target = float(s["cycle_time_target_min"])
    horizon = end - start
    util = {r: busy[r] / (horizon * res[r].capacity) for r in ROLES}
    return {
        "orders": n, "dispatched": done, "backlog": n - done,
        "throughput": done, "lines_dispatched": float(df["lines"].sum()) if done else 0.0,
        "cycle_p50": float(df["cycle"].quantile(0.5)) if done else np.nan,
        "cycle_p95": float(df["cycle"].quantile(0.95)) if done else np.nan,
        "lead_time_p50": float(df["lead_time"].quantile(0.5)) if done else np.nan,
        "sla_compliance": float((df["cycle"] <= target).sum() / n) if n else 1.0,
        **{f"util_{r}": util[r] for r in ROLES},
        **{f"wait_{r}": float(np.mean(waits[r])) if waits[r] else 0.0 for r in ROLES},
        "cycles": df["cycle"].to_numpy() if done else np.array([]),
    }


def monte_carlo(name: str, profile: DemandProfile, staffing: dict[str, int], inputs: SimInputs,
                reps: int, seed: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Run ``reps`` replications; return per-replication metrics and pooled cycle times."""
    rows, pooled = [], []
    for r in range(reps):
        out = simulate_day(profile, staffing, inputs, seed + r)
        pooled.append(out.pop("cycles"))
        rows.append({"scenario": name, "replication": r + 1, **out})
    return pd.DataFrame(rows), np.concatenate(pooled) if pooled else np.array([])


def staffing_cost(staffing: dict[str, int], cfg: dict[str, Any]) -> float:
    c = cfg["simulation"]["staffing_cost_per_shift"]
    return float(sum(staffing[r] * c[r] for r in ROLES) * SHIFTS_PER_DAY)


def meets_target(df: pd.DataFrame, pooled: np.ndarray, cfg: dict[str, Any]) -> bool:
    """Pooled P95 cycle within target and >= 99.5% of orders dispatched in the day."""
    target = float(cfg["simulation"]["cycle_time_target_min"])
    done_share = df["dispatched"].sum() / max(1, df["orders"].sum())
    return bool(pooled.size and np.quantile(pooled, 0.95) <= target and done_share >= 0.995)


def optimize_staffing(profile: DemandProfile, inputs: SimInputs, cfg: dict[str, Any], seed: int) -> tuple[dict[str, int], pd.DataFrame]:
    """Bottleneck-driven search for the cheapest roster meeting the cycle-time target.

    Start at lower bounds; add one head to the most utilised role until the
    target is met; then try removing one head from each role (most expensive
    first) while the target still holds.
    """
    s = cfg["simulation"]
    bounds = s["staffing_search_bounds"]
    reps = int(s["search_replications"])
    staff = {r: int(bounds[r][0]) for r in ROLES}
    log_rows = []
    for _ in range(80):
        df, pooled = monte_carlo("search", profile, staff, inputs, reps, seed)
        ok = meets_target(df, pooled, cfg)
        log_rows.append({**staff, "p95_cycle": float(np.quantile(pooled, 0.95)) if pooled.size else np.nan,
                         "meets_target": ok, "daily_cost": staffing_cost(staff, cfg), "phase": "grow"})
        if ok:
            break
        utils = {r: float(df[f"util_{r}"].mean()) for r in ROLES if staff[r] < bounds[r][1]}
        if not utils:
            break
        staff[max(utils, key=utils.get)] += 1
    cost_rank = sorted(ROLES, key=lambda r: -s["staffing_cost_per_shift"][r])
    improved = True
    while improved:
        improved = False
        for r in cost_rank:
            if staff[r] <= bounds[r][0]:
                continue
            trial = {**staff, r: staff[r] - 1}
            df, pooled = monte_carlo("search", profile, trial, inputs, reps, seed)
            ok = meets_target(df, pooled, cfg)
            log_rows.append({**trial, "p95_cycle": float(np.quantile(pooled, 0.95)) if pooled.size else np.nan,
                             "meets_target": ok, "daily_cost": staffing_cost(trial, cfg), "phase": "trim"})
            if ok:
                staff, improved = trial, True
    return staff, pd.DataFrame(log_rows)


def verify_staffing(profile: DemandProfile, staff: dict[str, int], inputs: SimInputs, cfg: dict[str, Any],
                    reps: int, seed: int) -> tuple[dict[str, int], int]:
    """Confirm a searched roster at full replication count; add heads at the bottleneck until it passes.

    The search uses fewer replications for speed, so its P95 estimate is noisier.
    Returns the verified roster and the number of heads added.
    """
    bounds = cfg["simulation"]["staffing_search_bounds"]
    added = 0
    staff = dict(staff)
    for _ in range(20):
        df, pooled = monte_carlo("verify", profile, staff, inputs, reps, seed)
        if meets_target(df, pooled, cfg):
            break
        utils = {r: float(df[f"util_{r}"].mean()) for r in ROLES if staff[r] < bounds[r][1]}
        if not utils:
            break
        staff[max(utils, key=utils.get)] += 1
        added += 1
    return staff, added


def summarize(name: str, df: pd.DataFrame, pooled: np.ndarray, staffing: dict[str, int], cfg: dict[str, Any]) -> dict[str, Any]:
    """P10/P50/P90 metrics and bottleneck identification for a scenario."""
    q = lambda col, p: float(np.nanquantile(df[col], p))  # noqa: E731
    utils = {r: float(df[f"util_{r}"].mean()) for r in ROLES}
    bottleneck = max(utils, key=utils.get)
    return {
        "scenario": name, **{f"staff_{r}": staffing[r] for r in ROLES},
        "daily_staff_cost": staffing_cost(staffing, cfg), "replications": len(df),
        "throughput_p10": q("throughput", 0.1), "throughput_p50": q("throughput", 0.5), "throughput_p90": q("throughput", 0.9),
        "cycle_p50_p10": q("cycle_p50", 0.1), "cycle_p50_p50": q("cycle_p50", 0.5), "cycle_p50_p90": q("cycle_p50", 0.9),
        "cycle_p95_p10": q("cycle_p95", 0.1), "cycle_p95_p50": q("cycle_p95", 0.5), "cycle_p95_p90": q("cycle_p95", 0.9),
        "pooled_cycle_p95": float(np.quantile(pooled, 0.95)) if pooled.size else np.nan,
        "lead_time_p50": q("lead_time_p50", 0.5),
        "sla_compliance_p50": q("sla_compliance", 0.5), "sla_compliance_p10": q("sla_compliance", 0.1),
        "backlog_p50": q("backlog", 0.5), "backlog_p90": q("backlog", 0.9),
        **{f"util_{r}": round(utils[r], 4) for r in ROLES},
        **{f"wait_{r}_min": round(float(df[f"wait_{r}"].mean()), 2) for r in ROLES},
        "bottleneck": bottleneck, "meets_p95_target": meets_target(df, pooled, cfg),
    }


def run_city(cfg: dict[str, Any], bundle: DataBundle, city_id: str, reps: int) -> dict[str, Any]:
    """All staffing scenarios for one city DC."""
    s = cfg["simulation"]
    seed = int(cfg["project"]["seed"]) + 100_000 * (list(cfg["cities"]).index(city_id) + 1)
    weight = float(cfg["cities"][city_id]["volume_weight"])
    profiles = build_profiles(bundle, cfg, city_id)
    inputs = build_inputs(bundle, cfg, city_id)
    base_staff = {r: max(1, int(round(int(s["baseline_staffing"][r]) * weight))) for r in ROLES}
    opt_staff, search_log = optimize_staffing(profiles["normal"], inputs, cfg, seed + 10_000)
    peak_staff, peak_log = optimize_staffing(profiles["diwali"], inputs, cfg, seed + 20_000)
    opt_staff, add_n = verify_staffing(profiles["normal"], opt_staff, inputs, cfg, reps, seed + 2000)
    peak_staff, add_p = verify_staffing(profiles["diwali"], peak_staff, inputs, cfg, reps, seed + 4000)
    if add_n or add_p:
        log.info("%s verification at %d replications added %d (normal) / %d (peak) heads", city_id, reps, add_n, add_p)
    scenarios = [("Baseline staffing", profiles["normal"], base_staff),
                 ("Optimized staffing", profiles["normal"], opt_staff),
                 ("Diwali peak - baseline staffing", profiles["diwali"], base_staff),
                 ("Diwali peak - peak roster", profiles["diwali"], peak_staff)]
    reps_all, summaries, pooled_rows = [], [], []
    wage = float(cfg["cities"][city_id]["wage_index"])
    for i, (name, prof, staff) in enumerate(scenarios):
        df, pooled = monte_carlo(name, prof, staff, inputs, reps, seed + 1000 * (i + 1))
        reps_all.append(df.assign(city_id=city_id))
        sm = summarize(name, df, pooled, staff, cfg)
        sm["daily_staff_cost"] = round(sm["daily_staff_cost"] * wage, 0)
        summaries.append({"city_id": city_id, **sm})
        hist, edges = np.histogram(np.clip(pooled, 0, 480), bins=48, range=(0, 480))
        pooled_rows += [{"city_id": city_id, "scenario": name, "bin_start_min": float(edges[k]), "orders": int(hist[k])}
                        for k in range(len(hist))]
        log.info("%s | %s | staff %s | P50 throughput %.0f | pooled P95 cycle %.1f min | bottleneck %s", city_id,
                 name, staff, sm["throughput_p50"], sm["pooled_cycle_p95"], sm["bottleneck"])
    meta = {"travel_s_per_line": round(inputs.travel_s_per_line, 2), "normal_mean_orders": profiles["normal"].mean_orders,
            "diwali_mean_orders": profiles["diwali"].mean_orders, "optimized_staffing": opt_staff,
            "peak_staffing": peak_staff, "baseline_staffing": base_staff}
    logs = pd.concat([search_log.assign(profile="normal"), peak_log.assign(profile="diwali")]).assign(city_id=city_id)
    return {"reps": pd.concat(reps_all), "summary": summaries, "hist": pooled_rows, "meta": meta, "search": logs}


def run(cfg: dict[str, Any], bundle: DataBundle | None = None, reps: int | None = None,
        cities: list[str] | None = None) -> dict[str, Any]:
    """Run all scenarios for every city DC and export replication-level and summary results."""
    bundle = bundle or load_bundle(cfg)
    reps = int(reps or cfg["simulation"]["replications"])
    results = {cid: run_city(cfg, bundle, cid, reps) for cid in (cities or list(cfg["cities"]))}
    tdir = resolve_path(cfg, "tables_dir")
    summary = pd.DataFrame([row for r in results.values() for row in r["summary"]])
    write_table(pd.concat([r["reps"] for r in results.values()], ignore_index=True),
                tdir / "simulation_replications.parquet", csv_copy=True)
    write_table(summary, tdir / "simulation_summary.parquet", csv_copy=True)
    write_table(pd.DataFrame([row for r in results.values() for row in r["hist"]]),
                tdir / "simulation_cycle_hist.parquet", csv_copy=True)
    write_table(pd.concat([r["search"] for r in results.values()], ignore_index=True),
                tdir / "simulation_staffing_search.parquet", csv_copy=True)
    meta = {"replications": reps, "target_min": cfg["simulation"]["cycle_time_target_min"],
            "by_city": {cid: r["meta"] for cid, r in results.items()}}
    save_json({"meta": meta, "scenarios": summary.to_dict(orient="records")}, tdir / "simulation_summary.json")
    return {"summary": summary, "meta": meta}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    parser.add_argument("--reps", type=int, default=None)
    args = parser.parse_args()
    run(load_config(args.config), reps=args.reps)


if __name__ == "__main__":
    main()
