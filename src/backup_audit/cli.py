"""backup-audit command-line entry point.

No AiOps Enabler reporting code lives here or anywhere else in this
package -- see README "Optional: AiOps Enabler integration". This CLI's
only contract with that integration is printing two stable, greppable
lines -- "Overall: outcome=<value>" and "Findings: <summary>" (only
printed when there is one) -- which `.github/workflows/scheduled.yml`
parses in plain shell.
"""

from __future__ import annotations

from backup_audit.audit import AuditResult, run_audit
from backup_audit.config import load_config
from backup_audit.models import Status

_STATUS_LABEL = {
    Status.OK: "OK",
    Status.STALE: "STALE",
    Status.MISSING: "MISSING",
    Status.ERROR: "ERROR",
}


def _print_report(result: AuditResult) -> None:
    for check in result.results:
        age = f", {check.age_hours:.1f}h old" if check.age_hours is not None else ""
        print(f"[{_STATUS_LABEL[check.status]}] {check.target.name}: {check.detail}{age}")
    print()
    print(f"Overall: outcome={result.outcome}")
    if result.findings_summary:
        print(f"Findings: {result.findings_summary}")


def main() -> int:
    config = load_config()
    result = run_audit(config)
    _print_report(result)
    return 1 if result.has_missing_or_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
