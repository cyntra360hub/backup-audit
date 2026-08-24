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
    # The GitHub release's own html_url (a direct .../releases/tag/{tag}
    # page, never a redirect) when the checker fetched a real release
    # object -- None for non-github_release targets, or when there is no
    # release at all to link to. See checkers.check_github_release.
    release_url: str | None = None
