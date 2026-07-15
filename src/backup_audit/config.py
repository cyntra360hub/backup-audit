"""Configuration for backup-audit, sourced from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from backup_audit.models import Target, TargetKind

DEFAULT_FRESHNESS_HOURS = 24 * 30  # 30 days

# Default targets: the latest GitHub release of each of the other four
# public-interest agents in this project. Documented as replaceable via
# BACKUP_AUDIT_TARGETS -- see README.
DEFAULT_TARGETS: tuple[Target, ...] = tuple(
    Target(
        name=f"{repo} latest release",
        kind=TargetKind.GITHUB_RELEASE,
        location=f"cyntra360hub/{repo}",
        freshness_hours=DEFAULT_FRESHNESS_HOURS,
    )
    for repo in ("cert-sentinel", "status-watch", "ci-triage", "alert-dedupe")
)


@dataclass(frozen=True)
class Config:
    targets: tuple[Target, ...] = DEFAULT_TARGETS


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a Config from environment variables (or an injected mapping,
    for tests). BACKUP_AUDIT_TARGETS, if set, is a JSON array of
    `{"name", "kind", "location", "freshness_hours"}` objects, fully
    replacing the default target list -- see README "Configuring
    targets"."""
    source = env if env is not None else os.environ

    raw_targets = (source.get("BACKUP_AUDIT_TARGETS") or "").strip()
    if raw_targets:
        parsed = json.loads(raw_targets)
        targets = tuple(
            Target(
                name=t["name"],
                kind=TargetKind(t["kind"]),
                location=t["location"],
                freshness_hours=float(t.get("freshness_hours", DEFAULT_FRESHNESS_HOURS)),
            )
            for t in parsed
        )
    else:
        targets = DEFAULT_TARGETS

    return Config(targets=targets)
