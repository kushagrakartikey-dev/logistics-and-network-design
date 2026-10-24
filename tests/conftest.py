"""Shared fixtures: a small two-city dataset (Mumbai + Kolkata) generated into a temp folder."""
from __future__ import annotations

import pytest

from src.logistics import data_generator
from src.shared.config import load_config

SMALL = {
    "addresses.count_per_city": 80,
    "orders.base_daily_orders": 40,
    "calendar.days": 35,
    "telematics.trip_logs_per_day": 25,
    "warehouse.sku_count": 600,
    "warehouse.families": 40,
    "warehouse.pick_orders": 1500,
    "simulation.replications": 20,
    "simulation.search_replications": 8,
    "routing.time_limit_s": 3,
    "fleet_sizing.time_limit_s": 3,
}


def small_config(tmp_path, cities=("MUM", "KOL")):
    cfg = load_config(overrides=SMALL)
    cfg["cities"] = {k: v for k, v in cfg["cities"].items() if k in cities}
    cfg["_root"] = str(tmp_path)
    return cfg


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    return small_config(tmp_path_factory.mktemp("sccc"))


@pytest.fixture(scope="session")
def bundle(cfg):
    return data_generator.run(cfg)
