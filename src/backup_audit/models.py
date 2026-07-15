"""Shared types for backup-audit."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TargetKind(str, Enum):
    GITHUB_RELEASE = "github_release"
    URL = "url"
    FILE = "file"


@dataclass(frozen=True)
class Target:
    name: str
    kind: TargetKind
    location: str
    freshness_hours: float


class Status(str, Enum):
    OK = "ok"
    STALE = "stale"
    MISSING = "missing"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    target: Target
    status: Status
    detail: str
    age_hours: float | None = None
