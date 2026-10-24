"""Portfolio design tokens shared by Excel, Dash and the static dashboard.

Palette is drawn from the Mumbai logistics context: harbour ink, sea-link teal,
marigold (festive peak / road signage) and signal red for alerts.
"""
from __future__ import annotations

PALETTE: dict[str, str] = {
    "ink": "1F2A44",        # headings, KPI values
    "teal": "0E7C86",       # optimized / primary series
    "slate": "6B7A90",      # baseline / secondary series
    "marigold": "E3A21A",   # highlights, peak period
    "signal": "C2413B",     # alerts, violations
    "leaf": "3C8D5A",       # targets met
    "mist": "EEF2F6",       # panel fill
    "paper": "FFFFFF",
    "rule": "CFD6DF",       # borders
}
SERIES: list[str] = ["0E7C86", "E3A21A", "6B7A90", "3C8D5A", "8E5A9B", "C2413B", "2F6DB5", "B8860B"]
VEHICLE_COLORS: dict[str, str] = {"VAN_2T": "1F2A44", "TEMPO_1T": "0E7C86", "TWO_WHEELER": "E3A21A"}
DEPOT_COLORS: dict[str, str] = {"DEP-BHW": "1F2A44", "DEP-AND": "0E7C86", "DEP-NVM": "E3A21A"}
FONT_EXCEL = "Arial"
FONT_WEB = "'Archivo', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def hexc(name: str) -> str:
    """Return a ``#RRGGBB`` string for a palette key or raw hex."""
    value = PALETTE.get(name, name)
    return f"#{value.lstrip('#')}"
