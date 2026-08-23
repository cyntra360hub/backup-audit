"""Publishes backup-audit findings to AiOps Community
(https://aiopscommunity.com) and joins relevant discussions there.

This is a second, separate integration from the AiOps Enabler workflow
step documented in the README -- that one reports every run's outcome
as a signed event and lives entirely as a copyable step in
`.github/workflows/scheduled.yml` so this package stays untouched by
it. This integration is different in kind: AiOps Community expects
prose articles built from real numbers, has to remember what it
already published across ephemeral CI runs, and has to read its own
inbox (discussion replies, joinable threads) -- state and judgment
that don't fit in a shell one-liner, so it lives here as real code
instead. See README "Optional: AiOps Community publishing".

Every article and comment is built directly from this agent's own
`AuditResult` (see `_build_article`, `_build_comment`) -- never an
invented shape. Nothing here runs unless `AIOPS_COMMUNITY_KEY` is set;
see `main`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup_audit.audit import AuditResult, run_audit
from backup_audit.config import load_config
from backup_audit.models import Status

BASE_URL = "https://aiopscommunity.com/api/v1"

# This repo's own slug, as registered at https://aiopscommunity.com --
# used to skip our own articles when looking for discussions to join.
AGENT_SLUG = "backup-audit"

# Must match a name from GET /api/v1/categories exactly.
CATEGORY = "Observability"

# Words that mark a discussion as something backup-audit could
# meaningfully add to. Deliberately narrow -- this agent only knows
# about backup existence/freshness, not AIOps in general, and
# commenting outside that produces exactly the generic filler the
# moderator rejects.
RELEVANT_TERMS = [
    "backup", "backups", "restore", "disaster recovery", "dr drill",
    "snapshot", "retention", "freshness", "stale data", "durability",
    "replication lag", "runbook", "rpo", "rto", "verification",
    "integrity check", "release artifact",
]

HEARTBEAT_MIN_HOURS = 4.0
HEARTBEAT_MAX_HOURS = 6.0
DISCUSSION_QUOTA_WINDOW = timedelta(hours=24)

# Where we remember what we've already published/commented on and when
# our next heartbeat is due. Relative to the current working directory
# (the workflow runs `backup-audit-publish` from the repo checkout) so
# a GitHub Actions cache step can restore/save it by path across the
# ephemeral runners this runs on -- see .github/workflows/scheduled.yml
# and README "Optional: AiOps Community publishing". Never commit this
# file -- see .gitignore.
STATE_DIR = Path(os.environ.get("AIOPS_COMMUNITY_STATE_DIR", "state"))
STATE_FILE = STATE_DIR / "aiops_community.json"

Transport = Callable[..., tuple[int, dict | None, dict[str, str]]]


def http_request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict | None, dict[str, str]]:
    """Real HTTP transport. Returns (status_code, parsed_json_body,
    response_headers) uniformly whether the call succeeded or failed --
    callers branch on status_code, never on exceptions, so 422/429/503
    are handled the same way for every endpoint."""
    url = f"{BASE_URL}{path}"
    data = None
    headers = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            body = json.loads(raw) if raw else None
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        return exc.code, body, dict(exc.headers or {})


# ------------------------------------------------------------------
# State
# ------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    state.setdefault("published", [])
    state.setdefault("rejected", {})
    state.setdefault("commented_on", {})
    state.setdefault("next_heartbeat_at", None)
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _seconds_since(iso_timestamp: str) -> float:
    then = datetime.fromisoformat(iso_timestamp)
    return (datetime.now(timezone.utc) - then).total_seconds()


# ------------------------------------------------------------------
# Building article/comment text from a real AuditResult
# ------------------------------------------------------------------


def _finding_id(result: AuditResult) -> str | None:
    """A stable id for the *content* of the current finding, not just
    this run -- so two runs in a row that find the exact same missing
    or stale targets are treated as the same finding (skip, don't
    duplicate), while a finding that changes (a target recovers, a new
    one goes stale) gets a new id and can be published."""
    if result.technical_summary is None:
        return None
    return hashlib.sha256(result.technical_summary.encode("utf-8")).hexdigest()[:16]


def _build_article(result: AuditResult) -> tuple[str, str] | None:
    issues = [r for r in result.results if r.status in (Status.MISSING, Status.STALE)]
    if not issues:
        return None

    total = len(result.results)
    lead = issues[0]
    title = f"{len(issues)} of {total} backup targets flagged: {lead.target.name} is {lead.status.value}"[:140]

    paragraphs = [
        "backup-audit is a deterministic checker (no LLM calls) that verifies "
        f"configured backup artifacts exist and are fresh. This scheduled run "
        f"checked {total} target(s) -- GitHub release timestamps, HTTP "
        "Last-Modified headers, or local file mtimes, each compared against a "
        f"per-target freshness threshold -- and flagged {len(issues)}."
    ]
    for r in issues:
        age = f"{r.age_hours:.1f} hours old" if r.age_hours is not None else "age unknown"
        paragraphs.append(
            f"{r.target.name} ({r.target.kind.value}, {r.target.freshness_hours:.0f}h "
            f"freshness threshold): {r.status.value} -- {r.detail} ({age})."
        )
    healthy = total - len(issues)
    if healthy:
        paragraphs.append(f"The remaining {healthy} target(s) checked out present and fresh.")

    return title, "\n\n".join(paragraphs)


def _build_comment(result: AuditResult) -> str:
    """Only called when there's a material finding (see join_discussion)
    -- always grounded in this run's real counts, never a canned
    "all healthy" line, so it isn't near-identical across every run and
    doesn't read as filler on an article it's replying to."""
    total = len(result.results)
    missing = sum(1 for r in result.results if r.status == Status.MISSING)
    stale = sum(1 for r in result.results if r.status == Status.STALE)
    return (
        "Data point from our own backup-audit agent: of "
        f"{total} backup targets we check on a schedule (GitHub release "
        "timestamps, HTTP Last-Modified headers, and local file mtimes, each "
        f"against a per-target freshness threshold), this run found {missing} "
        f"missing and {stale} stale -- {result.technical_summary}."
    )


# ------------------------------------------------------------------
# Publishing
# ------------------------------------------------------------------


def quota_remaining(api_key: str, *, transport: Transport = http_request) -> tuple[int, int]:
    status, body, _ = transport("GET", "/agents/me", api_key=api_key)
    if status != 200 or body is None:
        raise RuntimeError(f"GET /agents/me failed: {status} {body}")
    return body["posts_per_day"] - body["posts_used_today"], body["posts_per_day"]


def publish(
    result: AuditResult,
    api_key: str,
    state: dict,
    *,
    transport: Transport = http_request,
    dry_run: bool = False,
) -> None:
    finding_id = _finding_id(result)
    if finding_id is None:
        print("No material finding this run -- nothing to publish.")
        return
    if finding_id in state["published"]:
        print(f"Finding {finding_id} already published -- skipping (unchanged since last publish).")
        return
    if finding_id in state["rejected"]:
        print(
            f"Finding {finding_id} was rejected last time "
            f"({state['rejected'][finding_id]}) -- not resubmitting the same text."
        )
        return

    title, body = _build_article(result)

    if dry_run:
        print("[dry-run] would POST /api/v1/agents/posts")
        print(f"  finding_id: {finding_id}")
        print(f"  category:   {CATEGORY}")
        print(f"  title:      {title}")
        print(f"  body:\n{body}")
        return

    remaining, per_day = quota_remaining(api_key, transport=transport)
    if remaining <= 0:
        print(f"Quota spent for today (0/{per_day} remaining) -- skipping publish.")
        return

    status, resp, _headers = transport(
        "POST",
        "/agents/posts",
        api_key=api_key,
        json_body={"title": title, "body": body, "category": CATEGORY},
    )

    if status == 201:
        print(f"Published: {(resp or {}).get('url')}")
        state["published"].append(finding_id)
        save_state(state)
    elif status == 422:
        reason = (resp or {}).get("reason") or (resp or {}).get("reason_code") or "unknown"
        print(f"Rejected (422): {reason}")
        state["rejected"][finding_id] = reason
        save_state(state)
    elif status == 429:
        print("Quota spent for today (429) -- retry tomorrow.")
    elif status == 503:
        print("AiOps Community moderator unavailable (503) -- will retry next scheduled run.")
    else:
        print(f"Unexpected response publishing: {status} {resp}")


# ------------------------------------------------------------------
# Discussions
# ------------------------------------------------------------------


def _candidate_articles(state: dict, *, transport: Transport = http_request, limit: int = 20) -> list[dict]:
    status, body, _ = transport("GET", f"/posts?limit={limit}")
    if status != 200 or not body:
        return []

    candidates = []
    for article in body.get("data", []):
        if article.get("agent") == AGENT_SLUG:
            continue
        last = state["commented_on"].get(str(article["id"]))
        if last is not None and _seconds_since(last) < DISCUSSION_QUOTA_WINDOW.total_seconds():
            continue
        haystack = f"{article.get('title', '')} {article.get('excerpt', '')}".lower()
        if any(term in haystack for term in RELEVANT_TERMS):
            candidates.append(article)
    return candidates


def _discussion_target(state: dict, *, transport: Transport = http_request) -> dict | None:
    """One place to add a discussion entry this run: the live edge of an
    existing thread on a relevant article, or -- if none of our
    candidates have any discussion yet -- a fresh top-level entry on
    the first one."""
    fallback = None
    for article in _candidate_articles(state, transport=transport):
        status, body, _ = transport("GET", f"/posts/{article['slug']}")
        if status != 200 or not body:
            continue
        entries = body.get("discussion") or []
        if not entries:
            if fallback is None:
                fallback = {"post_id": article["id"], "slug": article["slug"], "reply_to": None}
            continue
        latest = max(entries, key=lambda e: e["created_at"])
        return {"post_id": article["id"], "slug": article["slug"], "reply_to": latest["id"]}
    return fallback


def join_discussion(
    result: AuditResult,
    api_key: str,
    state: dict,
    *,
    transport: Transport = http_request,
    dry_run: bool = False,
) -> None:
    if result.findings_summary is None:
        # Everything's healthy -- "all good" isn't a concrete
        # contribution to someone else's discussion, so sit this one
        # out rather than post filler.
        print("No material finding this run -- nothing concrete to add to a discussion.")
        return

    target = _discussion_target(state, transport=transport)
    if target is None:
        print("No relevant open discussion to join this run.")
        return

    body_text = _build_comment(result)

    if dry_run:
        print("[dry-run] would POST /api/v1/agents/comments")
        print(f"  post_id:  {target['post_id']} (slug={target['slug']})")
        print(f"  reply_to: {target['reply_to']}")
        print(f"  body:     {body_text}")
        return

    payload = {"post_id": target["post_id"], "body": body_text}
    if target["reply_to"] is not None:
        payload["reply_to"] = target["reply_to"]

    status, resp, headers = transport("POST", "/agents/comments", api_key=api_key, json_body=payload)

    if status == 201:
        print(f"Commented on {target['slug']}: {(resp or {}).get('url')}")
        state["commented_on"][str(target["post_id"])] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    elif status == 422:
        reason = (resp or {}).get("reason") or (resp or {}).get("reason_code") or "unknown"
        print(f"Discussion entry rejected (422): {reason}")
    elif status == 429:
        reason_code = (resp or {}).get("reason_code", "rate_limited")
        retry_after = headers.get("Retry-After")
        print(f"{reason_code} -- retry after {retry_after}s")
        if reason_code == "discussion_quota_spent":
            state["commented_on"][str(target["post_id"])] = datetime.now(timezone.utc).isoformat()
            save_state(state)
    elif status == 503:
        print("AiOps Community unavailable (503) -- will retry next scheduled run.")
    else:
        print(f"Unexpected response commenting: {status} {resp}")


# ------------------------------------------------------------------
# Heartbeat
# ------------------------------------------------------------------


def heartbeat(
    api_key: str,
    state: dict,
    *,
    transport: Transport = http_request,
    dry_run: bool = False,
) -> None:
    """GET /api/v1/home on a randomised 4-6 hour cadence (agents.md
    section 4) so this agent sees replies on its own articles and
    discussions worth joining. Runs at most once per due interval no
    matter how often the caller invokes this (this workflow runs every
    30 minutes) -- state tracks when the next check is actually due."""
    next_at = state.get("next_heartbeat_at")
    now = datetime.now(timezone.utc)
    if next_at is not None and now < datetime.fromisoformat(next_at):
        return

    if dry_run:
        print("[dry-run] would GET /api/v1/home (heartbeat)")
        return

    status, body, _ = transport("GET", "/home", api_key=api_key)
    if status == 200 and body:
        account = body.get("your_account", {})
        print(
            f"Heartbeat: {account.get('posts_used_today')}/{account.get('posts_per_day')} "
            f"posts used today, claim_status={account.get('claim_status')}"
        )
        for item in body.get("activity_on_your_articles", []):
            print(f"  reply on {item.get('article_slug')} from {item.get('commenter')}: {item.get('excerpt')}")
        for item in body.get("discussions_you_could_join", []):
            print(f"  open discussion on {item.get('article_slug')} ({item.get('entry_count')} entries)")
        for line in body.get("what_to_do_next", []):
            print(f"  next: {line}")
    else:
        print(f"Heartbeat failed: {status} {body}")

    interval_hours = random.uniform(HEARTBEAT_MIN_HOURS, HEARTBEAT_MAX_HOURS)
    state["next_heartbeat_at"] = (now + timedelta(hours=interval_hours)).isoformat()
    save_state(state)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------


def run(
    result: AuditResult,
    api_key: str,
    *,
    transport: Transport = http_request,
    dry_run: bool = False,
) -> None:
    state = load_state()
    heartbeat(api_key, state, transport=transport, dry_run=dry_run)
    publish(result, api_key, state, transport=transport, dry_run=dry_run)
    join_discussion(result, api_key, state, transport=transport, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish backup-audit findings to AiOps Community.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be submitted without calling the API or writing state.",
    )
    args = parser.parse_args(argv)

    try:
        api_key = os.environ["AIOPS_COMMUNITY_KEY"]
    except KeyError:
        print("AIOPS_COMMUNITY_KEY is not set -- nothing to publish.", file=sys.stderr)
        return 0
    if not api_key:
        # A repo secret referenced in a workflow's `env:` before it has
        # been added comes through as an empty string, not a missing
        # key -- treat it the same as unset rather than sending an
        # empty bearer token.
        print("AIOPS_COMMUNITY_KEY is empty -- nothing to publish.", file=sys.stderr)
        return 0

    result = run_audit(load_config())
    run(result, api_key, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
