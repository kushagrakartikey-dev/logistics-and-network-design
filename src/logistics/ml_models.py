"""Machine learning where it adds measurable value.

1. **Travel-time model** (telematics trip legs). Target: log ratio of observed
   minutes to the routing planning formula (straight-line km x network
   circuity / speed x traffic band). Gradient boosting learns zone road-network
   effects, rain and vehicle behaviour the planning formula ignores. Baseline =
   the planning formula itself. If the model beats the baseline on the
   time-ordered validation window, per-zone calibration factors are published
   for the routing and fleet modules.
2. **Zone demand forecast** (daily orders per zone). Features are known in
   advance (weekday, published sale/holiday calendar, days to Diwali, trend,
   zone), so the model forecasts the full horizon directly without lagged
   actuals - no leakage. Baseline = seasonal naive (last training week
   repeated). The 14-day forecast feeds fleet sizing only if the model beats
   the baseline.

Split: the last ``ml.validation_days`` (21) days are validation; models never
see them during fitting.

Run: ``python -m src.logistics.ml_models``
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import save_json, write_table
from src.shared.logging_utils import get_logger

from .common import DataBundle, load_bundle, traffic_factor
from .vrp_optimizer import representative_day

log = get_logger(__name__)


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """MAE, RMSE and MAPE (%), ignoring zero actuals for MAPE."""
    y_true, y_pred = np.asarray(y_true, float), np.asarray(y_pred, float)
    err = y_pred - y_true
    nz = y_true != 0
    return {"MAE": round(float(np.abs(err).mean()), 3), "RMSE": round(float(np.sqrt((err ** 2).mean())), 3),
            "MAPE": round(float(np.abs(err[nz] / y_true[nz]).mean() * 100), 2), "n": int(len(y_true))}


def time_split(dates: pd.Series, validation_days: int) -> tuple[np.ndarray, pd.Timestamp]:
    """Boolean mask of training rows and the first validation date."""
    cutoff = pd.Timestamp(dates.max()) - pd.Timedelta(days=validation_days - 1)
    return (dates < cutoff).to_numpy(), cutoff


# =========================================================================== travel time
def planning_minutes(df: pd.DataFrame, cfg: dict[str, Any]) -> np.ndarray:
    """The routing module's planning formula for each trip leg."""
    speed = df["vehicle_type"].map({k: v["speed_kmph"] for k, v in cfg["fleet"]["vehicle_types"].items()})
    road = df["haversine_km"] * float(cfg["geography"]["planning_circuity"])
    return (road / speed * 60.0 * traffic_factor(df["depart_min"], cfg["traffic"]["bands"])).to_numpy()


def travel_features(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    x = pd.DataFrame({
        "haversine_km": df["haversine_km"], "depart_hour": df["depart_min"] / 60.0,
        "traffic_band": traffic_factor(df["depart_min"], cfg["traffic"]["bands"]),
        "rain": df["rain"].astype(int), "event_traffic": df["event_traffic"], "dow": df["trip_date"].dt.dayofweek,
    })
    for col in ("city_id", "origin_zone", "dest_zone", "vehicle_type"):
        x = x.join(pd.get_dummies(df[col], prefix=col, dtype=int))
    return x


def train_travel_model(bundle: DataBundle, cfg: dict[str, Any]) -> dict[str, Any]:
    """Fit the travel-time ratio model and derive zone calibration factors."""
    df = bundle.trip_logs.copy()
    train, cutoff = time_split(df["trip_date"], int(cfg["ml"]["validation_days"]))
    plan = planning_minutes(df, cfg)
    x = travel_features(df, cfg)
    y = np.log(df["actual_minutes"].to_numpy() / plan)
    model = GradientBoostingRegressor(random_state=int(cfg["ml"]["random_state"]), **cfg["ml"]["gbr"])
    model.fit(x[train], y[train])
    pred = plan * np.exp(model.predict(x))
    val = ~train
    m_base = metrics(df.loc[val, "actual_minutes"], plan[val])
    m_ml = metrics(df.loc[val, "actual_minutes"], pred[val])
    beats = m_ml["MAE"] < m_base["MAE"]
    # Zone factors for routing: dry, non-event conditions, fleet-mix weighted
    std = df[val].copy()
    std["rain"] = False
    std["event_traffic"] = 1.0
    ratio = np.exp(model.predict(travel_features(std, cfg).reindex(columns=x.columns, fill_value=0)))
    std["ratio"] = ratio
    zone_factors = std.groupby("dest_zone")["ratio"].median().round(4).to_dict()
    importance = pd.Series(model.feature_importances_, index=x.columns).sort_values(ascending=False)
    val_df = df.loc[val, ["leg_id", "trip_date", "city_id", "dest_zone", "vehicle_type", "haversine_km", "depart_min",
                          "rain", "actual_minutes"]].copy()
    val_df["planning_minutes"] = np.round(plan[val], 2)
    val_df["ml_minutes"] = np.round(pred[val], 2)
    return {"baseline": m_base, "model": m_ml, "beats_baseline": beats, "zone_factors": zone_factors,
            "importance": importance, "validation": val_df, "cutoff": str(cutoff.date()),
            "train_rows": int(train.sum()), "validation_rows": int(val.sum())}


# =========================================================================== demand
def demand_frame(bundle: DataBundle, cfg: dict[str, Any], future_days: int = 0) -> pd.DataFrame:
    """City-zone x day order counts with the city's calendar features (optionally extended)."""
    cal = bundle.calendar[["city_id", "date", "dow", "event", "days_to_diwali"]].copy()
    if future_days:
        fut = pd.date_range(cal["date"].max() + pd.Timedelta(days=1), periods=future_days, freq="D")
        diwali = pd.Timestamp(cfg["calendar"]["diwali_date"])
        ext = [pd.DataFrame({"city_id": cid, "date": fut, "dow": fut.dayofweek, "event": "",
                             "days_to_diwali": (diwali - fut).days}) for cid in cfg["cities"]]
        cal = pd.concat([cal, *ext], ignore_index=True)
    zones = bundle.addresses[["city_id", "zone"]].drop_duplicates()
    grid = cal.merge(zones, on="city_id")
    counts = bundle.orders.groupby(["order_date", "zone"]).size().rename("orders").reset_index()
    grid = grid.merge(counts, left_on=["date", "zone"], right_on=["order_date", "zone"], how="left").drop(columns="order_date")
    hist_end = bundle.calendar["date"].max()
    grid.loc[grid["date"] <= hist_end, "orders"] = grid.loc[grid["date"] <= hist_end, "orders"].fillna(0)
    return grid


def demand_features(g: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    x = pd.DataFrame({"t": (g["date"] - start).dt.days, "dow": g["dow"],
                      "weekend": (g["dow"] >= 5).astype(int),
                      "sale": g["event"].str.contains("sale").astype(int),
                      "diwali_peak": g["event"].str.contains("Diwali peak").astype(int),
                      "holiday": g["event"].str.contains("holiday|Dussehra|Visarjan|Puja|Chhath|Rajyotsava",
                                                         case=False).astype(int),
                      "navratri": g["event"].str.contains("Navratri").astype(int),
                      "days_to_diwali": g["days_to_diwali"].clip(-30, 60)})
    return x.join(pd.get_dummies(g["zone"], prefix="zone", dtype=int)).join(
        pd.get_dummies(g["city_id"], prefix="city", dtype=int))


def train_demand_model(bundle: DataBundle, cfg: dict[str, Any], horizon: int = 14) -> dict[str, Any]:
    """Direct multi-horizon zone demand model vs seasonal naive."""
    vd = int(cfg["ml"]["validation_days"])
    g = demand_frame(bundle, cfg, future_days=horizon)
    start = g["date"].min()
    hist = g[g["orders"].notna()].reset_index(drop=True)
    train, cutoff = time_split(hist["date"], vd)
    x = demand_features(hist, start)
    model = GradientBoostingRegressor(random_state=int(cfg["ml"]["random_state"]), **cfg["ml"]["gbr"])
    model.fit(x[train], np.log1p(hist.loc[train, "orders"]))
    pred = np.expm1(model.predict(x))
    # Seasonal naive: last training week repeated across the validation window
    last_week = hist[train & (hist["date"] >= cutoff - pd.Timedelta(days=7))]
    naive_map = last_week.set_index(["zone", "dow"])["orders"]   # zone names are unique across cities
    naive = np.array([naive_map.get((z, d), np.nan) for z, d in zip(hist["zone"], hist["dow"])])
    val = ~train
    m_ml = metrics(hist.loc[val, "orders"], pred[val])
    m_naive = metrics(hist.loc[val, "orders"], naive[val])
    beats = m_ml["MAE"] < m_naive["MAE"]
    # Refit on all history, forecast the next `horizon` days
    model_full = GradientBoostingRegressor(random_state=int(cfg["ml"]["random_state"]), **cfg["ml"]["gbr"])
    model_full.fit(x, np.log1p(hist["orders"]))
    fut = g[g["orders"].isna()].reset_index(drop=True)
    fut["forecast"] = np.expm1(model_full.predict(demand_features(fut, start).reindex(columns=x.columns, fill_value=0)))
    daily_fc = fut.groupby("date")["forecast"].sum()
    rep_volume = float((bundle.orders["order_date"] == representative_day(bundle)).sum())
    val_df = hist.loc[val, ["city_id", "date", "zone", "orders"]].copy()
    val_df["ml_forecast"] = np.round(pred[val], 2)
    val_df["seasonal_naive"] = naive[val]
    city_fc = fut.groupby(["city_id", "date"])["forecast"].sum()
    rep_day = representative_day(bundle)
    rep_city = bundle.orders[bundle.orders["order_date"] == rep_day].groupby("city_id").size()
    city_peak = (city_fc.groupby("city_id").max() / rep_city).round(3).to_dict()
    return {"baseline": m_naive, "model": m_ml, "beats_baseline": beats, "cutoff": str(cutoff.date()),
            "validation": val_df, "forecast": fut[["city_id", "date", "zone", "forecast"]], "city_peak": city_peak,
            "next_peak_multiplier": float(daily_fc.max() / rep_volume), "daily_forecast": daily_fc,
            "rep_volume": rep_volume}


def run(cfg: dict[str, Any], bundle: DataBundle | None = None) -> dict[str, Any]:
    """Train, validate and publish both models' outputs."""
    bundle = bundle or load_bundle(cfg)
    tdir = resolve_path(cfg, "tables_dir")
    tt = train_travel_model(bundle, cfg)
    dm = train_demand_model(bundle, cfg)
    log.info("Travel time | planning formula %s | ML %s | beats=%s", tt["baseline"], tt["model"], tt["beats_baseline"])
    log.info("Zone demand | seasonal naive %s | ML %s | beats=%s", dm["baseline"], dm["model"], dm["beats_baseline"])
    if tt["beats_baseline"]:
        save_json({"zone_factors": tt["zone_factors"], "validated_mae_min": tt["model"]["MAE"],
                   "baseline_mae_min": tt["baseline"]["MAE"], "cutoff": tt["cutoff"]}, tdir / "ml_travel_calibration.json")
    else:
        (tdir / "ml_travel_calibration.json").unlink(missing_ok=True)
        log.warning("Travel model did not beat the planning formula; routing keeps uncalibrated travel times")
    save_json({"model_beats_baseline": dm["beats_baseline"], "next_14d_peak_multiplier": dm["next_peak_multiplier"],
               "horizon": [str(dm["daily_forecast"].index.min().date()), str(dm["daily_forecast"].index.max().date())],
               "daily_forecast": {str(k.date()): round(v, 1) for k, v in dm["daily_forecast"].items()},
               "representative_day_volume": dm["rep_volume"], "city_peak_multiplier": dm["city_peak"]},
              tdir / "ml_demand_forecast.json")
    perf = pd.DataFrame([
        {"model": "Travel time", "approach": "Planning formula (baseline)", **tt["baseline"], "unit": "minutes"},
        {"model": "Travel time", "approach": "Gradient boosting (log ratio)", **tt["model"], "unit": "minutes"},
        {"model": "Zone daily demand", "approach": "Seasonal naive (baseline)", **dm["baseline"], "unit": "orders"},
        {"model": "Zone daily demand", "approach": "Gradient boosting (calendar)", **dm["model"], "unit": "orders"},
    ])
    perf["validation_start"] = [tt["cutoff"], tt["cutoff"], dm["cutoff"], dm["cutoff"]]
    write_table(perf, tdir / "ml_performance.parquet", csv_copy=True)
    write_table(tt["validation"], tdir / "ml_travel_validation.parquet")
    write_table(dm["validation"], tdir / "ml_demand_validation.parquet", csv_copy=True)
    write_table(dm["forecast"], tdir / "ml_demand_forecast.parquet", csv_copy=True)
    write_table(tt["importance"].rename("importance").reset_index().rename(columns={"index": "feature"}),
                tdir / "ml_feature_importance.parquet", csv_copy=True)
    write_table(pd.DataFrame([{"dest_zone": k, "factor": v} for k, v in tt["zone_factors"].items()]),
                tdir / "ml_zone_factors.parquet", csv_copy=True)
    return {"travel": tt, "demand": dm, "performance": perf}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()
