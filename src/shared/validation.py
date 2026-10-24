"""Lightweight schema and data-quality validation for tabular data contracts."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


class DataValidationError(ValueError):
    """Raised when a table violates its data contract."""


@dataclass
class ColumnRule:
    """Contract for one column."""

    name: str
    kind: str = "any"            # numeric | string | datetime | any
    nullable: bool = False
    min: float | None = None
    max: float | None = None
    allowed: set | None = None
    unique: bool = False


@dataclass
class TableContract:
    """Contract for a table: required columns plus row-level rules."""

    name: str
    columns: list[ColumnRule] = field(default_factory=list)
    min_rows: int = 1

    def validate(self, df: pd.DataFrame) -> list[str]:
        """Return a list of violations (empty list = valid)."""
        issues: list[str] = []
        if len(df) < self.min_rows:
            issues.append(f"{self.name}: expected >= {self.min_rows} rows, got {len(df)}")
        for rule in self.columns:
            if rule.name not in df.columns:
                issues.append(f"{self.name}: missing column '{rule.name}'")
                continue
            s = df[rule.name]
            if not rule.nullable and s.isna().any():
                issues.append(f"{self.name}.{rule.name}: {int(s.isna().sum())} null values")
            if rule.kind == "numeric" and not pd.api.types.is_numeric_dtype(s):
                issues.append(f"{self.name}.{rule.name}: expected numeric dtype, got {s.dtype}")
            if rule.kind == "datetime" and not pd.api.types.is_datetime64_any_dtype(s):
                issues.append(f"{self.name}.{rule.name}: expected datetime dtype, got {s.dtype}")
            if rule.min is not None and (s.dropna() < rule.min).any():
                issues.append(f"{self.name}.{rule.name}: values below {rule.min}")
            if rule.max is not None and (s.dropna() > rule.max).any():
                issues.append(f"{self.name}.{rule.name}: values above {rule.max}")
            if rule.allowed is not None:
                bad = set(s.dropna().unique()) - set(rule.allowed)
                if bad:
                    issues.append(f"{self.name}.{rule.name}: unexpected values {sorted(map(str, bad))[:5]}")
            if rule.unique and s.duplicated().any():
                issues.append(f"{self.name}.{rule.name}: {int(s.duplicated().sum())} duplicate keys")
        return issues

    def enforce(self, df: pd.DataFrame) -> pd.DataFrame:
        """Validate and raise :class:`DataValidationError` on any violation."""
        issues = self.validate(df)
        if issues:
            raise DataValidationError("; ".join(issues))
        return df


def check_foreign_key(child: pd.DataFrame, col: str, parent: pd.DataFrame, parent_col: str, label: str) -> list[str]:
    """Return a violation message if ``child[col]`` has values missing in ``parent``."""
    missing = set(child[col].dropna()) - set(parent[parent_col])
    return [f"{label}: {len(missing)} orphan keys in {col}"] if missing else []
