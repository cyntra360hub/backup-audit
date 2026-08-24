import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

import backup_audit.aiops_community as aiops
from backup_audit.audit import AuditResult
from backup_audit.models import CheckResult, Status, Target, TargetKind


def _target(name: str, freshness_hours: float = 720) -> Target:
    return Target(name=name, kind=TargetKind.GITHUB_RELEASE, location="o/r", freshness_hours=freshness_hours)


def _healthy_result() -> AuditResult:
    return AuditResult(results=(CheckResult(_target("a"), Status.OK, "fresh", age_hours=1.0),))


def _material_result() -> AuditResult:
    return AuditResult(
        results=(
            CheckResult(_target("missing-one"), Status.MISSING, "no releases published"),
            CheckResult(_target("stale-one"), Status.STALE, "release v1", age_hours=999.5),
            CheckResult(_target("ok-one"), Status.OK, "fresh", age_hours=1.0),
        )
    )


def _observation(targets: dict, timestamp: str = "2026-08-01T00:00:00+00:00") -> dict:
    return {"timestamp": timestamp, "targets": targets}


def _stale_frequency_pattern(name: str = "cert-sentinel latest release") -> dict:
    return {
        "kind": "stale_frequency",
        "target_names": [name],
        "rate": 0.8,
        "avg_others": 0.1,
        "span_days": 40,
        "observation_count": 100,
    }


def _observations_for(name: str, location: str = "o/r", release_url: str | None = "https://github.com/o/r/releases/tag/v1.0.0", kind: str = "github_release") -> list[dict]:
    return [
        _observation(
            {name: {"status": "stale", "age_hours": 100.0, "release_url": release_url, "location": location, "kind": kind}}
        )
    ]


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(aiops, "STATE_FILE", tmp_path / "aiops_community.json")


class FakeTransport:
    """Records every call it receives and returns canned (status, body,
    headers) tuples queued up front, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, path, *, api_key=None, json_body=None, timeout=30.0):
        self.calls.append({"method": method, "path": path, "api_key": api_key, "json_body": json_body})
        return self._responses.pop(0)


class _FakeHTTPErrorBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        pass


# ------------------------------------------------------------------
# http_request() -- the real transport
# ------------------------------------------------------------------


def test_http_request_returns_json_error_body(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 422, "Unprocessable", {}, _FakeHTTPErrorBody(b'{"reason": "too_vague"}')
        )

    monkeypatch.setattr(aiops.urllib.request, "urlopen", fake_urlopen)
    status, body, _ = aiops.http_request("POST", "/agents/posts", api_key="k", json_body={"a": 1})
    assert status == 422
    assert body == {"reason": "too_vague"}


def test_http_request_preserves_raw_body_when_not_json(monkeypatch):
    # This is the exact gap that made the 2026-08-24 503s undiagnosable
    # from our own logs: a non-JSON error body used to be silently
    # discarded as None instead of surfaced.
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, _FakeHTTPErrorBody(b"<html>upstream down</html>")
        )

    monkeypatch.setattr(aiops.urllib.request, "urlopen", fake_urlopen)
    status, body, _ = aiops.http_request("POST", "/agents/posts", api_key="k", json_body={"a": 1})
    assert status == 503
    assert body == {"_raw_body": "<html>upstream down</html>"}


# ------------------------------------------------------------------
# Building article/comment text from a pattern
# ------------------------------------------------------------------


def test_pattern_id_stable_for_same_content():
    a = aiops._pattern_id(_stale_frequency_pattern())
    b = aiops._pattern_id(_stale_frequency_pattern())
    assert a == b


def test_pattern_id_changes_with_content():
    a = aiops._pattern_id(_stale_frequency_pattern("cert-sentinel latest release"))
    b = aiops._pattern_id(_stale_frequency_pattern("status-watch latest release"))
    assert a != b


def test_pattern_id_ignores_float_jitter():
    p1 = _stale_frequency_pattern()
    p2 = {**p1, "rate": p1["rate"] + 1e-9}
    assert aiops._pattern_id(p1) == aiops._pattern_id(p2)


def test_source_url_uses_release_url_from_history():
    observations = _observations_for("t", release_url="https://github.com/o/r/releases/tag/v1.0.0")
    assert aiops._source_url_for(observations, "t") == "https://github.com/o/r/releases/tag/v1.0.0"


def test_source_url_falls_back_to_releases_index_for_github_target():
    observations = _observations_for("t", release_url=None)
    assert aiops._source_url_for(observations, "t") == "https://github.com/o/r/releases"


def test_source_url_none_for_non_github_target_with_no_release_url():
    observations = _observations_for("t", release_url=None, kind="file")
    assert aiops._source_url_for(observations, "t") is None


def test_source_url_none_when_target_never_observed():
    assert aiops._source_url_for([], "unseen") is None


def test_build_analytical_article_stale_frequency():
    pattern = _stale_frequency_pattern("cert-sentinel latest release")
    observations = _observations_for("cert-sentinel latest release")
    title, body, source_url = aiops._build_analytical_article(pattern, observations)
    assert 10 <= len(title) <= 140
    assert "cert-sentinel latest release" in title or "cert-sentinel latest release" in body
    assert "80.0%" in body
    assert "10.0%" in body
    assert len(body) >= 200
    assert source_url == "https://github.com/o/r/releases/tag/v1.0.0"
    assert "https://github.com" not in body  # never in body -- would be stripped anyway


def test_build_analytical_article_duration_until_fixed():
    pattern = {
        "kind": "duration_until_fixed",
        "target_names": ["ci-triage latest release"],
        "avg_hours": 12.5,
        "cycle_count": 3,
    }
    title, body, _ = aiops._build_analytical_article(pattern, _observations_for("ci-triage latest release"))
    assert "12" in title
    assert "3" in body
    assert len(body) >= 200


def test_build_analytical_article_clustering_names_all_targets():
    pattern = {
        "kind": "clustering",
        "target_names": ["a latest release", "b latest release"],
        "when": "2026-08-01T00:00:00+00:00",
        "cluster_size": 2,
        "total_went_stale_events": 5,
    }
    title, body, _ = aiops._build_analytical_article(pattern, _observations_for("a latest release"))
    assert "a latest release" in body
    assert "b latest release" in body
    assert len(body) >= 200


def test_build_analytical_article_release_cadence():
    pattern = {
        "kind": "release_cadence",
        "target_names": ["status-watch latest release"],
        "avg_gap_hours": 720.0,
        "min_gap_hours": 700.0,
        "max_gap_hours": 740.0,
        "gap_count": 3,
    }
    title, body, _ = aiops._build_analytical_article(pattern, _observations_for("status-watch latest release"))
    assert "status-watch latest release" in title or "status-watch latest release" in body
    assert "700" in body and "740" in body
    assert len(body) >= 200


def test_build_comment_from_pattern_has_real_numbers():
    comment = aiops._build_comment_from_pattern(_stale_frequency_pattern("cert-sentinel latest release"))
    assert "cert-sentinel latest release" in comment
    assert "80.0%" in comment


# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------


def test_state_round_trip():
    state = aiops.load_state()
    assert state == {"published": [], "rejected": {}, "commented_on": {}, "next_heartbeat_at": None}
    state["published"].append("abc123")
    aiops.save_state(state)
    assert aiops.load_state()["published"] == ["abc123"]


# ------------------------------------------------------------------
# publish()
# ------------------------------------------------------------------


def test_publish_skips_when_no_pattern():
    transport = FakeTransport([])
    aiops.publish(None, [], "key", aiops.load_state(), transport=transport)
    assert transport.calls == []


def test_publish_dry_run_makes_no_calls():
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport([])
    aiops.publish(pattern, observations, "key", aiops.load_state(), transport=transport, dry_run=True)
    assert transport.calls == []


def test_publish_success_records_state():
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/x"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(pattern, observations, "key", state, transport=transport)
    finding_id = aiops._pattern_id(pattern)
    assert finding_id in state["published"]
    assert aiops.load_state()["published"] == [finding_id]

    post_call = transport.calls[-1]
    assert post_call["json_body"]["source_url"] == "https://github.com/o/r/releases/tag/v1.0.0"


def test_publish_omits_source_url_when_not_applicable():
    pattern = _stale_frequency_pattern("local")
    observations = _observations_for("local", release_url=None, kind="file")
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/x"}, {}),
        ]
    )
    aiops.publish(pattern, observations, "key", aiops.load_state(), transport=transport)
    post_call = transport.calls[-1]
    assert "source_url" not in post_call["json_body"]


def test_publish_skips_when_already_published():
    pattern = _stale_frequency_pattern()
    state = aiops.load_state()
    state["published"].append(aiops._pattern_id(pattern))
    transport = FakeTransport([])
    aiops.publish(pattern, _observations_for(pattern["target_names"][0]), "key", state, transport=transport)
    assert transport.calls == []


def test_publish_422_records_rejection_and_does_not_retry():
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (422, {"reason": "too_vague"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(pattern, observations, "key", state, transport=transport)
    finding_id = aiops._pattern_id(pattern)
    assert state["rejected"][finding_id] == "too_vague"
    assert finding_id not in state["published"]

    # A second run with the same pattern must not resubmit.
    transport2 = FakeTransport([])
    aiops.publish(pattern, observations, "key", aiops.load_state(), transport=transport2)
    assert transport2.calls == []


def test_publish_skips_when_quota_spent():
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport([(200, {"posts_per_day": 2, "posts_used_today": 2}, {})])
    state = aiops.load_state()
    aiops.publish(pattern, observations, "key", state, transport=transport)
    assert state["published"] == []
    assert len(transport.calls) == 1  # only the quota check, no POST


def test_publish_503_does_not_record_anything():
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (503, None, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(pattern, observations, "key", state, transport=transport)
    assert state["published"] == []
    assert state["rejected"] == {}


def test_publish_logs_full_response_body_on_any_non_201(capsys):
    # Added after the 2026-08-24 outage: the code used to print a fixed
    # message on 503 with no visibility into the actual response body.
    # Whatever the transport returns must now show up in the output.
    pattern = _stale_frequency_pattern()
    observations = _observations_for(pattern["target_names"][0])
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (503, {"_raw_body": "<html>upstream error</html>"}, {}),
        ]
    )
    aiops.publish(pattern, observations, "key", aiops.load_state(), transport=transport)
    out = capsys.readouterr().out
    assert "503" in out
    assert "upstream error" in out


# ------------------------------------------------------------------
# join_discussion()
# ------------------------------------------------------------------


def test_join_discussion_skips_when_no_pattern():
    transport = FakeTransport([])
    aiops.join_discussion(None, "key", aiops.load_state(), transport=transport)
    assert transport.calls == []


def test_join_discussion_replies_to_live_edge_of_thread():
    pattern = _stale_frequency_pattern()
    transport = FakeTransport(
        [
            (200, {"data": [{"id": 1, "slug": "backup-drills", "title": "Backup drills", "excerpt": "", "agent": "other"}]}, {}),
            (200, {"discussion": [{"id": 55, "thread_root": 55, "created_at": "2026-01-01T00:00:00+00:00", "depth": 0}]}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/backup-drills#c2"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.join_discussion(pattern, "key", state, transport=transport)
    assert str(1) in state["commented_on"]
    post_call = transport.calls[-1]
    assert post_call["json_body"]["reply_to"] == 55
    assert post_call["json_body"]["post_id"] == 1


def test_join_discussion_falls_back_to_top_level_when_no_thread_yet():
    pattern = _stale_frequency_pattern()
    transport = FakeTransport(
        [
            (200, {"data": [{"id": 2, "slug": "dr-basics", "title": "Disaster recovery basics", "excerpt": "", "agent": "other"}]}, {}),
            (200, {"discussion": []}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/dr-basics#c1"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.join_discussion(pattern, "key", state, transport=transport)
    post_call = transport.calls[-1]
    assert "reply_to" not in post_call["json_body"]


def test_join_discussion_respects_24h_cooldown():
    pattern = _stale_frequency_pattern()
    state = aiops.load_state()
    state["commented_on"]["1"] = datetime.now(timezone.utc).isoformat()
    transport = FakeTransport([(200, {"data": [{"id": 1, "slug": "x", "title": "backup drift", "excerpt": "", "agent": "other"}]}, {})])
    aiops.join_discussion(pattern, "key", state, transport=transport)
    # Only the listing call happens; the article is filtered out before
    # its detail page (and definitely before posting) is ever fetched.
    assert len(transport.calls) == 1


def test_join_discussion_skips_own_articles():
    pattern = _stale_frequency_pattern()
    transport = FakeTransport([(200, {"data": [{"id": 9, "slug": "x", "title": "backup drift", "excerpt": "", "agent": aiops.AGENT_SLUG}]}, {})])
    aiops.join_discussion(pattern, "key", aiops.load_state(), transport=transport)
    assert len(transport.calls) == 1  # listing only, no detail/post fetch for our own article


# ------------------------------------------------------------------
# heartbeat()
# ------------------------------------------------------------------


def test_heartbeat_fires_on_first_run_and_schedules_next():
    transport = FakeTransport([(200, {"your_account": {"posts_used_today": 0, "posts_per_day": 2}}, {})])
    state = aiops.load_state()
    aiops.heartbeat("key", state, transport=transport)
    assert len(transport.calls) == 1
    next_at = datetime.fromisoformat(state["next_heartbeat_at"])
    now = datetime.now(timezone.utc)
    assert timedelta(hours=aiops.HEARTBEAT_MIN_HOURS) <= (next_at - now) <= timedelta(hours=aiops.HEARTBEAT_MAX_HOURS)


def test_heartbeat_skips_when_not_due():
    state = aiops.load_state()
    state["next_heartbeat_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    transport = FakeTransport([])
    aiops.heartbeat("key", state, transport=transport)
    assert transport.calls == []


# ------------------------------------------------------------------
# run() -- wiring history recording into the publish decision
# ------------------------------------------------------------------


def test_run_records_observation_and_publishes_nothing_with_empty_history(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(aiops, "STATE_FILE", tmp_path / "aiops_community.json")
    import backup_audit.aiops_history as history

    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "audit_history.json")
    transport = FakeTransport([(200, {"your_account": {"posts_used_today": 0, "posts_per_day": 2}}, {})])

    aiops.run(_material_result(), "key", transport=transport)

    out = capsys.readouterr().out
    assert "publishing nothing" in out
    assert history.load_history() != []  # this run's observation was recorded


def test_run_dry_run_does_not_record_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(aiops, "STATE_FILE", tmp_path / "aiops_community.json")
    import backup_audit.aiops_history as history

    monkeypatch.setattr(history, "HISTORY_FILE", tmp_path / "audit_history.json")
    transport = FakeTransport([])

    aiops.run(_material_result(), "key", transport=transport, dry_run=True)

    assert not (tmp_path / "audit_history.json").exists()


# ------------------------------------------------------------------
# main()
# ------------------------------------------------------------------


def test_main_returns_0_when_key_missing(monkeypatch, capsys):
    monkeypatch.delenv("AIOPS_COMMUNITY_KEY", raising=False)
    assert aiops.main([]) == 0
    assert "not set" in capsys.readouterr().err


def test_main_returns_0_when_key_empty(monkeypatch, capsys):
    monkeypatch.setenv("AIOPS_COMMUNITY_KEY", "")
    assert aiops.main([]) == 0
    assert "empty" in capsys.readouterr().err
