"""Command Center data contract.

Every portfolio output publishes ``outputs/command_center/<module>_contract.json``
with a fixed envelope: module, version, generated_at, status, kpis, interfaces.
Consumers read contracts through :func:`read_contract`, which returns ``None``
when an upstream module has not been run, so no upstream number is ever invented.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io_utils import load_json, save_json

CONTRACT_VERSION = "1.0"
KNOWN_MODULES = {
    "sourcing": "Output 1 - Strategic Sourcing",
    "planning": "Output 2 - S&OP Planning",
    "process": "Output 3 - Lean Six Sigma",
    "procurement": "Output 4 - Procurement Platform",
    "inventory": "Output 5 - Inventory Optimizer",
    "logistics": "Output 6 - Logistics and Network Design",
}


def contract_path(cc_dir: Path, module: str) -> Path:
    return cc_dir / f"{module}_contract.json"


def publish_contract(cc_dir: Path, module: str, kpis: dict[str, Any], interfaces: dict[str, Any],
                     status: str = "ok", notes: list[str] | None = None) -> Path:
    """Write a module contract to the Command Center folder."""
    payload = {
        "module": module,
        "title": KNOWN_MODULES.get(module, module),
        "version": CONTRACT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "kpis": kpis,
        "interfaces": interfaces,
        "notes": notes or [],
    }
    return save_json(payload, contract_path(cc_dir, module))


def read_contract(cc_dir: Path, module: str) -> dict[str, Any] | None:
    """Return a published contract or ``None`` if the module has not run."""
    path = contract_path(cc_dir, module)
    if not path.exists():
        return None
    data = load_json(path)
    if data.get("version") != CONTRACT_VERSION:
        return None
    return data


def upstream_status(cc_dir: Path) -> dict[str, str]:
    """Availability of every portfolio module's contract."""
    return {m: ("available" if contract_path(cc_dir, m).exists() else "not run") for m in KNOWN_MODULES}
