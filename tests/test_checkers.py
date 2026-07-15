import json
from datetime import datetime, timedelta, timezone

from backup_audit.checkers import check_file, check_github_release, check_url
from backup_audit.models import Status, Target, TargetKind

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _target(kind: TargetKind, location: str, freshness_hours: float = 720) -> Target:
    return Target(name="t", kind=kind, location=location, freshness_hours=freshness_hours)


def _release_payload(published_at: datetime, assets: list | None = None) -> str:
    return json.dumps(
        {
            "tag_name": "v1.0.0",
            "published_at": published_at.isoformat().replace("+00:00", "Z"),
            "assets": assets if assets is not None else [{"name": "checksums.txt"}],
        }
    )


def test_github_release_fresh():
    target = _target(TargetKind.GITHUB_RELEASE, "o/r", freshness_hours=720)
    fetcher = lambda url, timeout: _release_payload(NOW - timedelta(hours=10))
    result = check_github_release(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.OK
    assert result.age_hours == 10.0


def test_github_release_stale():
    target = _target(TargetKind.GITHUB_RELEASE, "o/r", freshness_hours=24)
    fetcher = lambda url, timeout: _release_payload(NOW - timedelta(hours=100))
    result = check_github_release(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.STALE


def test_github_release_no_assets_is_missing():
    target = _target(TargetKind.GITHUB_RELEASE, "o/r")
    fetcher = lambda url, timeout: _release_payload(NOW, assets=[])
    result = check_github_release(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.MISSING


def test_github_release_404_is_missing():
    import urllib.error

    target = _target(TargetKind.GITHUB_RELEASE, "o/r")

    def fetcher(url, timeout):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    result = check_github_release(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.MISSING


def test_github_release_other_http_error_is_error():
    import urllib.error

    target = _target(TargetKind.GITHUB_RELEASE, "o/r")

    def fetcher(url, timeout):
        raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    result = check_github_release(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.ERROR


def test_url_fresh_via_last_modified():
    target = _target(TargetKind.URL, "https://example.test/f", freshness_hours=720)
    last_modified = (NOW - timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    fetcher = lambda url, timeout: {"Last-Modified": last_modified}
    result = check_url(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.OK


def test_url_stale_via_last_modified():
    target = _target(TargetKind.URL, "https://example.test/f", freshness_hours=24)
    last_modified = (NOW - timedelta(hours=100)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    fetcher = lambda url, timeout: {"Last-Modified": last_modified}
    result = check_url(target, now=NOW, fetcher=fetcher)
    assert result.status == Status.STALE


def test_url_missing_when_404():
    target = _target(TargetKind.URL, "https://example.test/f")
    result = check_url(target, now=NOW, fetcher=lambda url, timeout: None)
    assert result.status == Status.MISSING


def test_url_ok_without_last_modified():
    target = _target(TargetKind.URL, "https://example.test/f")
    result = check_url(target, now=NOW, fetcher=lambda url, timeout: {})
    assert result.status == Status.OK
    assert result.age_hours is None


def test_file_fresh():
    target = _target(TargetKind.FILE, "/some/path", freshness_hours=720)
    mtime = (NOW - timedelta(hours=5)).timestamp()
    result = check_file(target, now=NOW, stater=lambda path: mtime)
    assert result.status == Status.OK


def test_file_stale():
    target = _target(TargetKind.FILE, "/some/path", freshness_hours=24)
    mtime = (NOW - timedelta(hours=100)).timestamp()
    result = check_file(target, now=NOW, stater=lambda path: mtime)
    assert result.status == Status.STALE


def test_file_missing():
    target = _target(TargetKind.FILE, "/some/path")
    result = check_file(target, now=NOW, stater=lambda path: None)
    assert result.status == Status.MISSING
