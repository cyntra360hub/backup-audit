# Contributing to backup-audit

Thanks for considering a contribution! This is a small, focused tool —
keep changes deterministic (no LLM calls, no paid APIs) and offline-testable
(inject fetchers/staters/clocks, don't call real network/filesystem in tests).

## Getting started

```bash
git clone https://github.com/cyntra360hub/backup-audit.git
cd backup-audit
pip install -e ".[dev]"
pytest
```

## Workflow

1. Open an issue first for anything beyond a trivial fix, so we can agree
   on approach before you invest time.
2. Fork, branch, make your change, add/update tests.
3. Run `pytest` — all tests must pass, and new behavior needs new tests.
4. Open a PR describing what changed and why.

## Good first issues

These are scoped to be approachable without deep familiarity with the
codebase:

- **`good-first-issue`: Add an S3-style target kind.** Add `TargetKind.S3`
  and a `check_s3` in `checkers.py` that HEADs a public S3 object URL
  (reusing `check_url`'s Last-Modified logic is fine) with tests using an
  injected fetcher.
- **`good-first-issue`: Add a JSON output mode.** Add a `--json` flag (or
  `BACKUP_AUDIT_OUTPUT=json` env var) to `cli.py` that prints the
  `AuditResult` as machine-readable JSON instead of the human-readable
  report, for piping into other tools.
- **`good-first-issue`: Per-target retry.** `checkers.py`'s HTTP-based
  checks (`check_github_release`, `check_url`) currently fail on the
  first network error. Add a single retry with a short backoff, with
  tests using a fetcher that fails once then succeeds.
- **`good-first-issue`: Minimum-asset-count target option.** Extend
  `Target`/`check_github_release` with an optional `min_assets` field
  (default 1) so a release needing e.g. 3 platform-specific binaries can
  be flagged `MISSING` if fewer are attached, with tests.
- **`good-first-issue`: Port the copyable reporting template to another
  language.** The inline Python in `.github/workflows/scheduled.yml`'s
  "Report to AiOps Enabler" step is meant to be portable — add a Bash
  +`openssl`+`curl` equivalent as an alternative snippet in the README,
  for workflows that don't already have Python set up.

## Code style

- Standard library only.
- **No AiOps Enabler code belongs in `src/`** — that integration is
  confined entirely to the GitHub Actions workflow, by design (see
  README). A PR adding a `signing.py`/`reporting.py` here would be
  out of scope for this repo specifically (cert-sentinel, status-watch,
  ci-triage, and alert-dedupe all demonstrate the in-package pattern).
- Keep I/O behind injectable `fetcher`/`stater`/`now` parameters (see
  `checkers.py`) so tests never touch the network or filesystem.
- No comments explaining *what* code does — only *why*, when non-obvious.
