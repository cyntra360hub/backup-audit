"""Orchestrates checking every configured target and reducing to a
single run-level report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backup_audit.checkers import check_file, check_github_release, check_url
from backup_audit.config import Config
from backup_audit.models import CheckResult, Status, Target, TargetKind

_CHECKERS = {
    TargetKind.GITHUB_RELEASE: check_github_release,
    TargetKind.URL: check_url,
    TargetKind.FILE: check_file,
}


@dataclass(frozen=True)
class AuditResult:
    results: tuple[CheckResult, ...]

    @property
    def all_ok(self) -> bool:
        return all(r.status == Status.OK for r in self.results)

    @property
    def has_missing_or_error(self) -> bool:
        return any(r.status in (Status.MISSING, Status.ERROR) for r in self.results)

    @property
    def outcome(self) -> str:
        """Maps to the AiOps Enabler `task_completed` outcome enum
        (success | failure | escalated) -- computed here even though this
        package has no reporting code of its own, so the workflow's
        reporting step (see README) can parse a single stable value out
        of the CLI's plain-text report without any AiOps-specific logic
        in this package."""
        if self.all_ok:
            return "success"
        if self.has_missing_or_error:
            return "failure"
        return "escalated"


def _check_target(target: Target, now: datetime | None) -> CheckResult:
    checker = _CHECKERS[target.kind]
    return checker(target, now=now)


def run_audit(config: Config, now: datetime | None = None) -> AuditResult:
    results = tuple(_check_target(t, now) for t in config.targets)
    return AuditResult(results=results)
