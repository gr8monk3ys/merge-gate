#!/usr/bin/env python3
"""The merge record: one JSON line per verdict, per sweep, in one local file.

The gate used to keep no record of what it did. Whether a repo had already
taken three gate merges today, which commit on main was the gate's, and
whether one of those had since been reverted were all questions nothing could
answer -- so nothing could refuse to merge on them. This file is the answer,
and the circuit breakers in merge_gate.py read it.

Where it lives: `MERGE_GATE_LEDGER`, else
`~/.local/state/fleet/merge-gate-ledger.jsonl`. `MERGE_GATE_LEDGER=off`
disables it -- for a one-off local run only: with no ledger there is no
merge count and no revert history, so the rate breaker has nothing to count
and a real run (DRY_RUN=0) refuses to start (see `require_writable`).

Why JSONL here and not the control plane's Ledger (ADR-0008 there): that one
is a file per *loop run*, committed and pushed, under the orchestrator
checkout's `runs/ledger/`. It is not importable from this package (it lives in
a different repo and would make the gate depend on the operator's checkout),
and a commit-and-push per sweep is the wrong cost for a record the gate reads
back every half hour. One append-only file is safe here because exactly one
writer -- the sweep -- appends to it, under an exclusive lock. The control
plane can ingest it as a summary; see the README's "Merge ledger" section.

Every row carries `v` (schema version) and `event`:

  verdict  one per PR per sweep: what was decided and on what evidence
  pause    a revert of a gate-merged commit was seen on the default branch

Readers skip lines they cannot parse rather than failing: a torn final line
from a killed run must not stop the next sweep from reading the rest. The
count of skipped lines is reported, never silently dropped.
"""

import datetime as dt
import json
import os

try:                      # POSIX advisory locking; absent on Windows.
    import fcntl
except ImportError:       # pragma: no cover - platform
    fcntl = None

SCHEMA = 1
DEFAULT_PATH = os.path.join("~", ".local", "state", "fleet",
                            "merge-gate-ledger.jsonl")

# Decisions a row can carry. MERGED is the only one the rate breaker counts,
# and only with dry_run false: a report-only sweep merged nothing.
MERGED = "MERGED"
WOULD_MERGE = "WOULD_MERGE"
STALE = "STALE"
ROUTED = "ROUTED"
SKIPPED = "SKIPPED"
DRIFT = "DRIFT"
HELD = "HELD"
NO_VERDICT = "NO_VERDICT"


class LedgerError(Exception):
    """The ledger could not be written. Never swallowed on a real run."""


def path():
    """The ledger file, or None when disabled. Resolved per call, so a test
    or a caller can point it elsewhere late (same rule as repos_yml_path)."""
    raw = os.environ.get("MERGE_GATE_LEDGER", "").strip()
    if raw.lower() == "off":
        return None
    return os.path.expanduser(raw or DEFAULT_PATH)


def now_iso(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def require_writable():
    """Raise LedgerError unless a row can be appended right now.

    Called before a real sweep starts. The breakers are computed from this
    file, so a gate that cannot write it cannot count its own merges -- and
    finding that out after the first merge is finding it out too late.
    """
    p = path()
    if p is None:
        raise LedgerError("MERGE_GATE_LEDGER=off: a real run needs the ledger "
                          "(the merge-rate and revert breakers read it)")
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise LedgerError(f"ledger {p} is not writable: {exc}") from exc
    return p


def append(row, now=None):
    """Append one row; returns it as written. No-op (returns None) when the
    ledger is disabled. Raises LedgerError on an I/O failure."""
    p = path()
    if p is None:
        return None
    row = {"v": SCHEMA, "ts": now_iso(now), **row}
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as exc:
        raise LedgerError(f"ledger {p}: {exc}") from exc
    return row


def read(ledger_path=None):
    """(rows, unparseable_line_count). A missing file is an empty ledger."""
    p = ledger_path or path()
    if p is None or not os.path.exists(p):
        return [], 0
    rows, bad = [], 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def real_merges(rows, repo=None, since=None):
    """Rows recording a merge that actually happened (not a dry run)."""
    out = []
    for r in rows:
        if r.get("event", "verdict") != "verdict" or r.get("decision") != MERGED:
            continue
        if r.get("dry_run"):
            continue
        if repo is not None and r.get("repo") != repo:
            continue
        if since is not None:
            ts = parse_iso(r.get("ts"))
            if ts is None or ts < since:
                continue
        out.append(r)
    return out


def pauses(rows, repo=None):
    """Pause rows (a revert of a gate merge was detected), oldest first."""
    return [r for r in rows if r.get("event") == "pause"
            and (repo is None or r.get("repo") == repo)]
