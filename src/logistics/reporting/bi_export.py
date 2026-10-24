"""Export an import-ready star schema for Power BI and Tableau.

Writes CSV (and Parquet) fact and dimension tables to ``outputs/logistics/bi``
plus ``bi_manifest.json`` (table, grain, keys, row count, columns). The model,
relationships, DAX measures and Tableau calculations are specified in
``docs/bi/``. Native .pbix / .twb files are not generated here: those formats
are authored in Power BI Desktop / Tableau Desktop from these tables.

Run: ``python -m src.logistics.reporting.bi_export``
"""
from __future__ import annotations

import argparse
from typing import Any

import pandas as pd

from src.shared.config import load_config, resolve_path
from src.shared.io_utils import save_json
from src.shared.logging_utils import get_logger

from ..common import load_bundle

log = get_logger(__name__)


def _date_key(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y%m%d").astype(int)


def build_tables(cfg: dict[str, Any]) -> dict[str, tuple[pd.DataFrame, str, str]]:
    """Return {table: (frame, grain, keys)} for the star schema."""
    b = load_bundle(cfg)
    t = resolve_path(cfg, "tables_dir")
    rp = lambda n: pd.read_parquet(t / n)  # noqa: E731
    tables: dict[str, tuple[pd.DataFrame, str, str]] = {}
    # ---------------- dimensions
    city = pd.DataFrame([{"city_id": k, "city": c["name"], "states": c["state"], "volume_weight": c["volume_weight"],
                          "congestion_index": c["congestion"], "wage_index": c["wage_index"]} for k, c in cfg["cities"].items()])
    tables["dim_city"] = (city, "one row per metro cluster", "city_id")
    dates = pd.DataFrame({"date": pd.to_datetime(sorted(b.calendar["date"].unique()))})
    dates["date_key"] = _date_key(dates["date"])
    dates["day_name"] = dates["date"].dt.day_name()
    dates["iso_week"] = dates["date"].dt.isocalendar().week.astype(int)
    dates["month"] = dates["date"].dt.strftime("%Y-%m")
    dates["is_weekend"] = dates["date"].dt.dayofweek >= 5
    dates["days_to_diwali"] = (pd.Timestamp(cfg["calendar"]["diwali_date"]) - dates["date"]).dt.days
    tables["dim_date"] = (dates, "one row per calendar day", "date_key")
    zf = rp("ml_zone_factors.parquet").set_index("dest_zone")["factor"]
    zones = b.addresses.groupby(["zone"]).agg(city_id=("city_id", "first"), state=("state", "first")).reset_index()
    zones["observed_circuity"] = [cfg["cities"][c]["zones"][z]["circuity"] for c, z in zip(zones["city_id"], zones["zone"])]
    zones["ml_travel_factor"] = zones["zone"].map(zf)
    tables["dim_zone"] = (zones, "one row per delivery zone (names unique across cities)", "zone")
    tables["dim_depot"] = (b.depots[["depot_id", "city_id", "role", "name", "zone", "lat", "lon", "open_min", "close_min"]],
                           "one row per depot", "depot_id")
    tables["dim_vehicle"] = (b.vehicles, "one row per vehicle in the current fleet", "vehicle_id")
    tables["dim_vehicle_type"] = (b.vehicle_types, "one row per vehicle type", "vehicle_type")
    tables["dim_address"] = (b.addresses.drop(columns=["demand_weight"]), "one row per delivery address", "address_id")
    tables["dim_site"] = (rp("network_sites.parquet"), "one row per candidate MFC site", "site_id")
    # ---------------- facts
    o = b.orders.copy()
    o["date_key"] = _date_key(o["order_date"])
    tables["fact_orders"] = (o.drop(columns=["order_date", "lat", "lon"]), "one row per order (90 days)", "order_id")
    cal = b.calendar.copy()
    cal["date_key"] = _date_key(cal["date"])
    tables["fact_city_day"] = (cal.drop(columns=["date", "day_name", "dow", "days_to_diwali"]),
                               "one row per city per day (events, rain, multipliers)", "city_id + date_key")
    tables["fact_routes"] = (rp("routes_summary.parquet"), "one row per plan x vehicle route (representative day)",
                             "plan + vehicle_id")
    tables["fact_route_stops"] = (rp("route_stops.parquet"), "one row per plan x stop", "plan + order_id")
    tables["fact_unassigned"] = (rp("unassigned_orders.parquet"), "one row per plan x unserved order", "plan + order_id")
    tables["fact_fleet_scenarios"] = (rp("fleet_scenarios.parquet"), "one row per city (or ALL) x demand level",
                                      "city_id + demand_multiplier")
    tables["fact_fleet_tco"] = (rp("fleet_tco.parquet"), "one row per city x demand level x vehicle type",
                                "city_id + demand_multiplier + vehicle_type")
    tables["fact_warehouse_performance"] = (rp("warehouse_performance.parquet"), "one row per city DC x layout", "city_id + layout")
    tables["fact_warehouse_heatmap"] = (rp("warehouse_heatmap.parquet"), "one row per city DC x aisle x bay", "city_id + aisle + bay")
    tables["fact_simulation_runs"] = (rp("simulation_replications.parquet"), "one row per city x scenario x replication",
                                      "city_id + scenario + replication")
    tables["fact_simulation_summary"] = (rp("simulation_summary.parquet"), "one row per city x scenario", "city_id + scenario")
    tables["fact_network_scenarios"] = (rp("network_scenarios.parquet"), "one row per city x p", "city_id + p")
    tables["fact_network_cashflows"] = (rp("network_cashflows.parquet"), "one row per city x p x year", "city_id + p + year")
    tables["fact_network_assignments"] = (rp("network_assignments.parquet"), "one row per city x p x address",
                                          "city_id + p + address_id")
    tables["fact_ml_performance"] = (rp("ml_performance.parquet"), "one row per model x approach", "model + approach")
    dv = rp("ml_demand_validation.parquet")
    dv["date_key"] = _date_key(dv["date"])
    tables["fact_ml_demand_validation"] = (dv.drop(columns=["date"]), "one row per zone x validation day", "zone + date_key")
    tables["fact_cc_alerts"] = (rp("cc_alerts.parquet"), "one row per Command Center alert", "row order")
    return tables


def run(cfg: dict[str, Any]) -> dict[str, Any]:
    """Write all star-schema tables and the manifest."""
    out = resolve_path(cfg, "bi_dir")
    manifest = []
    for name, (df, grain, keys) in build_tables(cfg).items():
        df.to_csv(out / f"{name}.csv", index=False)
        df.to_parquet(out / f"{name}.parquet", index=False)
        manifest.append({"table": name, "type": "dimension" if name.startswith("dim_") else "fact", "grain": grain,
                         "keys": keys, "rows": int(len(df)), "columns": [str(c) for c in df.columns]})
    save_json({"tables": manifest, "refresh": "python run_demo.py --modules all --export-reports, then refresh the model"},
              out / "bi_manifest.json")
    log.info("BI export: %d tables to %s", len(manifest), out)
    return {"tables": manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None)
    run(load_config(parser.parse_args().config))


if __name__ == "__main__":
    main()
