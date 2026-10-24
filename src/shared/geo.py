"""Geodesic helpers: vectorised Haversine distances and matrices (kilometres)."""
from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius, matches geopy.distance.great_circle


def haversine_km(lat1: ArrayLike, lon1: ArrayLike, lat2: ArrayLike, lon2: ArrayLike) -> NDArray[np.float64]:
    """Great-circle distance in km between point arrays (broadcastable)."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(a, dtype=float)) for a in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def haversine_matrix(lats: ArrayLike, lons: ArrayLike,
                     lats2: ArrayLike | None = None, lons2: ArrayLike | None = None) -> NDArray[np.float64]:
    """Pairwise straight-line distance matrix in km (rows: first set, cols: second set)."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    lats2 = lats if lats2 is None else np.asarray(lats2, dtype=float)
    lons2 = lons if lons2 is None else np.asarray(lons2, dtype=float)
    return haversine_km(lats[:, None], lons[:, None], lats2[None, :], lons2[None, :])
