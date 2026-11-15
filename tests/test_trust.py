"""Trust hardening: what "green" means, what gets recorded, and when the
gate refuses to merge even a green PR.

Pure, like test_policy.py: every GitHub read is a stand-in.
"""
import json

import pytest

import classify_pr as cp
import merge_gate as mg


def _gh(answers):
    """A sh_strict stand-in: answer by the first key found in the argv."""
    def fake(*args):
        joined = " ".join(args)
        for key, val in answers:
            if key in joined:
                return val(joined) if callable(val) else val
        raise AssertionError(f"unexpected gh read: {joined}")
    return fake


def _run(name, conclusion, *, id=1, done="2026-11-01T00:00:00Z", app="github-actions"):
    return {"id": id, "name": name, "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "started_at": done, "completed_at":
            done if conclusion else None, "app": app}


def _repo(required, runs, branch_head="base0", base_runs=None):
    """A green-shaped PR #7 on o/r whose check runs are `runs`."""
    base_runs = base_runs if base_runs is not None else []
    return [
        ("pulls/7 --jq .head.sha", "h7\nmain\nfalse\nclean\nbase0\n"),
        (".auto_merge != null", "false"),
        (".allow_auto_merge", "true"),
        ("required_status_checks", json.dumps(required)),
        ("commits/h7/check-runs", json.dumps(runs)),
        (f"commits/{branch_head}/check-runs", json.dumps(base_runs)),
        ("branches/main --jq .commit.sha", branch_head),
    ]


# --- item 2: reading checks ----------------------------------------------

def test_check_runs_are_read_paginated_at_100(monkeypatch):
    seen = []

    def fake(*args):
        seen.append(args)
        # Two pages, as `--paginate --jq '[...]'` prints them: one array a line.
        return (json.dumps([_run("a", "success", id=1)]) + "\n"
                + json.dumps([_run("b", "success", id=2)]) + "\n")
    monkeypatch.setattr(mg, "sh_strict", fake)
    runs = mg.check_runs("o/r", "h7")
    assert [r["name"] for r in runs] == ["a", "b"]
    assert "--paginate" in seen[0]
    assert "per_page=100" in seen[0][1]


def test_a_garbled_check_runs_page_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: "[]\n<html>502</html>")
    with pytest.raises(mg.ReadFailed):
        mg.check_runs("o/r", "h7")


def test_the_newest_run_of_a_name_wins_whatever_order_github_lists():
    old_green = _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")
    new_red = _run("ci", "failure", id=2, done="2026-11-02T00:00:00Z")
    assert mg.latest_by_name([new_red, old_green])["ci"]["conclusion"] == "failure"
    assert mg.latest_by_name([old_green, new_red])["ci"]["conclusion"] == "failure"


def test_an_in_flight_rerun_shadows_the_finished_attempt_before_it():
    old_green = _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")
    rerun = dict(_run("ci", None, id=2), started_at="2026-11-02T00:00:00Z")
    assert mg.latest_by_name([rerun, old_green])["ci"]["conclusion"] is None


def test_the_id_breaks_a_timestamp_tie():
    a = _run("ci", "success", id=5)
    b = _run("ci", "failure", id=9)
    assert mg.latest_by_name([b, a])["ci"]["id"] == 9


def test_a_newer_red_rerun_blocks_a_pr_an_older_green_run_would_pass(monkeypatch):
    runs = [_run("ci", "failure", id=2, done="2026-11-02T00:00:00Z"),
            _run("ci", "success", id=1, done="2026-11-01T00:00:00Z")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], runs)))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is False and "red: ci" in why


def test_a_skipped_required_check_is_not_green_by_default(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test"], [_run("test", "skipped")])))
    st = mg.read_state("o/r", 7)
    green, why = mg.judge(st)
    assert green is False
    assert "skipped" in why and "may_skip" in why and "test" in why
    assert st.green_by_skip is False


def test_a_skipped_check_the_repo_allowlists_passes_and_says_so(monkeypatch):
    runs = [_run("test", "skipped"), _run("lint", "success")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test", "lint"], runs)))
    st = mg.read_state("o/r", 7, cfg={"may_skip": ["test"]})
    green, why = mg.judge(st)
    assert green is True
    assert st.green_by_skip is True
    assert "green by skip: test" in why


def test_may_skip_is_per_check_not_per_repo(monkeypatch):
    runs = [_run("test", "skipped"), _run("lint", "skipped")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test", "lint"], runs)))
    st = mg.read_state("o/r", 7, cfg={"may_skip": ["test"]})
    green, why = mg.judge(st)
    assert green is False and why.endswith("may_skip: lint")


def test_a_required_check_still_running_is_not_green(monkeypatch):
    # conclusion null used to be neither "missing" nor "failed", so a check
    # that had not finished fell through as green.
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", None)])))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is False and "still running: ci" in why


def test_neutral_and_cancelled_are_not_green(monkeypatch):
    for concl in ("neutral", "cancelled", "timed_out", "action_required"):
        monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", concl)])))
        assert mg.judge(mg.read_state("o/r", 7))[0] is False


def test_each_required_check_is_recorded_with_its_conclusion_and_app(monkeypatch):
    runs = [_run("ci", "success", app="github-actions")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci", "e2e"], runs)))
    st = mg.read_state("o/r", 7)
    assert st.checks == [
        {"name": "ci", "conclusion": "success", "app": "github-actions"},
        {"name": "e2e", "conclusion": "missing", "app": None},
    ]


def test_repo_config_reaches_read_state_through_the_sweep_cache(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["test"], [_run("test", "skipped")])))
    cache = mg.RepoCache({"r": {"may_skip": ["test"]}})
    assert mg.judge(mg.read_state("o/r", 7, cache))[0] is True


def test_gate_config_is_read_from_repos_yml(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    y = tmp_path / "repos.yml"
    y.write_text("repos:\n"
                 "  a: { may_skip: [test], merge_gate_max_per_day: 1 }\n"
                 "  b: { merge_gate_paused: true, merge_gate_ack_reverts: [abc1234] }\n"
                 "  c: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["may_skip"] == ["test"]
    assert cfg["a"]["merge_gate_max_per_day"] == 1
    assert cfg["b"]["merge_gate_paused"] is True
    assert cfg["b"]["merge_gate_ack_reverts"] == ["abc1234"]
    # Absent is the strict value, never a permissive one.
    assert cfg["c"] == {"may_skip": [], "correctness_checks": [],
                        "merge_gate_max_per_day": None,
                        "merge_gate_paused": False, "merge_gate_ack_reverts": []}


def test_a_missing_repos_yml_yields_no_gate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_REPOS_YML", str(tmp_path / "nope.yml"))
    assert cp.load_gate_config() == {}


# --- item 1: the ledger -----------------------------------------------------

import gate_ledger as ledger  # noqa: E402


def _bot_pr(num=7):
    return {"number": num, "title": "bump x from 1.0.0 to 1.0.1", "body": "",
            "author": {"login": "app/dependabot", "is_bot": True},
            "labels": [], "headRefName": f"dependabot/npm_and_yarn/x-{num}"}


def _rows():
    rows, bad = ledger.read()
    assert bad == 0
    return rows


def test_the_ledger_path_comes_from_the_environment_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MERGE_GATE_LEDGER", str(tmp_path / "x.jsonl"))
    assert ledger.path() == str(tmp_path / "x.jsonl")
    monkeypatch.delenv("MERGE_GATE_LEDGER")
    assert ledger.path().endswith("/.local/state/fleet/merge-gate-ledger.jsonl")
    monkeypatch.setenv("MERGE_GATE_LEDGER", "off")
    assert ledger.path() is None and ledger.append({"x": 1}) is None


def test_a_torn_line_is_counted_not_fatal(_isolated_ledger):
    ledger.append({"event": "verdict", "decision": "ROUTED"})
    with open(_isolated_ledger, "a") as f:
        f.write('{"event": "verd')            # a run killed mid-write
    rows, bad = ledger.read()
    assert len(rows) == 1 and bad == 1


def test_a_merged_dependabot_pr_is_recorded_with_its_evidence(monkeypatch):
    runs = [_run("ci", "success", app="github-actions")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], runs)))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: (0, "m3rg3d" if ".merge_commit_sha" in a else "", ""))
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s)
    assert len(s.armed) == 1
    [row] = _rows()
    assert row["event"] == "verdict" and row["v"] == ledger.SCHEMA
    assert (row["repo"], row["pr"], row["decision"]) == ("o/r", 7, "MERGED")
    assert (row["head_sha"], row["base_ref"], row["base_sha"]) == ("h7", "main", "base0")
    assert row["shape"] == "lockfile-only" and row["tier"] == "patch"
    assert row["source"] == "dependabot"
    assert row["checks"] == [{"name": "ci", "conclusion": "success",
                              "app": "github-actions"}]
    assert row["green_by_skip"] is False
    assert row["artifacts"] == {"count": 1, "paths": ["uv.lock"]}
    assert row["merge_commit_sha"] == "m3rg3d"
    assert row["dry_run"] is False and row["reason"].startswith("lockfile-only; green")
    assert row["ts"].endswith("Z")


def test_a_dry_run_records_would_merge_and_reads_no_merge_commit(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", "success")])))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "DRY_RUN", True)
    monkeypatch.setattr(mg, "sh", lambda *a: pytest.fail(f"wrote in a dry run: {a}"))
    mg._evaluate(_bot_pr(), "o/r", 7, {}, mg.Sweep())
    [row] = _rows()
    assert row["decision"] == "WOULD_MERGE" and row["dry_run"] is True
    assert row["merge_commit_sha"] is None
    assert ledger.real_merges(_rows()) == []      # never counted as a merge


def test_a_routed_pr_is_recorded_with_the_head_it_was_judged_at(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh([
        (".auto_merge != null", "false\nh9\nmain\nbase0\n")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["src/x.py"])
    pr = {"number": 9, "title": "feat: thing", "body": "", "labels": [],
          "author": {"login": "bot"}, "headRefName": "auto/finish-20261101"}
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    [row] = _rows()
    assert row["decision"] == "ROUTED" and row["reason"] == "shape=mixed"
    assert row["source"] == "loop" and row["head_sha"] == "h9"
    assert row["checks"] == [] and row["tier"] is None


def test_a_skipped_pr_is_recorded(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh([
        ("pulls/7 --jq .head.sha", "h7\nmain\nfalse\nclean\nbase0\n"),
        (".auto_merge != null", "false"),
        (".allow_auto_merge", "false")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    mg._evaluate(_bot_pr(), "o/r", 7, {}, mg.Sweep())
    [row] = _rows()
    assert row["decision"] == "SKIPPED" and "auto-merge disabled" in row["reason"]


def test_an_unanswered_pr_is_recorded_as_no_verdict(monkeypatch):
    def boom(pr, repo, num, dd, cache=None, stop=None):
        raise mg.ReadFailed("tls blip")
    monkeypatch.setattr(mg, "enumerate_open_prs",
                        lambda deadline=None, clock=None, workers=None:
                        _enum([dict(_bot_pr(), repository={"nameWithOwner": "o/r"})]))
    monkeypatch.setattr(mg, "load_data_dirs", lambda: {})
    monkeypatch.setattr(mg, "load_gate_config", lambda: {})
    monkeypatch.setattr(mg, "is_loop_produced", lambda repo, num, pr: True)
    monkeypatch.setattr(mg, "_read", boom)
    s = mg.sweep()
    assert len(s.failed) == 1
    [row] = _rows()
    assert row["decision"] == "NO_VERDICT" and row["reason"] == "tls blip"
    assert row["source"] == "dependabot"


def test_a_failed_ledger_write_is_reported_not_swallowed(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("MERGE_GATE_LEDGER", str(blocker / "ledger.jsonl"))
    monkeypatch.setattr(mg, "sh_strict", _gh([(".auto_merge != null", "false")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["src/x.py"])
    s = mg.Sweep()
    mg._evaluate(dict(_bot_pr(), title="x"), "o/r", 7, {}, s)
    assert len(s.review) == 1 and len(s.ledger_errors) == 1


def test_a_real_run_refuses_to_start_without_a_writable_ledger(monkeypatch, capsys):
    monkeypatch.setenv("MERGE_GATE_LEDGER", "off")
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sweep", lambda deadline=None: pytest.fail("swept"))
    assert mg.main() == mg.EXIT_NO_LEDGER
    assert "Refusing to merge" in capsys.readouterr().out


def test_a_dry_run_does_not_need_the_ledger(monkeypatch):
    monkeypatch.setenv("MERGE_GATE_LEDGER", "off")
    monkeypatch.setattr(mg, "DRY_RUN", True)
    monkeypatch.setattr(mg, "sweep", lambda deadline=None: mg.Sweep())
    assert mg.main() == 0


def _enum(prs):
    e = cp.Enumeration(sorted({p["repository"]["nameWithOwner"] for p in prs}))
    e.prs = list(prs)
    return e


# --- item 3: a correctness gate is required --------------------------------

@pytest.mark.parametrize("name,gate", [
    ("ci", True), ("test (3.12)", True), ("Lint, Test & Build", True),
    ("typecheck", True), ("e2e", True),
    ("security-baseline", False), ("CodeQL", False), ("Analyze (python)", False),
    ("Dependency Review / build", False),    # deny beats allow
    ("ci-tests-minimum", False),              # runs nothing, cannot fail
    ("Vercel – preview", False), ("extension", False),
])
def test_correctness_by_name(name, gate):
    assert cp.is_correctness_check(name) is gate


def test_a_declared_correctness_check_beats_both_lists():
    assert cp.is_correctness_check("extension", ["extension"]) is True
    assert cp.is_correctness_check("security-baseline", ["security-baseline"]) is True


def test_the_predicate_matches_the_control_planes():
    """Same lists as _fleet.is_correctness_check, checked when the control
    plane is importable beside this checkout; skipped standalone."""
    import importlib.util, os
    path = os.path.expanduser("~/code/_orchestrator-automation/scripts/_fleet.py")
    if not os.path.exists(path):
        pytest.skip("control plane not present")
    src = open(path).read()
    for pat in cp.CORRECTNESS + cp.NOT_CORRECTNESS:
        assert repr(pat) in src or f'r"{pat}"' in src, pat


def test_auto_merge_with_only_a_security_check_is_drift(monkeypatch):
    # fraud-stream: one required check, a security scan. Green says nothing
    # about whether the code still works.
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["security-baseline"],
                                                   [_run("security-baseline", "success")])))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: pytest.fail(f"wrote: {a}"))
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s)
    assert s.armed == [] and s.skipped == []
    [(_, _, why, _)] = s.drift
    assert why.startswith(mg.DRIFT) and "security-baseline" in why
    assert "correctness_checks" in why
    [row] = _rows()
    assert row["decision"] == "DRIFT"


def test_drift_does_not_read_check_runs(monkeypatch):
    calls = []
    fake = _gh(_repo(["CodeQL"], []))
    monkeypatch.setattr(mg, "sh_strict", lambda *a: calls.append(" ".join(a)) or fake(*a))
    st = mg.read_state("o/r", 7)
    assert mg.judge(st)[1].startswith(mg.DRIFT)
    assert not any("check-runs" in c for c in calls)


def test_auto_merge_with_no_required_checks_is_drift_not_skipped(monkeypatch):
    answers = [a for a in _repo([], []) if a[0] != "required_status_checks"]
    monkeypatch.setattr(mg, "sh_strict", _gh([("required_status_checks", None)] + answers))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is None and why.startswith(mg.DRIFT) and "no required checks" in why


def test_auto_merge_disabled_is_still_a_deliberate_skip(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh([
        ("pulls/7 --jq .head.sha", "h7\nmain\nfalse\nclean\nbase0\n"),
        (".allow_auto_merge", "false")]))
    green, why = mg.judge(mg.read_state("o/r", 7))
    assert green is None and not why.startswith(mg.DRIFT)


def test_a_declared_gate_clears_drift(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["extension"], [_run("extension", "success")])))
    assert mg.judge(mg.read_state("o/r", 7))[1].startswith(mg.DRIFT)
    st = mg.read_state("o/r", 7, cfg={"correctness_checks": ["extension"]})
    assert mg.judge(st)[0] is True


def test_one_correctness_check_among_others_is_enough(monkeypatch):
    runs = [_run("CodeQL", "success"), _run("build", "success")]
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["CodeQL", "build"], runs)))
    assert mg.judge(mg.read_state("o/r", 7))[0] is True


def test_drift_counts_as_a_verdict_and_is_reported(monkeypatch, capsys):
    s = mg.Sweep()
    s.drift.append(("o/r", 7, f"{mg.DRIFT} — x", "t"))
    assert s.verdicts == 1
    monkeypatch.setattr(mg, "sweep", lambda deadline=None: s)
    monkeypatch.setattr(mg, "DRY_RUN", True)
    mg.main()
    out = capsys.readouterr().out
    assert "=== DRIFT" in out and "drift=1" in out


def test_a_sweep_can_be_asked_not_to_record(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", _gh([(".auto_merge != null", "false")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["src/x.py"])
    mg._evaluate(_bot_pr(), "o/r", 7, {}, mg.Sweep(record=False))
    assert ledger.read() == ([], 0)


# --- item 4: circuit breakers ----------------------------------------------

import datetime as _dt  # noqa: E402

_NOW = _dt.datetime(2026, 11, 15, 12, 0, tzinfo=_dt.timezone.utc)


def _merged_row(repo="o/r", pr=1, hours_ago=1, sha="a" * 40, head="b" * 40,
                dry_run=False, decision="MERGED"):
    return {"event": "verdict", "repo": repo, "pr": pr, "decision": decision,
            "dry_run": dry_run, "base_ref": "main", "merge_commit_sha": sha,
            "head_sha": head,
            "ts": ledger.now_iso(_NOW - _dt.timedelta(hours=hours_ago))}


def _green_real_run(monkeypatch, cfg=None, base_runs=None):
    """A green, allowlisted dependabot PR in a real run; returns the writes."""
    writes = []
    monkeypatch.setattr(mg, "sh_strict", _gh(_repo(["ci"], [_run("ci", "success")],
                                                   base_runs=base_runs)))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    cache = mg.RepoCache({"r": cfg or {}})
    return writes, cache


def _merges(writes):
    return [w for w in writes if w[:2] == ("pr", "merge") and "--disable-auto" not in w]


def test_a_paused_repo_is_never_merged(monkeypatch):
    writes, cache = _green_real_run(monkeypatch, {"merge_gate_paused": True})
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert s.armed == [] and _merges(writes) == []
    [(_, _, why, _)] = s.held
    assert why.startswith(mg.HELD) and "merge_gate_paused" in why
    assert _rows()[-1]["decision"] == "HELD"


def test_a_paused_repo_is_never_armed_and_a_standing_arm_is_retired(monkeypatch):
    writes, cache = _green_real_run(monkeypatch, {"merge_gate_paused": True})
    monkeypatch.setattr(mg, "sh_strict", _gh(
        [(".auto_merge != null", "true\nh7\nmain\nbase0\n")]
        + _repo(["ci"], [_run("ci", "success")])))
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert _merges(writes) == []
    assert ("pr", "merge", "7", "--repo", "o/r", "--disable-auto") in writes
    assert not any("--auto" in w for w in writes)
    assert len(s.held) == 1


def test_a_paused_repo_is_held_in_a_dry_run_too(monkeypatch):
    writes, cache = _green_real_run(monkeypatch, {"merge_gate_paused": True})
    monkeypatch.setattr(mg, "DRY_RUN", True)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert s.armed == [] and len(s.held) == 1 and writes == []


def test_the_daily_cap_counts_real_merges_from_the_ledger(monkeypatch):
    for i in range(3):
        ledger.append(_merged_row(pr=i, hours_ago=0))
    writes, cache = _green_real_run(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert _merges(writes) == [] and s.armed == []
    assert "merge rate: 3" in s.held[0][2] and "limit 3" in s.held[0][2]


def test_the_cap_ignores_dry_runs_other_repos_and_old_merges(monkeypatch):
    ledger.append(_merged_row(decision="WOULD_MERGE", dry_run=True, hours_ago=0))
    ledger.append(_merged_row(repo="o/other", hours_ago=0))
    ledger.append(_merged_row(hours_ago=0))
    ledger.append(_merged_row(hours_ago=0))
    rows = ledger.read()[0]
    rows[-1]["ts"] = rows[-2]["ts"] = "2026-01-01T00:00:00Z"   # long ago
    s = mg.Sweep()
    s.history = rows
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean")
    assert mg.breaker_reason(st, s, now=_NOW) is None


def test_the_cap_is_overridable_per_repo_and_by_env(monkeypatch):
    s = mg.Sweep()
    s.history = [_merged_row(hours_ago=1)]
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean",
                  {"merge_gate_max_per_day": 1})
    assert "limit 1" in mg.breaker_reason(st, s, now=_NOW)
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean")
    monkeypatch.setenv("MERGE_GATE_MAX_PER_DAY", "1")
    assert "limit 1" in mg.breaker_reason(st, s, now=_NOW)
    monkeypatch.setenv("MERGE_GATE_MAX_PER_DAY", "5")
    assert mg.breaker_reason(st, s, now=_NOW) is None
    # Repo config beats env; nonsense falls back to the default, not unlimited.
    assert mg.max_per_day({"merge_gate_max_per_day": 2}) == 2
    monkeypatch.setenv("MERGE_GATE_MAX_PER_DAY", "lots")
    assert mg.max_per_day({}) == mg.DEFAULT_MAX_PER_DAY


def test_this_sweeps_own_merges_count_toward_the_cap(monkeypatch):
    s = mg.Sweep()
    s.history = [_merged_row(hours_ago=1), _merged_row(hours_ago=2)]
    s.armed.append(("o/r", 3, "lockfile-only; green", "t"))
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean")
    assert "merge rate: 3" in mg.breaker_reason(st, s, now=_NOW)


def test_a_red_base_holds_a_green_pr(monkeypatch):
    writes, cache = _green_real_run(
        monkeypatch, base_runs=[_run("test", "failure"), _run("CodeQL", "success")])
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert _merges(writes) == []
    assert s.held[0][2] == f"{mg.HELD} — main is red at base0: test"


def test_a_red_non_correctness_check_on_base_does_not_hold(monkeypatch):
    writes, cache = _green_real_run(
        monkeypatch, base_runs=[_run("CodeQL", "failure"), _run("build", "success"),
                                _run("test", "cancelled")])
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert len(_merges(writes)) == 1 and s.held == []


def test_a_base_fixed_by_a_rerun_is_not_red(monkeypatch):
    base_runs = [_run("test", "failure", id=1, done="2026-11-01T00:00:00Z"),
                 _run("test", "success", id=2, done="2026-11-02T00:00:00Z")]
    writes, cache = _green_real_run(monkeypatch, base_runs=base_runs)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert len(_merges(writes)) == 1


def _commits(*pairs):
    return lambda repo, branch, since: list(pairs)


def test_a_revert_of_a_gate_merge_is_detected_by_sha():
    rows = [_merged_row(pr=4, sha="abcdef1234567890" + "0" * 24)]
    pauses, unscanned = mg.detect_reverts(rows, now=_NOW, read=_commits(
        ("r3v3rt" + "0" * 34, 'Revert "bump x"\n\nThis reverts commit abcdef1234567.'),
        ("other" + "0" * 35, "fix: unrelated")))
    assert unscanned == set()
    [p] = pauses
    assert (p["event"], p["repo"], p["pr"]) == ("pause", "o/r", 4)
    assert p["revert_sha"].startswith("r3v3rt")


def test_a_github_revert_pr_is_detected_by_pr_number():
    rows = [_merged_row(pr=12)]
    pauses, _ = mg.detect_reverts(rows, now=_NOW, read=_commits(
        ("c" * 40, 'Revert "bump x (#12)" (#13)\n\nReverts o/r#12')))
    assert [p["pr"] for p in pauses] == [12]


def test_unrelated_reverts_and_other_repos_prs_do_not_pause():
    rows = [_merged_row(pr=12, sha="a" * 40)]
    pauses, _ = mg.detect_reverts(rows, now=_NOW, read=_commits(
        ("c" * 40, "This reverts commit 1234567890abcdef."),
        ("d" * 40, "Reverts o/other#12")))
    assert pauses == []


def test_a_revert_already_recorded_is_not_recorded_again():
    rows = [_merged_row(pr=12), {"event": "pause", "repo": "o/r", "revert_sha": "c" * 40}]
    pauses, _ = mg.detect_reverts(rows, now=_NOW, read=_commits(
        ("c" * 40, "Reverts o/r#12")))
    assert pauses == []


def test_only_real_recent_merges_are_scanned():
    reads = []
    rows = [_merged_row(dry_run=True, decision="WOULD_MERGE"),
            _merged_row(hours_ago=24 * 30)]
    mg.detect_reverts(rows, now=_NOW, read=lambda *a: reads.append(a) or [])
    assert reads == []


def test_an_unscannable_repo_is_reported_and_its_merges_held():
    def fail(*a):
        raise mg.ReadFailed("tls blip")
    pauses, unscanned = mg.detect_reverts([_merged_row()], now=_NOW, read=fail)
    assert pauses == [] and unscanned == {"o/r"}
    s = mg.Sweep()
    s.history, s.unscanned = [], {"o/r"}
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean")
    assert "could not scan" in mg.breaker_reason(st, s, now=_NOW)


def test_a_revert_pause_holds_until_its_sha_is_acknowledged(monkeypatch):
    revert = "c0ffee" + "0" * 34
    ledger.append({"event": "pause", "repo": "o/r", "base_ref": "main",
                   "revert_sha": revert, "pr": 4, "reverted_sha": "a" * 40})
    writes, cache = _green_real_run(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert _merges(writes) == []
    why = s.held[0][2]
    assert "PAUSED" in why and "c0ffee000000" in why and "merge_gate_ack_reverts" in why

    # Time passing and later sweeps finding nothing new do not clear it...
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean")
    s2 = mg.Sweep()
    assert "PAUSED" in mg.breaker_reason(st, s2, now=_NOW + _dt.timedelta(days=90))
    # ...an unrelated ack does not clear it...
    st = mg.State("o/r", 7, "h7", "main", "base0", "false", "clean",
                  {"merge_gate_ack_reverts": ["deadbeef"]})
    assert "PAUSED" in mg.breaker_reason(st, s2, now=_NOW)
    # ...only acknowledging that revert's sha (a prefix will do) clears it.
    writes, cache = _green_real_run(monkeypatch, {"merge_gate_ack_reverts": ["c0ffee0"]})
    s3 = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s3, cache)
    assert len(_merges(writes)) == 1 and s3.held == []


def test_the_sweep_scans_for_reverts_before_deciding_and_records_the_pause(monkeypatch):
    ledger.append(_merged_row(pr=4, hours_ago=1))
    monkeypatch.setattr(mg, "branch_commits",
                        lambda repo, branch, since: [("c" * 40, "Reverts o/r#4")])
    monkeypatch.setattr(mg, "detect_reverts",
                        lambda rows, now=None, read=None, _d=mg.detect_reverts:
                        _d(rows, now=now, read=mg.branch_commits))
    writes, _ = _green_real_run(monkeypatch)
    monkeypatch.setattr(mg, "load_data_dirs", lambda: {})
    monkeypatch.setattr(mg, "load_gate_config", lambda: {})
    monkeypatch.setattr(mg, "is_loop_produced", lambda repo, num, pr: True)
    monkeypatch.setattr(mg, "enumerate_open_prs",
                        lambda deadline=None, clock=None, workers=None:
                        _enum([dict(_bot_pr(), repository={"nameWithOwner": "o/r"})]))
    s = mg.sweep()
    assert _merges(writes) == [] and len(s.held) == 1
    assert [r["event"] for r in _rows()] == ["verdict", "pause", "verdict"]
    assert _rows()[-1]["decision"] == "HELD"


def test_branch_commits_reads_every_page(monkeypatch):
    seen = []

    def fake(*args):
        seen.append(args)
        return (json.dumps([{"sha": "a" * 40, "commit": {"message": "x\n\ny"}}])
                + json.dumps([{"sha": "b" * 40, "commit": {"message": "z"}}]))
    monkeypatch.setattr(mg, "sh_strict", fake)
    assert mg.branch_commits("o/r", "main", "2026-11-01T00:00:00Z") == [
        ("a" * 40, "x\n\ny"), ("b" * 40, "z")]
    assert "--paginate" in seen[0] and "since=2026-11-01T00:00:00Z" in seen[0][1]


# --- item 5: tests-only merges brand-new test files only --------------------

def _new(name, patch="@@ -0,0 +1 @@\n+def test_p():\n+    assert p()\n"):
    return {"filename": name, "status": "added", "patch": patch}


def _f(name, patch="", status="modified", prev=None):
    d = {"filename": name, "status": status, "patch": patch}
    if prev:
        d["previous_filename"] = prev
    return d


EXISTING = ["src/p.py", "tests/test_q.py", "src/a.test.ts"]


def test_brand_new_test_files_pass():
    files = [_new("tests/test_p.py"), _new("src/b.test.ts", "@@ -0,0 +1 @@\n+expect(b()).toBe(1)\n")]
    assert cp.tests_only_veto(files, EXISTING) is None


@pytest.mark.parametrize("status", ["modified", "changed", "removed"])
def test_a_change_to_an_existing_test_file_is_never_auto_merged(status):
    # Even a pure addition to an existing file: an early `return` or a
    # shadowing duplicate inside it is not something a pattern can rule out.
    why = cp.tests_only_veto([_f("tests/test_q.py", "@@ -1 +1,2 @@\n+    assert q()\n",
                                 status)], EXISTING)
    assert why == (f"{status} existing test file tests/test_q.py: only brand-new "
                   "test files auto-merge")


@pytest.mark.parametrize("status,prev,name", [
    ("renamed", "tests/test_parser.py", "tests/parser.py"),
    ("renamed", "src/a.test.ts", "src/a.spec.ts"),
    ("copied", "tests/test_a.py", "tests/test_b.py"),
])
def test_a_renamed_or_copied_test_file_is_vetoed(status, prev, name):
    why = cp.tests_only_veto([_f(name, "", status, prev=prev)], EXISTING)
    assert why == (f"{status} existing test file {prev} -> {name}: only brand-new "
                   "test files auto-merge")


@pytest.mark.parametrize("path", [
    "conftest.py", "tests/conftest.py", "e2e/login.spec.ts",
    "apps/web/e2e/login.spec.ts", "playwright/fixtures.ts", "cypress/e2e/a.cy.ts",
    "playwright.config.ts", "web/cypress.config.js", "tests/pytest.ini",
    "tests/jest.config.js", "web/vitest.config.ts", "tests/setupTests.ts",
])
def test_new_harness_and_config_files_are_rejected(path):
    assert cp.tests_only_veto([_new(path)], EXISTING) == f"touches test harness/config: {path}"


def test_a_new_file_named_like_an_existing_one_is_rejected():
    why = cp.tests_only_veto([_new("tests/unit/test_q.py")], EXISTING)
    assert why == "new test file tests/unit/test_q.py shares its name with tests/test_q.py"


def test_two_new_files_with_one_name_are_rejected():
    why = cp.tests_only_veto([_new("tests/a/test_z.py"), _new("tests/b/test_z.py")], EXISTING)
    assert why == "new test file tests/b/test_z.py shares its name with tests/a/test_z.py"


def test_a_duplicate_test_name_inside_a_new_file_is_rejected():
    patch = "@@ -0,0 +1,4 @@\n+def test_p():\n+    assert 0\n+def test_p():\n+    assert 1\n"
    why = cp.tests_only_veto([_new("tests/test_p.py", patch)], EXISTING)
    assert why == "defines test_p twice in tests/test_p.py; the second shadows the first"


def test_an_unlisted_base_tree_vetoes():
    why = cp.tests_only_veto([_new("tests/test_p.py")], None)
    assert why.startswith("could not list the base tree")


def test_a_non_test_path_listed_as_added_is_rejected():
    assert cp.tests_only_veto([_new("src/p2.py")], EXISTING) == "src/p2.py is not a test path"


def test_an_added_file_whose_patch_removes_lines_is_not_new():
    why = cp.tests_only_veto([_new("tests/test_p.py", "@@ -1 +1 @@\n-x\n+y\n")], EXISTING)
    assert why == "tests/test_p.py is listed as added but its patch removes lines"


@pytest.mark.parametrize("line", [
    "@pytest.mark.skip(reason='flaky')", "    pytest.mark.skip", "@mark.xfail",
    "pytestmark = [slow]", "    return pytestmark",
    # aliased: none of these names the marker by its usual spelling
    "from pytest import mark as m", "@m.skip", "sk = pytest.mark.skip",
    "@pt.mark.skipif(True, reason='x')", "    pt.skip('later')",
    "from unittest import SkipTest", "    raise SkipTest('later')",
    "    raise pytest.skip.Exception('later')",
    "__test__ = False", "    pytest.skip('later')", "    pytest.importorskip('numpy')",
    "it.skip('renders', () => {", "describe.only('suite', () => {",
    "test.todo('later')", "xit('renders', () => {", "fdescribe('s', () => {",
    "@unittest.expectedFailure", "@skipIf(True, 'x')", "        self.skipTest('x')",
    '    t.Skip("later")', "#[ignore]",
])
def test_a_disabling_marker_in_a_new_file_is_caught(line):
    why = cp.tests_only_veto([_new("tests/test_p.py", f"@@ -0,0 +1 @@\n+{line}\n")], EXISTING)
    assert why is not None and why.startswith("adds a test-disabling marker (")
    assert "tests/test_p.py" in why


def test_ordinary_test_code_is_not_a_marker():
    patch = ("@@ -0,0 +1,3 @@\n+def test_parse():\n+    skip_empty = True\n"
             "+    assert parse(skip_empty) == []\n")
    assert cp.tests_only_veto([_new("tests/test_parse.py", patch)], EXISTING) is None


def test_a_file_with_no_patch_or_an_unread_diff_vetoes():
    why = cp.tests_only_veto([{"filename": "tests/test_big.py", "status": "added"}], EXISTING)
    assert why.startswith("no readable patch for tests/test_big.py")
    assert cp.tests_only_veto(None, EXISTING) == "could not read the diff to check test content"
    assert cp.tests_only_veto([], EXISTING) == "the diff lists no files"


# The read path: compare API at exactly base_sha...head_sha, head read once.

def _tests_only(monkeypatch, files, head="hA", tree=None, paths=None):
    reads = []
    tree = tree if tree is not None else {"truncated": False,
                                          "tree": [{"path": p} for p in EXISTING]}
    answers = _gh([
        (f"compare/base0...{head}", json.dumps({"files": files})),
        ("git/trees/base0", json.dumps(tree)),
        (".auto_merge != null", "false"),
        ("pulls/9 --jq .head.sha", f"{head}\nmain\nfalse\nclean\nbase0\n"),
        (".allow_auto_merge", "true"),
        ("required_status_checks", '["ci"]'),
        (f"commits/{head}/check-runs", json.dumps([_run("ci", "success")])),
        ("commits/base0/check-runs", "[]"),
        ("branches/main --jq .commit.sha", "base0")])
    monkeypatch.setattr(mg, "sh_strict", lambda *a: reads.append(" ".join(a)) or answers(*a))
    monkeypatch.setattr(mg, "changed_paths",
                        lambda *a: paths if paths is not None else [f["filename"] for f in files])
    pr = {"number": 9, "title": "test: cover p", "body": "", "labels": [],
          "author": {"login": "bot"}, "headRefName": "auto/tests-20261115"}
    return pr, reads


def test_a_clean_new_test_file_merges_at_the_judged_sha(monkeypatch):
    pr, reads = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    writes = []
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    assert s.armed and s.armed[0][2].startswith("tests-only; green")
    merges = [w for w in writes if w[:2] == ("pr", "merge")]
    assert merges == [("pr", "merge", "9", "--repo", "o/r", "--squash",
                       "--match-head-commit", "hA")]
    # The diff judged is the compare of exactly that range, and the head was
    # read once -- there is no second head read to disagree with.
    assert any("compare/base0...hA" in r for r in reads)
    assert sum("pulls/9 --jq .head.sha" in r for r in reads) == 1
    assert not any("pulls/9/files" in r for r in reads)


def test_a_modified_test_file_is_never_auto_merged(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_f("tests/test_q.py", "@@ -1 +1,2 @@\n+    assert q()\n")])
    writes = []
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    assert s.armed == [] and not [w for w in writes if w[:2] == ("pr", "merge")]
    [(_, _, why, _)] = s.review
    assert why == ("tests-only rejected: modified existing test file tests/test_q.py: "
                   "only brand-new test files auto-merge")
    assert _rows()[-1]["decision"] == "ROUTED"


def test_an_aliased_skip_in_a_new_file_routes_to_review(monkeypatch):
    patch = ("@@ -0,0 +1,4 @@\n+from pytest import mark as m\n+@m.skip\n"
             "+def test_p():\n+    assert p()\n")
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py", patch)])
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    assert s.armed == []
    assert s.review[0][2].startswith("tests-only rejected: adds a test-disabling marker")


def test_a_diff_that_is_not_the_classified_one_is_no_verdict(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py"), _new("src/evil.py")],
                        paths=["tests/test_p.py"])
    with pytest.raises(mg.ReadFailed, match="not the diff that was classified"):
        mg._read(pr, "o/r", 9, {})


def test_a_truncated_base_tree_vetoes_rather_than_guesses(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")],
                        tree={"truncated": True, "tree": []})
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    assert s.review[0][2].startswith("tests-only rejected: could not list the base tree")


def test_a_compare_at_the_file_cap_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: json.dumps(
        {"files": [_new(f"tests/test_{i}.py") for i in range(mg.COMPARE_FILE_CAP)]}))
    with pytest.raises(mg.ReadFailed, match="possibly truncated"):
        mg.compare_files("o/r", "base0", "hA")
