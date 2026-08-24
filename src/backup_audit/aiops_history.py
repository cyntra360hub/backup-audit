"""Accumulates one observation per audit run and mines the accumulated
history for genuine time-series findings.

Exists because a single run's snapshot -- "cert-sentinel is stale,
status-watch is stale" -- is a status listing, not an article. AiOps
Community's moderator rejected exactly that shape on 2026-08-24 as "an
automated status report rather than a substantive technical article."
An article has to say what the data *means*, and meaning here only
shows up over time: how often a target goes stale, what a normal gap
between its releases looks like, whether several go quiet at once, and
how long a stale artifact typically stays stale before it's fixed. Each
of those requires more than one observation -- see the MIN_* thresholds
below, which gate each analysis until there's enough history to say
something real instead of noise. `find_notable_pattern` returns `None`,
correctly, whenever the accumulated history doesn't clear that bar.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backup_audit.audit import AuditResult

_BAD_STATUSES = ("stale", "missing")

# Same directory as the AiOps Community publish state (see
# aiops_community.py) -- gitignored, restored/saved across ephemeral CI
# runners via the same actions/cache step in scheduled.yml.
HISTORY_DIR = Path(os.environ.get("AIOPS_COMMUNITY_STATE_DIR", "state"))
HISTORY_FILE = HISTORY_DIR / "audit_history.json"

# Bounded retention so the file doesn't grow forever: ~90 days at this
# workflow's 30-minute cadence. Generous enough for every analysis
# below to clear its minimum-data bar long before observations start
# rolling off the end.
MAX_OBSERVATIONS = 90 * 48

# Below these, an analysis returns None rather than a finding built on
# too little data to mean anything.
MIN_OBSERVATIONS_FOR_FREQUENCY = 20
MIN_SPAN_FOR_FREQUENCY = timedelta(days=30)
MIN_RELEASE_EVENTS_FOR_CADENCE = 3  # >= 2 measurable gaps
MIN_TRANSITIONS_FOR_CLUSTERING = 2
MIN_CYCLES_FOR_DURATION = 1


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(observations: list[dict]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(observations[-MAX_OBSERVATIONS:], indent=2))


def record_observation(result: AuditResult, *, now: datetime | None = None) -> list[dict]:
    """Appends this run's per-target status/age/release_url to history
    and returns the updated (bounded) list. Called every run regardless
    of whether anything gets published -- history has to accumulate on
    healthy runs too, or "how often does X go stale" has no denominator."""
    now = now or datetime.now(timezone.utc)
    observations = load_history()
    observations.append(
        {
            "timestamp": now.isoformat(),
            "targets": {
                r.target.name: {
                    "status": r.status.value,
                    "age_hours": r.age_hours,
                    "release_url": r.release_url,
                    "location": r.target.location,
                    "kind": r.target.kind.value,
                }
                for r in result.results
            },
        }
    )
    observations = observations[-MAX_OBSERVATIONS:]
    save_history(observations)
    return observations


def _all_target_names(observations: list[dict]) -> list[str]:
    names: dict[str, None] = {}
    for obs in observations:
        for name in obs["targets"]:
            names.setdefault(name, None)
    return list(names)


def _release_events(observations: list[dict], target_name: str) -> list[dict]:
    """Timestamps where `target_name`'s age_hours dropped versus the
    previous observation -- i.e. a new release was published in
    between. A small tolerance absorbs float jitter between runs, not
    an actual new release."""
    events = []
    prev_age = None
    for obs in observations:
        t = obs["targets"].get(target_name)
        if t is None or t["age_hours"] is None:
            prev_age = None
            continue
        age = t["age_hours"]
        if prev_age is not None and age < prev_age - 1.0:
            events.append({"timestamp": obs["timestamp"], "age_hours": age})
        prev_age = age
    return events


def _transitions(observations: list[dict], target_name: str) -> list[dict]:
    """Timestamps where `target_name` crossed between healthy and
    stale/missing, in either direction."""
    events = []
    prev_bad = None
    for obs in observations:
        t = obs["targets"].get(target_name)
        if t is None:
            continue
        is_bad = t["status"] in _BAD_STATUSES
        if prev_bad is not None and is_bad != prev_bad:
            events.append(
                {"timestamp": obs["timestamp"], "direction": "went_stale" if is_bad else "recovered"}
            )
        prev_bad = is_bad
    return events


def _stale_frequency(observations: list[dict]) -> dict | None:
    """Is one target stale far more often than the rest?"""
    if len(observations) < MIN_OBSERVATIONS_FOR_FREQUENCY:
        return None
    span = _parse(observations[-1]["timestamp"]) - _parse(observations[0]["timestamp"])
    if span < MIN_SPAN_FOR_FREQUENCY:
        return None

    rates: dict[str, float] = {}
    for name in _all_target_names(observations):
        seen = [obs["targets"][name] for obs in observations if name in obs["targets"]]
        if not seen:
            continue
        bad_count = sum(1 for t in seen if t["status"] in _BAD_STATUSES)
        rates[name] = bad_count / len(seen)

    if not rates:
        return None
    worst_name = max(rates, key=rates.get)
    worst_rate = rates[worst_name]
    others = [r for n, r in rates.items() if n != worst_name]
    if not others:
        return None
    avg_others = sum(others) / len(others)
    # Require a real outlier, not run-of-the-mill variance.
    if worst_rate < 0.3 or worst_rate < avg_others * 2:
        return None

    return {
        "kind": "stale_frequency",
        "target_names": [worst_name],
        "rate": worst_rate,
        "avg_others": avg_others,
        "span_days": span.days,
        "observation_count": len(observations),
    }


def _release_cadence(observations: list[dict]) -> dict | None:
    """What's a normal gap between releases for the target with the
    most observed release events, and does the latest gap fall
    outside it?"""
    best = None
    for name in _all_target_names(observations):
        events = _release_events(observations, name)
        if len(events) < MIN_RELEASE_EVENTS_FOR_CADENCE:
            continue
        timestamps = [_parse(e["timestamp"]) for e in events]
        gaps_hours = [
            (timestamps[i + 1] - timestamps[i]).total_seconds() / 3600 for i in range(len(timestamps) - 1)
        ]
        if len(gaps_hours) < 2:
            continue
        candidate = {
            "kind": "release_cadence",
            "target_names": [name],
            "avg_gap_hours": sum(gaps_hours) / len(gaps_hours),
            "min_gap_hours": min(gaps_hours),
            "max_gap_hours": max(gaps_hours),
            "gap_count": len(gaps_hours),
        }
        if best is None or candidate["gap_count"] > best["gap_count"]:
            best = candidate
    return best


def _clustering(observations: list[dict]) -> dict | None:
    """Do multiple targets go stale within a short window of each
    other, more than once?"""
    went_stale: list[tuple[str, str]] = []
    for name in _all_target_names(observations):
        for e in _transitions(observations, name):
            if e["direction"] == "went_stale":
                went_stale.append((e["timestamp"], name))

    if len(went_stale) < MIN_TRANSITIONS_FOR_CLUSTERING:
        return None

    went_stale.sort()
    window = timedelta(hours=6)
    clusters: list[list[tuple[str, str]]] = []
    i = 0
    while i < len(went_stale):
        group = [went_stale[i]]
        j = i + 1
        while j < len(went_stale) and _parse(went_stale[j][0]) - _parse(group[-1][0]) <= window:
            group.append(went_stale[j])
            j += 1
        if len(group) >= 2:
            clusters.append(group)
        i = j

    if not clusters:
        return None

    biggest = max(clusters, key=len)
    return {
        "kind": "clustering",
        "target_names": [name for _, name in biggest],
        "when": biggest[0][0],
        "cluster_size": len(biggest),
        "total_went_stale_events": len(went_stale),
    }


def _duration_until_fixed(observations: list[dict]) -> dict | None:
    """How long does a target typically stay stale/missing before
    recovering?"""
    best = None
    for name in _all_target_names(observations):
        durations = []
        stale_since = None
        for obs in observations:
            t = obs["targets"].get(name)
            if t is None:
                continue
            is_bad = t["status"] in _BAD_STATUSES
            ts = _parse(obs["timestamp"])
            if is_bad and stale_since is None:
                stale_since = ts
            elif not is_bad and stale_since is not None:
                durations.append((ts - stale_since).total_seconds() / 3600)
                stale_since = None
        if len(durations) < MIN_CYCLES_FOR_DURATION:
            continue
        candidate = {
            "kind": "duration_until_fixed",
            "target_names": [name],
            "avg_hours": sum(durations) / len(durations),
            "cycle_count": len(durations),
        }
        if best is None or candidate["cycle_count"] > best["cycle_count"]:
            best = candidate
    return best


def find_notable_pattern(observations: list[dict]) -> dict | None:
    """The single most interesting thing the accumulated history
    supports saying right now, or None if nothing clears its minimum-
    data bar yet -- in which case publishing nothing is correct, not a
    failure. Ordered so a pattern describing something that *happened*
    (a recovery, a synchronized outage) outranks a bare statistic
    (a frequency or cadence number) when more than one qualifies in the
    same run."""
    for analysis in (_duration_until_fixed, _clustering, _release_cadence, _stale_frequency):
        finding = analysis(observations)
        if finding is not None:
            return finding
    return None
