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
    assert result.findings_summary is None


def test_missing_target_is_success_with_findings(monkeypatch):
    # A detected missing backup is a successful detection, not a tool
    # failure -- outcome stays "success"; the finding is surfaced via
    # findings_summary (reported as the event's external_ref).
    monkeypatch.setitem(
        audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.MISSING)
    )
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert result.has_missing_or_error
    assert not result.has_errors
    assert result.outcome == "success"
    assert result.findings_summary == "found 1 backup issue across 1 target -- e.g. t (missing)"
    assert result.technical_summary == "missing: t"


def test_error_target_is_failure(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.ERROR))
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert result.has_errors
    assert result.outcome == "failure"


def test_stale_target_is_success_with_findings(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.STALE))
    config = Config(targets=(_target("t"),))
    result = run_audit(config)
    assert not result.all_ok
    assert not result.has_errors
    assert result.outcome == "success"
    assert result.findings_summary == "found 1 backup issue across 1 target -- e.g. t (stale)"
    assert result.technical_summary == "stale: t"


def test_findings_summary_names_only_one_example_with_a_count(monkeypatch):
    def checker(target: Target, now=None):
        status = Status.MISSING if target.name == "missing-one" else Status.STALE
        return CheckResult(target, status, "fake")

    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, checker)
    config = Config(targets=(_target("missing-one"), _target("stale-one")))
    result = run_audit(config)
    assert result.findings_summary == "found 2 backup issues across 2 targets -- e.g. missing-one (missing)"
    assert "stale-one" not in result.findings_summary


def test_technical_summary_lists_both_missing_and_stale(monkeypatch):
    def checker(target: Target, now=None):
        status = Status.MISSING if target.name == "missing-one" else Status.STALE
        return CheckResult(target, status, "fake")

    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, checker)
    config = Config(targets=(_target("missing-one"), _target("stale-one")))
    result = run_audit(config)
    assert result.technical_summary == "missing: missing-one; stale: stale-one"


def test_multiple_targets_all_present_in_results(monkeypatch):
    monkeypatch.setitem(audit_mod._CHECKERS, TargetKind.GITHUB_RELEASE, _fake_checker(Status.OK))
    config = Config(targets=(_target("a"), _target("b")))
    result = run_audit(config)
    assert len(result.results) == 2
    assert {r.target.name for r in result.results} == {"a", "b"}
