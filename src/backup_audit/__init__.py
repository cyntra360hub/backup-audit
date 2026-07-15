"""backup-audit: verifies configured backup artifacts exist and are
fresh, flagging stale or missing ones.

Deliberately has NO AiOps Enabler reporting code anywhere in this
package -- that integration lives entirely in
`.github/workflows/scheduled.yml` as a copyable template. See README
"Optional: AiOps Enabler integration" for why."""

from backup_audit.audit import AuditResult, run_audit

__all__ = ["AuditResult", "run_audit"]
__version__ = "0.1.0"
