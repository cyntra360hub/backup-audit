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
# Building finding text
# ------------------------------------------------------------------


def test_finding_id_none_when_healthy():
    assert aiops._finding_id(_healthy_result()) is None


def test_finding_id_stable_for_same_content():
    a = aiops._finding_id(_material_result())
    b = aiops._finding_id(_material_result())
    assert a == b


def test_finding_id_changes_with_content():
    other = AuditResult(results=(CheckResult(_target("different"), Status.MISSING, "no releases published"),))
    assert aiops._finding_id(_material_result()) != aiops._finding_id(other)


def test_build_article_none_when_healthy():
    assert aiops._build_article(_healthy_result()) is None


def test_build_article_has_real_numbers():
    title, body, source_url = aiops._build_article(_material_result())
    assert 10 <= len(title) <= 140
    assert "missing-one" in title or "missing-one" in body
    assert "999.5 hours old" in body
    assert "no releases published" in body
    assert len(body) >= 200
    assert "https://github.com/o/r/releases" not in body  # never in body -- would be stripped anyway


def test_build_article_source_url_prefers_missing_over_stale():
    # Missing outranks stale for the single-URL-per-article citation
    # (agents.md source_url is one URL only) -- a missing backup is a
    # worse finding than a stale one, so it's the one cited. Neither
    # CheckResult in _material_result() carries a release_url, so this
    # also exercises the no-release-object fallback.
    _, _, source_url = aiops._build_article(_material_result())
    assert source_url == "https://github.com/o/r/releases"


def test_build_article_source_url_uses_release_url_directly():
    # The normal case: checkers.py already captured the release's own
    # html_url (a direct .../releases/tag/{tag} page, never a redirect)
    # -- use it as-is rather than constructing anything.
    result = AuditResult(
        results=(
            CheckResult(
                _target("stale-one"),
                Status.STALE,
                "release v1",
                age_hours=999.5,
                release_url="https://github.com/o/r/releases/tag/v1.0.0",
            ),
        )
    )
    _, _, source_url = aiops._build_article(result)
    assert source_url == "https://github.com/o/r/releases/tag/v1.0.0"


def test_build_article_source_url_falls_back_when_no_release_object():
    # A true "no releases published" 404 -- no release object, so no
    # html_url to capture. The plain releases index is still a direct
    # 200 (verified against github.com), not a redirect.
    result = AuditResult(
        results=(CheckResult(_target("stale-one"), Status.STALE, "release v1", age_hours=999.5),)
    )
    _, _, source_url = aiops._build_article(result)
    assert source_url == "https://github.com/o/r/releases"


def test_build_article_source_url_none_for_non_github_targets():
    file_target = Target(name="local", kind=TargetKind.FILE, location="/backups/x.tar.gz", freshness_hours=24)
    result = AuditResult(results=(CheckResult(file_target, Status.MISSING, "file does not exist"),))
    _, _, source_url = aiops._build_article(result)
    assert source_url is None


def test_build_comment_has_counts():
    comment = aiops._build_comment(_material_result())
    assert "1 missing and 1 stale" in comment


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


def test_publish_skips_when_no_finding():
    transport = FakeTransport([])
    aiops.publish(_healthy_result(), "key", aiops.load_state(), transport=transport)
    assert transport.calls == []


def test_publish_dry_run_makes_no_calls():
    transport = FakeTransport([])
    aiops.publish(_material_result(), "key", aiops.load_state(), transport=transport, dry_run=True)
    assert transport.calls == []


def test_publish_success_records_state():
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/x"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(_material_result(), "key", state, transport=transport)
    finding_id = aiops._finding_id(_material_result())
    assert finding_id in state["published"]
    assert aiops.load_state()["published"] == [finding_id]

    post_call = transport.calls[-1]
    assert post_call["json_body"]["source_url"] == "https://github.com/o/r/releases"


def test_publish_omits_source_url_when_not_applicable():
    file_target = Target(name="local", kind=TargetKind.FILE, location="/backups/x.tar.gz", freshness_hours=24)
    result = AuditResult(results=(CheckResult(file_target, Status.MISSING, "file does not exist"),))
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/x"}, {}),
        ]
    )
    aiops.publish(result, "key", aiops.load_state(), transport=transport)
    post_call = transport.calls[-1]
    assert "source_url" not in post_call["json_body"]


def test_publish_skips_when_already_published():
    state = aiops.load_state()
    state["published"].append(aiops._finding_id(_material_result()))
    transport = FakeTransport([])
    aiops.publish(_material_result(), "key", state, transport=transport)
    assert transport.calls == []


def test_publish_422_records_rejection_and_does_not_retry():
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (422, {"reason": "too_vague"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(_material_result(), "key", state, transport=transport)
    finding_id = aiops._finding_id(_material_result())
    assert state["rejected"][finding_id] == "too_vague"
    assert finding_id not in state["published"]

    # A second run with the same finding must not resubmit.
    transport2 = FakeTransport([])
    aiops.publish(_material_result(), "key", aiops.load_state(), transport=transport2)
    assert transport2.calls == []


def test_publish_skips_when_quota_spent():
    transport = FakeTransport([(200, {"posts_per_day": 2, "posts_used_today": 2}, {})])
    state = aiops.load_state()
    aiops.publish(_material_result(), "key", state, transport=transport)
    assert state["published"] == []
    assert len(transport.calls) == 1  # only the quota check, no POST


def test_publish_503_does_not_record_anything():
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (503, None, {}),
        ]
    )
    state = aiops.load_state()
    aiops.publish(_material_result(), "key", state, transport=transport)
    assert state["published"] == []
    assert state["rejected"] == {}


def test_publish_logs_full_response_body_on_any_non_201(capsys):
    # Added after the 2026-08-24 outage: the code used to print a fixed
    # message on 503 with no visibility into the actual response body.
    # Whatever the transport returns must now show up in the output.
    transport = FakeTransport(
        [
            (200, {"posts_per_day": 2, "posts_used_today": 0}, {}),
            (503, {"_raw_body": "<html>upstream error</html>"}, {}),
        ]
    )
    aiops.publish(_material_result(), "key", aiops.load_state(), transport=transport)
    out = capsys.readouterr().out
    assert "503" in out
    assert "upstream error" in out


# ------------------------------------------------------------------
# join_discussion()
# ------------------------------------------------------------------


def test_join_discussion_skips_when_healthy():
    transport = FakeTransport([])
    aiops.join_discussion(_healthy_result(), "key", aiops.load_state(), transport=transport)
    assert transport.calls == []


def test_join_discussion_replies_to_live_edge_of_thread():
    transport = FakeTransport(
        [
            (200, {"data": [{"id": 1, "slug": "backup-drills", "title": "Backup drills", "excerpt": "", "agent": "other"}]}, {}),
            (200, {"discussion": [{"id": 55, "thread_root": 55, "created_at": "2026-01-01T00:00:00+00:00", "depth": 0}]}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/backup-drills#c2"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.join_discussion(_material_result(), "key", state, transport=transport)
    assert str(1) in state["commented_on"]
    post_call = transport.calls[-1]
    assert post_call["json_body"]["reply_to"] == 55
    assert post_call["json_body"]["post_id"] == 1


def test_join_discussion_falls_back_to_top_level_when_no_thread_yet():
    transport = FakeTransport(
        [
            (200, {"data": [{"id": 2, "slug": "dr-basics", "title": "Disaster recovery basics", "excerpt": "", "agent": "other"}]}, {}),
            (200, {"discussion": []}, {}),
            (201, {"url": "https://aiopscommunity.com/posts/dr-basics#c1"}, {}),
        ]
    )
    state = aiops.load_state()
    aiops.join_discussion(_material_result(), "key", state, transport=transport)
    post_call = transport.calls[-1]
    assert "reply_to" not in post_call["json_body"]


def test_join_discussion_respects_24h_cooldown():
    state = aiops.load_state()
    state["commented_on"]["1"] = datetime.now(timezone.utc).isoformat()
    transport = FakeTransport([(200, {"data": [{"id": 1, "slug": "x", "title": "backup drift", "excerpt": "", "agent": "other"}]}, {})])
    aiops.join_discussion(_material_result(), "key", state, transport=transport)
    # Only the listing call happens; the article is filtered out before
    # its detail page (and definitely before posting) is ever fetched.
    assert len(transport.calls) == 1


def test_join_discussion_skips_own_articles():
    transport = FakeTransport([(200, {"data": [{"id": 9, "slug": "x", "title": "backup drift", "excerpt": "", "agent": aiops.AGENT_SLUG}]}, {})])
    aiops.join_discussion(_material_result(), "key", aiops.load_state(), transport=transport)
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
