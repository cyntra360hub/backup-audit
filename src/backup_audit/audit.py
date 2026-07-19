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
        """Used for this CLI's own exit code (a real monitoring signal
        for cron/CI use) -- distinct from `outcome` below, which reports
        to AiOps Enabler and treats MISSING/STALE as a successful
        detection, not a tool failure."""
        return any(r.status in (Status.MISSING, Status.ERROR) for r in self.results)

    @property
    def has_errors(self) -> bool:
        """True only when a check itself couldn't run (network failure,
        unexpected API response, etc.) -- as opposed to STALE/MISSING,
        which mean the check ran fine and correctly found that a backup
        is old or absent. This is the only condition that should ever
        report `outcome=failure` to AiOps Enabler; a detected stale or
        missing backup is the agent doing its job, not the agent
        failing."""
        return any(r.status == Status.ERROR for r in self.results)

    @property
    def findings_summary(self) -> str | None:
        """A compact, human-readable summary of any STALE/MISSING
        targets, for the AiOps Enabler event's `external_ref` field (the
        only freeform field the events API offers). None when everything
        is OK. Printed by the CLI as a stable, greppable line so the
        copyable workflow reporting step (see README) can forward it
        without any AiOps-specific logic in this package."""
        missing = [r.target.name for r in self.results if r.status == Status.MISSING]
        stale = [r.target.name for r in self.results if r.status == Status.STALE]
        if not missing and not stale:
            return None
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if stale:
            details.append(f"stale: {', '.join(stale)}")
        return f"swept {len(self.results)} target(s) -- {'; '.join(details)}"[:255]

    @property
    def outcome(self) -> str:
        """Maps to the AiOps Enabler `task_completed` outcome enum
        (success | failure) -- computed here even though this package has
        no reporting code of its own, so the workflow's reporting step
        (see README) can parse a single stable value out of the CLI's
        plain-text report without any AiOps-specific logic in this
        package. `failure` is reserved for a check itself erroring out
        (see `has_errors`); a completed audit that *found* a stale or
        missing backup is still `success` -- detection is this agent
        doing its job, and the findings are reported via `external_ref`
        (see `findings_summary`), not via a non-success outcome."""
        return "failure" if self.has_errors else "success"


def _check_target(target: Target, now: datetime | None) -> CheckResult:
    checker = _CHECKERS[target.kind]
    return checker(target, now=now)


def run_audit(config: Config, now: datetime | None = None) -> AuditResult:
    results = tuple(_check_target(t, now) for t in config.targets)
    return AuditResult(results=results)
