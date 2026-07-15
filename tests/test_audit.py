from backup_audit.audit import run_audit
from backup_audit.config import Config
from backup_audit.models import CheckResult, Status, Target, TargetKind

import backup_audit.audit as audit_mod


def _target(name: str) -> Target:
    return Target(name=name, kind=TargetKind.GITHUB_RELEASE, location="o/r", freshness_hours=720)


def _fake_checker(status: Status):
    def checker(target: Target, now=None):
        return CheckResult(target, status, "fake")

    return checker


def test_all_ok_is_success(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.OK))
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert result.all_ok
    assert result.outcome == "success"


def test_missing_target_is_failure(monkeypatch):
    monkeypatch.setitem(
        audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.MISSING)
    )
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert result.has_missing_or_error
    assert result.outcome == "failure"


def test_error_target_is_failure(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.ERROR))
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert result.outcome == "failure"


def test_stale_target_is_escalated(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.STALE))
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert not result.all_ok
    assert not result.has_missing_or_error
    assert result.outcome == "escalated"


def test_multiple_targets_all_present_in_results(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.OK))
    config = Config(targets=(_target("a"), _target("b")))
    result = run_audit(config)
    assert len(result.results) == 2
    assert {r.target.name for r in result.results} == {"a", "b"}
