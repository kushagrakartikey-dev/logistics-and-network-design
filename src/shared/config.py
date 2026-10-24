"""Configuration loading with dotted-path access and repository-relative paths."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG: Path = REPO_ROOT / "config" / "logistics.yaml"


class ConfigError(KeyError):
    """Raised when a configuration key is missing or malformed."""


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a YAML configuration file and apply dotted-key overrides.

    Args:
        path: YAML file; defaults to ``config/logistics.yaml``.
        overrides: mapping of dotted keys (``"routing.time_limit_s"``) to values.

    Returns:
        The configuration dictionary (a fresh copy on every call).
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    cfg = copy.deepcopy(cfg)
    for key, value in (overrides or {}).items():
        set_by_path(cfg, key, value)
    return cfg


def get_by_path(cfg: dict[str, Any], key: str) -> Any:
    """Resolve a dotted key such as ``fleet.vehicle_types.VAN_2T.capacity_kg``."""
    node: Any = cfg
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise ConfigError(f"Config key not found: {key}")
    return node


def set_by_path(cfg: dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted key, creating intermediate dictionaries if needed."""
    parts = key.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def resolve_path(cfg: dict[str, Any], key: str, root: Path | None = None) -> Path:
    """Return an absolute path for ``paths.<key>``, creating the parent folder."""
    rel = Path(get_by_path(cfg, f"paths.{key}"))
    path = rel if rel.is_absolute() else (root or Path(cfg.get("_root", REPO_ROOT))) / rel
    target_dir = path if path.suffix == "" else path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    return path
