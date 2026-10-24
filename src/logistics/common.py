"""Shared logistics primitives: traffic bands, travel times, tables and contracts."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.shared.config import resolve_path
from src.shared.io_utils import read_table, write_table
from src.shared.validation import ColumnRule as C
from src.shared.validation import TableContract

SEGMENTS = ("ECOM_B2C", "FMCG_RETAIL", "MODERN_RETAIL", "AUTOMOTIVE")
VEHICLE_TYPES = ("VAN_2T", "TEMPO_1T", "TWO_WHEELER")


# --------------------------------------------------------------------------- traffic
def traffic_factor(minute: float | np.ndarray, bands: list[dict[str, Any]]) -> np.ndarray:
    """Clock-time traffic multiplier for minutes since midnight (vectorised)."""
    m = np.mod(np.asarray(minute, dtype=float), 1440.0)
    out = np.ones_like(m)
    for band in bands:
        mask = (m >= band["start"]) & (m < band["end"])
        out = np.where(mask, float(band["factor"]), out)
    return out


def travel_minutes(road_km: float | np.ndarray, speed_kmph: float, depart_min: float | np.ndarray,
                   bands: list[dict[str, Any]]) -> np.ndarray:
    """Travel time in minutes: free-flow time x traffic factor at departure."""
    return np.asarray(road_km, dtype=float) / speed_kmph * 60.0 * traffic_factor(depart_min, bands)


def city_ids(cfg: dict[str, Any]) -> list[str]:
    """Configured metro cluster codes in order (e.g. MUM, DEL, BLR, CHE, KOL)."""
    return list(cfg["cities"].keys())


def city_of(entity_id: str) -> str:
    """City code embedded in a portfolio identifier such as ``DEP-MUM-DC`` or ``ORD-DEL-000123``."""
    return entity_id.split("-")[1]


def minutes_to_clock(m: float) -> str:
    """Format minutes-since-midnight as HH:MM."""
    m = int(round(m))
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------- bundle
@dataclass
class DataBundle:
    """All operational tables for the Mumbai scenario."""

    depots: pd.DataFrame
    vehicles: pd.DataFrame
    vehicle_types: pd.DataFrame
    addresses: pd.DataFrame
    calendar: pd.DataFrame
    orders: pd.DataFrame
    trip_logs: pd.DataFrame
    skus: pd.DataFrame
    slots: pd.DataFrame
    pick_lines: pd.DataFrame
    candidate_sites: pd.DataFrame
    staffing: pd.DataFrame

    def table_names(self) -> list[str]:
        return [f.name for f in fields(self)]

    def save(self, data_dir: Path) -> None:
        for name in self.table_names():
            write_table(getattr(self, name), data_dir / f"{name}.parquet")

    @classmethod
    def load(cls, data_dir: Path) -> DataBundle:
        missing = [f.name for f in fields(cls) if not (data_dir / f"{f.name}.parquet").exists()]
        if missing:
            raise FileNotFoundError(f"Dataset incomplete in {data_dir}; missing {missing}. Run the data module first.")
        return cls(**{f.name: read_table(data_dir / f"{f.name}.parquet") for f in fields(cls)})


def load_bundle(cfg: dict[str, Any]) -> DataBundle:
    """Load the generated dataset from ``paths.data_dir``."""
    return DataBundle.load(resolve_path(cfg, "data_dir"))


# --------------------------------------------------------------------------- contracts
CONTRACTS: dict[str, TableContract] = {
    "depots": TableContract("depots", [C("depot_id", "string", unique=True), C("city_id"), C("role", allowed={"DC", "MFC", "HUB"}),
                                       C("lat", "numeric", min=8.0, max=35.0), C("lon", "numeric", min=68.0, max=97.0),
                                       C("open_min", "numeric", min=0, max=1440),
                                       C("close_min", "numeric", min=0, max=1440)], min_rows=3),
    "vehicles": TableContract("vehicles", [C("vehicle_id", "string", unique=True), C("vehicle_type", allowed=set(VEHICLE_TYPES)),
                                           C("depot_id"), C("city_id"), C("capacity_kg", "numeric", min=1)], min_rows=1),
    "addresses": TableContract("addresses", [C("address_id", "string", unique=True), C("zone"),
                                             C("city_id"), C("state"), C("lat", "numeric", min=8.0, max=35.0),
                                             C("lon", "numeric", min=68.0, max=97.0),
                                             C("segment", allowed=set(SEGMENTS)), C("demand_weight", "numeric", min=0)], min_rows=10),
    "orders": TableContract("orders", [C("order_id", "string", unique=True), C("order_date", "datetime"), C("address_id"), C("city_id"),
                                       C("segment", allowed=set(SEGMENTS)), C("weight_kg", "numeric", min=0.05, max=2000),
                                       C("tw_start_min", "numeric", min=0, max=1440), C("tw_end_min", "numeric", min=0, max=1440),
                                       C("service_min", "numeric", min=0, max=120), C("lines", "numeric", min=1),
                                       C("priority", allowed={"EXPRESS", "STANDARD"}), C("depot_id"),
                                       C("intake_min", "numeric", min=0, max=1440)], min_rows=10),
    "trip_logs": TableContract("trip_logs", [C("leg_id", "string", unique=True), C("trip_date", "datetime"),
                                             C("haversine_km", "numeric", min=0), C("actual_minutes", "numeric", min=0)], min_rows=10),
    "skus": TableContract("skus", [C("sku_id", "string", unique=True), C("weight_kg", "numeric", min=0.01),
                                   C("family", "numeric", min=0)], min_rows=10),
    "slots": TableContract("slots", [C("slot_id", "string", unique=True), C("zone", allowed={"A", "B", "C"}),
                                     C("level", "numeric", min=1, max=10), C("travel_m", "numeric", min=0)], min_rows=10),
    "pick_lines": TableContract("pick_lines", [C("pick_order_id"), C("sku_id"), C("city_id"), C("qty", "numeric", min=1)], min_rows=10),
    "candidate_sites": TableContract("candidate_sites", [C("site_id", "string", unique=True), C("city_id"), C("lat", "numeric"),
                                                         C("lon", "numeric"), C("annual_rent_inr", "numeric", min=0)], min_rows=1),
}
