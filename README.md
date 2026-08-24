# backup-audit

A small, deterministic Python agent that verifies configured backup
artifacts **exist and are fresh**, flagging stale or missing ones — no
LLM calls, no paid APIs, no server to run.

## What it does

For each configured target, backup-audit checks existence and — where
the source provides a timestamp — freshness against a per-target
threshold:

- **`github_release`**: the target repo's latest GitHub release must
  exist and have at least one asset; freshness is `published_at`.
- **`url`**: a `HEAD` request must return non-404; freshness uses the
  `Last-Modified` response header if the server sends one (if it
  doesn't, the target is reported as existing with "freshness unknown"
  rather than guessed).
- **`file`**: a local path must exist; freshness is its mtime.

Default targets: the **latest GitHub release of each of the other four
agents in this project** (cert-sentinel, status-watch, ci-triage,
alert-dedupe) — this repo doubles as a live demonstration that its
siblings' release artifacts stay present and fresh. Fully replaceable —
see "Configuring targets" below.

## Install

Requires Python 3.12+.

```bash
pip install .
```

## Usage

```bash
backup-audit
```

Or as a module:

```bash
python -m backup_audit.cli
```

Exits non-zero if any target is missing or errored (not merely stale) —
usable directly as a CI/cron failure signal.

### Configuring targets

Set `BACKUP_AUDIT_TARGETS` to a JSON array to fully replace the default
four:

```json
[
  {"name": "my backup", "kind": "url", "location": "https://example.com/backup.tar.gz", "freshness_hours": 24},
  {"name": "my release", "kind": "github_release", "location": "myorg/myrepo"},
  {"name": "local snapshot", "kind": "file", "location": "/var/backups/latest.tar.gz", "freshness_hours": 6}
]
```

`freshness_hours` defaults to `720` (30 days) if omitted.

Copy `.env.example` to `.env` to set this locally; `.env` is gitignored
and never committed.

## Optional: AiOps Enabler integration

Unlike its sibling repos, **this package contains zero AiOps Enabler
code** — no `signing.py`, no `reporting.py`, nothing HMAC-related
anywhere under `src/`. Reporting is implemented entirely as a **copyable
GitHub Actions workflow step**, in
[`.github/workflows/scheduled.yml`](.github/workflows/scheduled.yml):
a self-contained inline Python script (standard library only) that
implements the exact signing scheme from
[skill.md](https://aiopsenabler.com/skill.md) section 3, reads this
tool's `Overall: outcome=...`, `Findings: ...`, and
`Findings-technical: ...` lines from the previous step's output, and
POSTs a signed `task_started`/`task_completed` event pair.

`outcome` is `success` whenever the audit actually ran — **including**
when it finds a stale or missing backup, since detecting that is this
agent doing its job, not a failure. `outcome` is `failure` only when a
check itself couldn't run (`backup_audit.audit.AuditResult.has_errors`
— see `checkers.py`). Any STALE/MISSING findings are printed by the CLI
as two lines: a short, human-readable `Findings: ...` line (naming only
one example target plus a count, e.g. `"found 1 backup issue across 3
targets -- e.g. nightly-db (missing)"`), forwarded into the reported
event's `details` field — what actually renders on the agent's public
pulse/profile activity — and a fuller `Findings-technical: ...` line
(every missing/stale target by name), forwarded into the legacy
`external_ref` field.

**Why this shape:** it's meant to be lifted wholesale into *any* other
workflow, for *any* other tool, in *any* language — the tool under audit
doesn't need to know AiOps Enabler exists, doesn't need a dependency
added, and doesn't need a single line of its own source touched.
Copy the "Report to AiOps Enabler" step, point `TASK_OUTCOME`/
`TASK_FINDINGS`/`TASK_FINDINGS_TECHNICAL` at whatever your own tool
prints, and set two repo secrets:

```
BACKUP_AUDIT_AGENT_KEY_ID=ak_...
BACKUP_AUDIT_AGENT_SECRET=...
```

This is opt-in and off by default in the sense that it only runs in this
repo's own scheduled/dispatched Actions workflow, never from a local
`backup-audit` invocation — there is no code path in this package that
could phone home even if you wanted it to.

## Optional: AiOps Community publishing

This repo is registered on [AiOps Community](https://aiopscommunity.com)
as `backup-audit`, per [agents.md](https://aiopscommunity.com/agents.md).
Unlike the AiOps Enabler integration above, this one *is* real code --
[`src/backup_audit/aiops_community.py`](src/backup_audit/aiops_community.py)
and [`src/backup_audit/aiops_history.py`](src/backup_audit/aiops_history.py)
-- because it needs more than a stateless shell step: it has to
accumulate observations across runs, remember what it already
published, and read its own inbox for replies and discussions worth
joining.

**Articles are never built from a single run's snapshot.** "cert-sentinel
is stale, status-watch is stale" is a status listing, not an article --
the moderator rejected exactly that shape (2026-08-24) as "an automated
status report rather than a substantive technical article." Every run
appends its per-target observation to a bounded history file
(`aiops_history.record_observation`), and `aiops_history.find_notable_
pattern` mines the accumulated history for one of four things a single
snapshot can't express, each gated on its own minimum-data threshold so
"not enough history yet" -- and therefore publishing nothing -- is the
correct default, not a fallback:

- **Stale-frequency outlier** -- one target stale far more often than
  its peers, over enough checks and wall-clock span to mean something.
- **Release cadence** -- the normal gap between a target's releases,
  from at least two observed gaps.
- **Simultaneous-staleness clustering** -- multiple independently
  monitored targets going stale within the same short window, pointing
  at a shared cause rather than independent drift.
- **Duration until fixed** -- how long a target typically stays stale
  before a new release resolves it, from at least one observed cycle.

What happens each scheduled run (after the audit step above):

- **Record.** This run's per-target status/age/release URL is appended
  to history -- on every run, healthy or not, since frequency and
  cadence need a full denominator, not just the bad runs.
- **Heartbeat.** `GET /api/v1/home` on a randomised 4-6 hour interval
  (state-tracked, not a fixed time) to see replies on this agent's own
  articles and discussions worth joining.
- **Publish.** Only when `find_notable_pattern` returns something --
  never on a raw "target X is currently stale" snapshot. The one target
  a pattern is actually about gets named and its claim gets a
  `source_url` citation (a direct GitHub URL, never a redirect --
  `.../releases/latest` is a 302 and gets rejected; the release's own
  `html_url`, captured in `CheckResult.release_url` by checkers.py, is
  already the direct tag-page form); a target that doesn't need naming
  for the point being made isn't named. `source_url` is one URL per
  article, so a clustering finding across several targets names all of
  them in prose but still cites only the one anchor URL. A hash of the
  pattern's content is tracked in local state so an unchanged
  conclusion is never republished, and a `422` rejection is remembered
  too, so the same rejected text is never resubmitted.
- **Discussions.** Looks for other agents' articles matching backup/DR/
  freshness-adjacent terms and, only when this run *also* has a notable
  pattern, adds one entry grounded in the same real numbers as the
  article -- replying to the live edge of an existing thread if there
  is one, a fresh top-level entry otherwise. Respects the platform's
  one-entry-per-article-per-24-hours quota locally before ever calling
  the API.

Every non-`201` response (`422`, `429`, `503`) is logged with its full
body, not just the status code -- added after a 2026-08-24 run of `503`s
that were undiagnosable from the logs because nothing printed what the
response actually contained.

State (audit history, published/rejected pattern ids, discussion
cooldowns, next heartbeat time) lives in `state/`, gitignored -- see
`.gitignore` -- and persisted across this repo's ephemeral Actions
runners via `actions/cache` in
[`.github/workflows/scheduled.yml`](.github/workflows/scheduled.yml).

Requires one repo secret:

```
AIOPS_COMMUNITY_KEY=<the api_key from this repo's AiOps Community registration>
```

the API key issued when this repo registered on AiOps Community. Read
via `os.environ["AIOPS_COMMUNITY_KEY"]` only -- never given a fallback
value, and never written to a file. If the secret isn't set, the
publish step logs that and exits `0` without touching the network; it
never fails the workflow on that basis alone.

Guarded the same way as the Enabler step: only runs on this repo's own
`schedule` trigger (`github.repository == 'cyntra360hub/backup-audit'`),
never on a fork and never on a manual `workflow_dispatch`.

Try it locally without publishing anything:

```bash
AIOPS_COMMUNITY_KEY=placeholder python -m backup_audit.aiops_community --dry-run
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

All tests run fully offline — every checker's I/O (HTTP fetch,
filesystem stat, current time) is injected, so the suite never touches
the network or the real filesystem outside pytest's own fixtures.

## License

MIT — see [LICENSE](LICENSE).
