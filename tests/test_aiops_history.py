from datetime import datetime, timedelta, timezone

import pytest

import backup_audit.aiops_history as history
from backup_audit.audit import AuditResult
from backup_audit.models import CheckResult, Status, Target, TargetKind


@pytest.fixture(autouse=True)
def _isolate_history_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "audit_history.json")


def _target(name: str) -> Target:
    return Target(name=name, kind=TargetKind.GITHUB_RELEASE, location="o/r", freshness_hours=720)


def _obs(ts: datetime, **targets: dict) -> dict:
    return {"timestamp": ts.isoformat(), "targets": targets}


def _t(status: str, age_hours: float | None = 1.0, release_url: str | None = None, location: str = "o/r", kind: str = "github_release") -> dict:
    return {"status": status, "age_hours": age_hours, "release_url": release_url, "location": location, "kind": kind}


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------
# record_observation / load_history
# ------------------------------------------------------------------


def test_record_observation_round_trip():
    result = AuditResult(results=(CheckResult(_target("a"), Status.STALE, "release v1", age_hours=10.0, release_url="https://x"),))
    history.record_observation(result, now=T0)
    observations = history.load_history()
    assert len(observations) == 1
    assert observations[0]["targets"]["a"] == {
        "status": "stale",
        "age_hours": 10.0,
        "release_url": "https://x",
        "location": "o/r",
        "kind": "github_release",
    }


def test_record_observation_appends_across_calls():
    result = AuditResult(results=(CheckResult(_target("a"), Status.OK, "fresh", age_hours=1.0),))
    history.record_observation(result, now=T0)
    history.record_observation(result, now=T0 + timedelta(hours=1))
    assert len(history.load_history()) == 2


def test_record_observation_bounded_retention(monkeypatch):
    monkeypatch.setattr(history, "MAX_OBSERVATIONS", 5)
    result = AuditResult(results=(CheckResult(_target("a"), Status.OK, "fresh", age_hours=1.0),))
    for i in range(5 + 3):
        history.record_observation(result, now=T0 + timedelta(minutes=30 * i))
    observations = history.load_history()
    assert len(observations) == 5


def test_load_history_empty_when_no_file():
    assert history.load_history() == []


# ------------------------------------------------------------------
# _stale_frequency
# ------------------------------------------------------------------


def test_stale_frequency_none_below_observation_count():
    observations = [
        _obs(T0 + timedelta(days=i), flaky=_t("stale"), steady=_t("ok")) for i in range(10)
    ]
    assert history._stale_frequency(observations) is None


def test_stale_frequency_none_below_span():
    # Enough observations, but packed into too short a wall-clock span.
    observations = [
        _obs(T0 + timedelta(hours=i), flaky=_t("stale"), steady=_t("ok")) for i in range(25)
    ]
    assert history._stale_frequency(observations) is None


def test_stale_frequency_detects_outlier():
    observations = []
    for i in range(25):
        ts = T0 + timedelta(days=i * 31 / 24)
        flaky_status = "stale" if i % 5 != 0 else "ok"  # stale 80% of the time
        steady_status = "stale" if i % 12 == 0 else "ok"  # stale ~8% of the time
        observations.append(_obs(ts, flaky=_t(flaky_status), steady=_t(steady_status)))

    finding = history._stale_frequency(observations)
    assert finding is not None
    assert finding["kind"] == "stale_frequency"
    assert finding["target_names"] == ["flaky"]
    assert finding["rate"] == pytest.approx(0.8)


def test_stale_frequency_none_when_no_clear_outlier():
    observations = [
        _obs(T0 + timedelta(days=i * 31 / 24), a=_t("stale" if i % 2 == 0 else "ok"), b=_t("stale" if i % 2 == 1 else "ok"))
        for i in range(25)
    ]
    # Both targets are stale ~50% of the time -- no outlier.
    assert history._stale_frequency(observations) is None


# ------------------------------------------------------------------
# _release_cadence
# ------------------------------------------------------------------


def test_release_cadence_none_with_too_few_events():
    observations = [
        _obs(T0, cadence=_t("ok", age_hours=5.0)),
        _obs(T0 + timedelta(days=10), cadence=_t("ok", age_hours=245.0)),
        _obs(T0 + timedelta(days=10, minutes=1), cadence=_t("ok", age_hours=3.0)),  # 1 release event only
    ]
    assert history._release_cadence(observations) is None


def test_release_cadence_computes_average_gap():
    observations = [
        _obs(T0, cadence=_t("ok", age_hours=5.0)),
        _obs(T0 + timedelta(days=10), cadence=_t("ok", age_hours=245.0)),
        _obs(T0 + timedelta(days=10, minutes=1), cadence=_t("ok", age_hours=3.0)),  # event 1
        _obs(T0 + timedelta(days=20), cadence=_t("ok", age_hours=243.0)),
        _obs(T0 + timedelta(days=20, minutes=1), cadence=_t("ok", age_hours=4.0)),  # event 2
        _obs(T0 + timedelta(days=30), cadence=_t("ok", age_hours=241.0)),
        _obs(T0 + timedelta(days=30, minutes=1), cadence=_t("ok", age_hours=2.0)),  # event 3
    ]
    finding = history._release_cadence(observations)
    assert finding is not None
    assert finding["kind"] == "release_cadence"
    assert finding["target_names"] == ["cadence"]
    assert finding["gap_count"] == 2
    assert finding["avg_gap_hours"] == pytest.approx(240.0, abs=1.0)


# ------------------------------------------------------------------
# _clustering
# ------------------------------------------------------------------


def test_clustering_none_with_single_transition():
    observations = [
        _obs(T0, a=_t("ok"), b=_t("ok")),
        _obs(T0 + timedelta(hours=1), a=_t("stale"), b=_t("ok")),
    ]
    assert history._clustering(observations) is None


def test_clustering_detects_simultaneous_staleness():
    observations = [
        _obs(T0, a=_t("ok"), b=_t("ok")),
        _obs(T0 + timedelta(hours=1), a=_t("stale"), b=_t("ok")),
        _obs(T0 + timedelta(hours=2), a=_t("stale"), b=_t("stale")),
    ]
    finding = history._clustering(observations)
    assert finding is not None
    assert finding["kind"] == "clustering"
    assert set(finding["target_names"]) == {"a", "b"}
    assert finding["cluster_size"] == 2


def test_clustering_none_when_transitions_are_far_apart():
    observations = [
        _obs(T0, a=_t("ok"), b=_t("ok")),
        _obs(T0 + timedelta(hours=1), a=_t("stale"), b=_t("ok")),
        _obs(T0 + timedelta(days=5), a=_t("stale"), b=_t("stale")),
    ]
    assert history._clustering(observations) is None


# ------------------------------------------------------------------
# _duration_until_fixed
# ------------------------------------------------------------------


def test_duration_until_fixed_none_with_no_completed_cycle():
    observations = [
        _obs(T0, x=_t("ok")),
        _obs(T0 + timedelta(hours=5), x=_t("stale")),
    ]
    assert history._duration_until_fixed(observations) is None


def test_duration_until_fixed_computes_average():
    observations = [
        _obs(T0, x=_t("ok")),
        _obs(T0 + timedelta(hours=5), x=_t("stale")),
        _obs(T0 + timedelta(hours=15), x=_t("ok")),  # cycle: 10h stale
    ]
    finding = history._duration_until_fixed(observations)
    assert finding is not None
    assert finding["kind"] == "duration_until_fixed"
    assert finding["target_names"] == ["x"]
    assert finding["cycle_count"] == 1
    assert finding["avg_hours"] == pytest.approx(10.0)


# ------------------------------------------------------------------
# find_notable_pattern
# ------------------------------------------------------------------


def test_find_notable_pattern_none_on_empty_history():
    assert history.find_notable_pattern([]) is None


def test_find_notable_pattern_prefers_duration_over_frequency(monkeypatch):
    monkeypatch.setattr(history, "_duration_until_fixed", lambda obs: {"kind": "duration_until_fixed"})
    monkeypatch.setattr(history, "_clustering", lambda obs: None)
    monkeypatch.setattr(history, "_release_cadence", lambda obs: None)
    monkeypatch.setattr(history, "_stale_frequency", lambda obs: {"kind": "stale_frequency"})
    assert history.find_notable_pattern([{}])["kind"] == "duration_until_fixed"


def test_find_notable_pattern_falls_through_to_frequency(monkeypatch):
    monkeypatch.setattr(history, "_duration_until_fixed", lambda obs: None)
    monkeypatch.setattr(history, "_clustering", lambda obs: None)
    monkeypatch.setattr(history, "_release_cadence", lambda obs: None)
    monkeypatch.setattr(history, "_stale_frequency", lambda obs: {"kind": "stale_frequency"})
    assert history.find_notable_pattern([{}])["kind"] == "stale_frequency"
