"""One checker function per `TargetKind`, each returning a `CheckResult`.
Every real I/O call (HTTP fetch, filesystem stat, current time) is
injectable, so the test suite never touches the network or a real clock.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from backup_audit.models import CheckResult, Status, Target

JsonFetcher = Callable[[str, float], str]
HeadFetcher = Callable[[str, float], dict[str, str] | None]
Clock = Callable[[], datetime]
Stater = Callable[[Path], float | None]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def fetch_json(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def fetch_headers(url: str, timeout: float) -> dict[str, str] | None:
    """HEAD request returning response headers, or None for a 404 (any
    other non-2xx re-raises, since that's a genuine error rather than
    "the artifact is absent")."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return dict(response.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def default_stat(path: Path) -> float | None:
    if not path.exists():
        return None
    return path.stat().st_mtime


def _age_status(age_hours: float, freshness_hours: float) -> Status:
    return Status.OK if age_hours <= freshness_hours else Status.STALE


def check_github_release(
    target: Target,
    now: datetime | None = None,
    fetcher: JsonFetcher = fetch_json,
    timeout: float = 15.0,
) -> CheckResult:
    now = now or default_clock()
    url = f"https://api.github.com/repos/{target.location}/releases/latest"
    try:
        raw = fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return CheckResult(target, Status.MISSING, "no releases published")
        return CheckResult(target, Status.ERROR, f"GitHub API error {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(target, Status.ERROR, str(exc))

    release = json.loads(raw)
    if not release.get("assets"):
        return CheckResult(
            target, Status.MISSING, f"release {release.get('tag_name')} has no assets"
        )

    published_at = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
    age_hours = (now - published_at).total_seconds() / 3600
    status = _age_status(age_hours, target.freshness_hours)
    detail = f"release {release.get('tag_name')}, {len(release['assets'])} asset(s)"
    return CheckResult(target, status, detail, age_hours=age_hours)


def check_url(
    target: Target,
    now: datetime | None = None,
    fetcher: HeadFetcher = fetch_headers,
    timeout: float = 15.0,
) -> CheckResult:
    now = now or default_clock()
    try:
        headers = fetcher(target.location, timeout)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(target, Status.ERROR, str(exc))

    if headers is None:
        return CheckResult(target, Status.MISSING, "404 not found")

    last_modified = headers.get("Last-Modified")
    if not last_modified:
        return CheckResult(target, Status.OK, "exists (no Last-Modified header; freshness unknown)")

    modified_at = parsedate_to_datetime(last_modified)
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    age_hours = (now - modified_at).total_seconds() / 3600
    status = _age_status(age_hours, target.freshness_hours)
    return CheckResult(target, status, f"Last-Modified: {last_modified}", age_hours=age_hours)


def check_file(
    target: Target,
    now: datetime | None = None,
    stater: Stater = default_stat,
) -> CheckResult:
    now = now or default_clock()
    try:
        mtime = stater(Path(target.location))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(target, Status.ERROR, str(exc))

    if mtime is None:
        return CheckResult(target, Status.MISSING, "file does not exist")

    modified_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_hours = (now - modified_at).total_seconds() / 3600
    status = _age_status(age_hours, target.freshness_hours)
    return CheckResult(target, status, f"mtime {modified_at.isoformat()}", age_hours=age_hours)
