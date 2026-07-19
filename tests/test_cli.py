import backup_audit.audit as audit_mod
import backup_audit.cli as cli_mod
from backup_audit.config import Config
from backup_audit.models import CheckResult, Status, Target, TargetKind


def _target(name: str) -> Target:
    return Target(name=name, kind=TargetKind.GITHUB_RELEASE, location="o/r", freshness_hours=720)


def _fake_checker(status: Status):
    def checker(target: Target, now=None):
        return CheckResult(target, status, "fake")

    return checker


def test_findings_line_printed_when_missing(monkeypatch, capsys):
    monkeypatch.setitem(
        audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.MISSING)
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: Config(targets=(_target("t"),)))

    exit_code = cli_mod.main()

    captured = capsys.readouterr()
    assert "Overall: outcome=success" in captured.out
    assert "Findings: swept 1 target(s) -- missing: t" in captured.out
    # A detected finding is not a tool failure for AiOps outcome purposes,
    # but the CLI's own exit code still flags it for local/cron use.
    assert exit_code == 1


def test_no_findings_line_when_all_ok(monkeypatch, capsys):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.OK))
    monkeypatch.setattr(cli_mod, "load_config", lambda: Config(targets=(_target("t"),)))

    exit_code = cli_mod.main()

    captured = capsys.readouterr()
    assert "Overall: outcome=success" in captured.out
    assert "Findings:" not in captured.out
    assert exit_code == 0


def test_error_target_is_failure_outcome(monkeypatch, capsys):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.ERROR))
    monkeypatch.setattr(cli_mod, "load_config", lambda: Config(targets=(_target("t"),)))

    exit_code = cli_mod.main()

    captured = capsys.readouterr()
    assert "Overall: outcome=failure" in captured.out
    assert exit_code == 1
