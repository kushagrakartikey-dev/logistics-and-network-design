"""Small I/O helpers: parquet/CSV/JSON writing with directory creation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


class _NumpyEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # noqa: D401 - json hook
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return None if np.isnan(o) else round(float(o), 6)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (pd.Timestamp,)):
            return o.isoformat()
        return super().default(o)


def write_table(df: pd.DataFrame, path: Path, csv_copy: bool = False) -> Path:
    """Write a DataFrame to parquet (and optionally a CSV sibling)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    if csv_copy:
        df.to_csv(path.with_suffix(".csv"), index=False)
    return path


def read_table(path: Path) -> pd.DataFrame:
    """Read a parquet table written by :func:`write_table`."""
    return pd.read_parquet(path)


def _clean(obj: Any) -> Any:
    """Replace NaN/inf (invalid in strict JSON, e.g. browsers' JSON.parse) with None, recursively."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (float, np.floating)) and not np.isfinite(obj):
        return None
    return obj


def save_json(obj: Any, path: Path, compact: bool = False) -> Path:
    """Serialise ``obj`` to strict JSON (NaN/inf -> null) handling numpy/pandas scalars."""
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = _clean(obj)
    with open(path, "w", encoding="utf-8") as fh:
        if compact:
            json.dump(obj, fh, cls=_NumpyEncoder, separators=(",", ":"), allow_nan=False)
        else:
            json.dump(obj, fh, cls=_NumpyEncoder, indent=2, allow_nan=False)
    return path


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
