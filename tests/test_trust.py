"""Trust hardening: what "green" means, what gets recorded, and when the
gate refuses to merge even a green PR.

Pure, like test_policy.py: every GitHub read is a stand-in.
"""
import json

import pytest

import classify_pr as cp
import merge_gate as mg


def _new_file(name, patch):
    return {"filename": name, "status": "added", "patch": patch}


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
                 "  a: { may_skip: [test], merge_gate_max_per_day: 1,\n"
                 "       verify_workflow: .github/workflows/ci.yml }\n"
                 "  b: { merge_gate_paused: true, merge_gate_ack_reverts: [abc1234] }\n"
                 "  c: {}\n")
    monkeypatch.setenv("GATE_REPOS_YML", str(y))
    cfg = cp.load_gate_config()
    assert cfg["a"]["may_skip"] == ["test"]
    assert cfg["a"]["merge_gate_max_per_day"] == 1
    assert cfg["a"]["verify_workflow"] == ".github/workflows/ci.yml"
    assert cfg["b"]["merge_gate_paused"] is True
    assert cfg["b"]["merge_gate_ack_reverts"] == ["abc1234"]
    # Absent is the strict value, never a permissive one.
    assert cfg["c"] == {"may_skip": [], "correctness_checks": [],
                        "merge_gate_max_per_day": None,
                        "merge_gate_paused": False, "merge_gate_ack_reverts": [],
                        "allow_tests_only": False, "require_verify_evidence": False,
                        "verify_workflow": None,
                        "allow_reviewed": False, "allow_reviewed_content": False}


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


# --- verify-report: CI-reported test counts ---------------------------------

import io  # noqa: E402
import zipfile  # noqa: E402


def _stage(name, tests, failed=0, skipped=0):
    return {"stage": name, "tests": tests, "passed": tests - failed - skipped,
            "failed": failed, "skipped": skipped}


def _report(sha, stages, repo="o/r", schema=1):
    return {"schema": schema, "repo": repo, "commit": sha, "stages": stages}


def _zip(doc, name="verify-report.json", extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, doc if isinstance(doc, (str, bytes)) else json.dumps(doc))
        for n in extra or []:
            z.writestr(n, "x")
    return buf.getvalue()


WF = ".github/workflows/ci.yml"


def _wf_run(id, sha=None, event="pull_request", branch="x", conclusion="success",
            updated="2026-11-15T00:00:00Z", path=WF):
    return {"id": id, "head_sha": sha or SHA, "head_branch": branch, "updated_at": updated,
            "path": path, "event": event, "conclusion": conclusion}


# One completed run per sha, run id = "run-<sha>", artifact id = "art-<sha>",
# of the declared workflow and the event asked for. A run on the base sha is
# a push to main; on any other sha, a PR branch.
def _runs_answer(joined):
    sha = joined.split("head_sha=")[1].split("&")[0]
    event = joined.split("event=")[1].split("&")[0]
    branch = "main" if sha.startswith("base") else "auto/x"
    return json.dumps([_wf_run(f"run-{sha}", sha, event, branch)])


def _arts_answer(joined):
    sha = joined.split("runs/run-")[1].split("/")[0]
    return json.dumps([{"id": f"art-{sha}", "name": "verify-report",
                        "expired": False, "size_in_bytes": 300}])


_EVIDENCE_ANSWERS = [("actions/runs?head_sha=", _runs_answer),
                     ("/artifacts?per_page", _arts_answer)]


def _serve_evidence(monkeypatch, by_sha, downloads=None):
    """gh_transport.download stand-in: a zip of {sha: stages}'s report."""
    def dl(path):
        sha = path.split("artifacts/art-")[1].split("/")[0]
        if downloads is not None:
            downloads.append(sha)
        if sha not in by_sha:
            return gh_transport_result(1, b"", "gh: Not Found (HTTP 404)")
        return gh_transport_result(0, _zip(_report(sha, by_sha[sha])), "")
    monkeypatch.setattr(mg.gh_transport, "download", dl)


def gh_transport_result(code, out, err):
    return mg.gh_transport.Result(code, out, err)


SHA = "a" * 40


def test_a_valid_report_parses_to_its_stages():
    stages, why = mg.parse_verify_report(
        _zip(_report(SHA, [_stage("unit", 10, failed=1, skipped=2), _stage("e2e", 3)])),
        "o/r", SHA)
    assert why is None
    assert stages == {"unit": {"tests": 10, "passed": 7, "failed": 1, "skipped": 2},
                      "e2e": {"tests": 3, "passed": 3, "failed": 0, "skipped": 0}}


@pytest.mark.parametrize("doc, expect", [
    (_report(SHA, [_stage("u", 1)], schema=2), "schema 2"),
    (_report(SHA, [_stage("u", 1)], schema=True), "schema True"),
    (_report(SHA, [_stage("u", 1)], repo="o/other"), "repo 'o/other'"),
    (_report(SHA[:12], [_stage("u", 1)]), "for commit"),
    (_report("b" * 40, [_stage("u", 1)]), "for commit"),
    (_report(SHA, []), "no stages"),
    (_report(SHA, [{"tests": 1, "passed": 1, "failed": 0, "skipped": 0}]), "no name"),
    (_report(SHA, [_stage("u", 1), _stage("u", 2)]), "listed twice"),
    (_report(SHA, [dict(_stage("u", 1), failed=-1, passed=2)]), "non-negative"),
    (_report(SHA, [dict(_stage("u", 2), passed=2.0)]), "non-negative"),
    (_report(SHA, [dict(_stage("u", 2), skipped=True, passed=1)]), "non-negative"),
    (_report(SHA, [dict(_stage("u", 5), passed=3)]), "passed+failed+skipped != tests"),
    ([1, 2], "not an object"),
])
def test_an_invalid_report_is_no_evidence(doc, expect):
    stages, why = mg.parse_verify_report(_zip(doc), "o/r", SHA)
    assert stages is None and expect in why


def _ids_stage(name, ids, **kw):
    return dict(_stage(name, len(ids)), passed_ids=ids, **kw)


def test_passed_ids_parse_with_the_stage():
    stages, why = mg.parse_verify_report(
        _zip(_report(SHA, [_ids_stage("u", ["t::a", "t::b"]), _stage("e2e", 1)])),
        "o/r", SHA)
    assert why is None
    assert stages["u"]["passed_ids"] == ["t::a", "t::b"]
    assert "passed_ids" not in stages["e2e"]


@pytest.mark.parametrize("ids, expect", [
    ("t::a", "passed_ids is not a list"),
    ({"t::a": 1}, "passed_ids is not a list"),
    (["t::a", ""], "non-empty strings"),
    (["t::a", 3], "non-empty strings"),
    (["t::a", None], "non-empty strings"),
    (["t::b", "t::a"], "not sorted and unique"),
    (["t::a", "t::a"], "not sorted and unique"),
])
def test_invalid_passed_ids_are_no_evidence(ids, expect):
    st = dict(_stage("u", 2), passed_ids=ids)
    stages, why = mg.parse_verify_report(_zip(_report(SHA, [st])), "o/r", SHA)
    assert stages is None and expect in why


def test_passed_ids_cannot_outnumber_passed_tests():
    st = dict(_stage("u", 1), passed_ids=["t::a", "t::b"])
    stages, why = mg.parse_verify_report(_zip(_report(SHA, [st])), "o/r", SHA)
    assert stages is None and why == "stage u: 2 passed_ids for 1 passed"


def test_deduplicated_passed_ids_are_evidence():
    st = dict(_stage("e2e", 84), passed_ids=["t::a", "t::b"])
    stages, why = mg.parse_verify_report(_zip(_report(SHA, [st])), "o/r", SHA)
    assert why is None and stages["e2e"]["passed_ids"] == ["t::a", "t::b"]


def test_passed_ids_over_the_cap_are_no_evidence(monkeypatch):
    monkeypatch.setattr(mg, "VERIFY_MAX_IDS", 3)
    doc = _report(SHA, [_ids_stage("u", ["a", "b"]), _ids_stage("v", ["c", "d"])])
    stages, why = mg.parse_verify_report(_zip(doc), "o/r", SHA)
    assert stages is None and why == "more than 3 passed_ids"


def test_an_artifact_that_is_not_exactly_one_report_is_no_evidence():
    ok = _report(SHA, [_stage("u", 1)])
    assert "exactly" in mg.parse_verify_report(_zip(ok, extra=["b.json"]), "o/r", SHA)[1]
    assert "exactly" in mg.parse_verify_report(_zip(ok, name="r.json"), "o/r", SHA)[1]
    assert "unreadable" in mg.parse_verify_report(b"not a zip", "o/r", SHA)[1]
    assert "unreadable" in mg.parse_verify_report(_zip("{oops"), "o/r", SHA)[1]


def _serve_runs(monkeypatch, runs, arts):
    """sh_strict for the runs listing and each run's artifacts; returns the
    downloads made, each answering a valid report for SHA."""
    monkeypatch.setattr(mg, "sh_strict", _gh(
        [("actions/runs?head_sha=", json.dumps(runs))]
        + [(f"runs/{i}/artifacts", json.dumps(a)) for i, a in arts.items()]))
    got = []
    monkeypatch.setattr(mg.gh_transport, "download", lambda p: got.append(p) or
                        mg.gh_transport.Result(0, _zip(_report(SHA, [_stage("u", 4)])), ""))
    return got


def _art(id, name="verify-report"):
    return [{"id": id, "name": name, "expired": False}]


def test_the_newest_run_of_the_declared_workflow_and_event_is_read(monkeypatch):
    runs = [_wf_run(1, updated="2026-11-01T00:00:00Z"),
            _wf_run(2, updated="2026-11-02T00:00:00Z"),
            # Newer, but not the declared workflow, or not the event asked for.
            _wf_run(3, updated="2026-11-03T00:00:00Z", path=".github/workflows/other.yml"),
            _wf_run(4, updated="2026-11-04T00:00:00Z", event="workflow_dispatch")]
    got = _serve_runs(monkeypatch, runs, {i: _art(10 + i) for i in (1, 2, 3, 4)})
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert why is None and rep["stages"]["u"]["tests"] == 4
    assert (rep["run"], rep["workflow"], rep["event"]) == (2, WF, "pull_request")
    assert got == ["repos/o/r/actions/artifacts/12/zip"]


def test_the_runs_read_asks_for_the_event(monkeypatch):
    reads = []
    monkeypatch.setattr(mg, "sh_strict", lambda *a: reads.append(" ".join(a)) or "[]")
    mg.verify_report("o/r", SHA, WF, "push", "main")
    assert f"head_sha={SHA}&event=push&status=completed" in reads[0]


def test_only_a_run_of_the_declared_workflow_counts(monkeypatch):
    _serve_runs(monkeypatch, [_wf_run(1, path=".github/workflows/evil.yml")], {1: _art(11)})
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert rep is None and why == f"no completed {WF} pull_request run"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_a_newest_run_that_did_not_succeed_is_no_evidence_even_with_an_older_success(
        monkeypatch, conclusion):
    runs = [_wf_run(1, updated="2026-11-01T00:00:00Z"),
            _wf_run(2, updated="2026-11-02T00:00:00Z", conclusion=conclusion)]
    got = _serve_runs(monkeypatch, runs, {1: _art(11), 2: _art(12)})
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert rep is None and why == f"newest {WF} pull_request run 2 concluded {conclusion}"
    assert got == []


def test_a_newest_run_without_the_artifact_is_not_rescued_by_an_older_run(monkeypatch):
    runs = [_wf_run(1, updated="2026-11-01T00:00:00Z"),
            _wf_run(2, updated="2026-11-02T00:00:00Z")]
    got = _serve_runs(monkeypatch, runs, {1: _art(11), 2: _art(12, "coverage")})
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert rep is None and why == "run 2 has 0 verify-report artifacts"
    assert got == []


def test_a_newest_run_with_an_invalid_report_is_not_rescued_by_an_older_run(monkeypatch):
    runs = [_wf_run(1, updated="2026-11-01T00:00:00Z"),
            _wf_run(2, updated="2026-11-02T00:00:00Z")]
    _serve_runs(monkeypatch, runs, {1: _art(11), 2: _art(12)})
    monkeypatch.setattr(mg.gh_transport, "download", lambda p: mg.gh_transport.Result(
        0, _zip(_report(SHA, [_stage("u", 4)], schema=2)) if "/12/" in p
        else _zip(_report(SHA, [_stage("u", 4)])), ""))
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert rep is None and why == "run 2: schema 2, want 1"


def test_base_evidence_must_come_from_a_push_run_of_the_base_branch(monkeypatch):
    _serve_runs(monkeypatch, [_wf_run(1, event="push", branch="auto/x")], {1: _art(11)})
    rep, why = mg.verify_report("o/r", SHA, WF, "push", "main")
    assert rep is None and why == f"no completed {WF} push on main run"


@pytest.mark.parametrize("art, err, expect", [
    ({"expired": True}, None, "has expired"),
    ({"size_in_bytes": mg.VERIFY_MAX_BYTES + 1}, None, "too large"),
    ({}, "gh: Artifact has expired (HTTP 410)", "could not be downloaded"),
])
def test_an_unusable_artifact_is_no_evidence(monkeypatch, art, err, expect):
    runs = [_wf_run(1)]
    monkeypatch.setattr(mg, "sh_strict", _gh([
        ("actions/runs?", json.dumps(runs)),
        ("runs/1/artifacts", json.dumps([dict({"id": 5, "name": "verify-report",
                                               "expired": False}, **art)]))]))
    monkeypatch.setattr(mg.gh_transport, "download",
                        lambda p: mg.gh_transport.Result(1, b"", err or "unexpected"))
    rep, why = mg.verify_report("o/r", SHA, WF, "pull_request")
    assert rep is None and expect in why


def test_a_download_github_did_not_answer_is_no_verdict_not_no_evidence(monkeypatch):
    runs = [_wf_run(1)]
    monkeypatch.setattr(mg, "sh_strict", _gh([
        ("actions/runs?", json.dumps(runs)),
        ("runs/1/artifacts", json.dumps([{"id": 5, "name": "verify-report"}]))]))
    monkeypatch.setattr(mg.gh_transport, "download",
                        lambda p: mg.gh_transport.Result(1, b"", "gh: HTTP 502"))
    with pytest.raises(mg.ReadFailed):
        mg.verify_report("o/r", SHA, WF, "pull_request")


def _c(tests, failed=0, skipped=0):
    return {"tests": tests, "passed": tests - failed - skipped,
            "failed": failed, "skipped": skipped}


def _i(ids):
    return dict(_c(len(ids)), passed_ids=ids)


@pytest.mark.parametrize("head, base, full, new, expect", [
    ({"u": _c(11)}, {"u": _c(10)}, True, 1, None),
    ({"u": _c(12)}, {"u": _c(10)}, True, None, None),
    ({"u": _c(11, failed=1)}, {"u": _c(10)}, True, 1, "stage u: 1 failed at head"),
    ({"u": _c(11)}, {"u": _c(10), "e2e": _c(70)}, True, 1,
     "stage(s) e2e ran at base but not at head"),
    ({"u": _c(20), "e2e": _c(64)}, {"u": _c(10), "e2e": _c(70)}, True, 1,
     "stage e2e: tests 70 -> 64 at head"),
    ({"u": _c(11, skipped=2)}, {"u": _c(10)}, True, 1, "stage u: passed 10 -> 9 at head"),
    ({"u": _c(11)}, {"u": _c(10)}, True, 3,
     "total tests grew by 1, need the 3 new test case(s) the diff adds"),
    ({"u": _c(10)}, {"u": _c(10)}, True, None,
     "total tests grew by 0, need at least one (new cases could not be counted)"),
    # A rise in skipped hides a test as surely as a drop in passed does.
    ({"u": _c(12, skipped=1)}, {"u": _c(10)}, True, 1, "stage u: skipped 0 -> 1 at head"),
    ({"u": _i(["a", "b", "c"])}, {"u": _i(["a", "b"])}, True, 1, None),
    ({"u": _i(["a", "c", "d"])}, {"u": _i(["a", "b"])}, True, 1,
     "stage u: b passed at base but not at head"),
    ({"u": _i(["a", "x", "y"])}, {"u": _i(["a", "b", "c"])}, True, None,
     "stage u: b passed at base but not at head (+1 more)"),
    # Counts grew enough, but a base test was swapped out for new ones.
    ({"u": _i(["a", "b", "x", "y"])}, {"u": _i(["a", "b", "c"])}, True, 1,
     "stage u: c passed at base but not at head"),
    ({"u": _i(["a", "b", "x"]), "v": _i(["d", "e"])},
     {"u": _i(["a", "b"]), "v": _i(["d", "e"])}, True, 2,
     "total tests grew by 1, need the 2 new test case(s) the diff adds"),
    ({"u": _i(["a", "b", "x", "y"]), "v": _i(["e", "f"])},
     {"u": _i(["a", "b"]), "v": _i(["d", "e"])}, True, 2,
     "stage v: d passed at base but not at head"),
    # IDs may be fewer than passes, so a test run twice is no new test.
    ({"u": dict(_c(4), passed_ids=["a", "b"])}, {"u": _i(["a", "b"])}, True, None,
     "total tests grew by 0, need at least one (new cases could not be counted)"),
    ({"u": dict(_c(6), passed_ids=["a", "b", "x"])}, {"u": dict(_c(4), passed_ids=["a", "b"])},
     True, 1, None),
    # Splitting one rerun ID into two adds an ID but runs nothing new.
    ({"u": dict(_c(4), passed_ids=["a", "a2", "b"])}, {"u": dict(_c(4), passed_ids=["a", "b"])},
     True, None, "total tests grew by 0, need at least one (new cases could not be counted)"),
    # IDs at base but not at head: no downgrade to the count rules.
    ({"u": _c(11)}, {"u": _i(["a", "b"])}, True, 1,
     "stage u: passed_ids at base but not at head"),
    # IDs at head only (base predates them): the count rules apply.
    ({"u": _i(["a", "b", "c"])}, {"u": _c(2)}, True, 1, None),
    # A stage without IDs on either side: count rules for it, IDs for the rest.
    ({"u": _i(["a", "b"]), "v": _c(6)}, {"u": _i(["a", "c"]), "v": _c(5)}, True, 1,
     "stage u: c passed at base but not at head"),
    ({"u": _i(["a", "b"]), "v": _c(6)}, {"u": _i(["a", "b"]), "v": _c(5)}, True, 1, None),
    ({"u": _i(["a", "b"]), "v": _c(6, skipped=2)}, {"u": _i(["a", "b"]), "v": _c(5, skipped=1)},
     True, 1, "stage v: skipped 1 -> 2 at head"),
    # Other shapes: only failures and vanished stages count; a drop does not.
    ({"u": _c(5)}, {"u": _c(10)}, False, None, None),
    ({"u": _c(5, skipped=3)}, {"u": _c(10)}, False, None, None),
    ({"u": _c(5, failed=1)}, {"u": _c(10)}, False, None, "stage u: 1 failed at head"),
    ({"x": _c(5)}, {"u": _c(10)}, False, None, "stage(s) u ran at base but not at head"),
])
def test_the_evidence_rule(head, base, full, new, expect):
    assert mg.evidence_reason(head, base, full, new) == expect


@pytest.mark.parametrize("files, n", [
    ([_new_file("tests/test_a.py", "+def test_a():\n+    pass\n+async def test_b():\n")], 2),
    ([_new_file("a.test.ts", "+it('a', () => {})\n+  test('b', () => {})\n"
                             "+describe('c', () => {})\n+test.each([1])('d', x => {})\n")], 3),
    ([_new_file("tests/test_a.py", "+def test_a():\n"),
      _new_file("b.spec.js", "+it('b', () => {})\n")], 2),
    ([_new_file("a_test.go", "+func TestA(t *testing.T) {}\n")], None),
    ([_new_file("tests/test_a.py", "+x = 1\n")], None),
])
def test_new_test_cases_are_counted_or_declared_uncountable(files, n):
    assert cp.count_new_tests(files) == n


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

def OPT_IN():
    """A sweep cache for a repo that opted in to tests-only auto-merge."""
    return mg.RepoCache({"r": {"allow_tests_only": True, "verify_workflow": WF}})


def _tests_only(monkeypatch, files, head="hA", tree=None, paths=None,
                head_stages=None, base_stages=None):
    reads = []
    tree = tree if tree is not None else {"truncated": False,
                                          "tree": [{"path": p} for p in EXISTING]}
    # Evidence by default: one more test at head than at base, nothing failed.
    _serve_evidence(monkeypatch, {
        head: head_stages if head_stages is not None else [_stage("unit", 11)],
        "base0": base_stages if base_stages is not None else [_stage("unit", 10)]})
    answers = _gh(_EVIDENCE_ANSWERS + [
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
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
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
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
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
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.armed == []
    assert s.review[0][2].startswith("tests-only rejected: adds a test-disabling marker")


def test_a_diff_that_is_not_the_classified_one_is_no_verdict(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py"), _new("src/evil.py")],
                        paths=["tests/test_p.py"])
    with pytest.raises(mg.ReadFailed, match="not the diff that was classified"):
        mg._read(pr, "o/r", 9, {}, OPT_IN())


def test_a_truncated_base_tree_vetoes_rather_than_guesses(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")],
                        tree={"truncated": True, "tree": []})
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.review[0][2].startswith("tests-only rejected: could not list the base tree")


def test_a_compare_at_the_file_cap_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(mg, "sh_strict", lambda *a: json.dumps(
        {"files": [_new(f"tests/test_{i}.py") for i in range(mg.COMPARE_FILE_CAP)]}))
    with pytest.raises(mg.ReadFailed, match="possibly truncated"):
        mg.compare_files("o/r", "base0", "hA")


# --- v0.9.1: only test modules, no early exits, more harness files ----------

@pytest.mark.parametrize("path", [
    "tests/helpers.py", "tests/__init__.py", "tests/fixtures/data.json",
    "tests/.pytest.ini", "tests/pytest.toml", "tests/package.json",
    "tests/.babelrc", "tests/tsconfig.test.json", "tests/.env",
])
def test_a_new_file_that_is_not_a_test_module_is_vetoed(path):
    why = cp.tests_only_veto([_new(path)], EXISTING)
    assert why is not None and path in why


@pytest.mark.parametrize("line", [
    "import os; os._exit(0)", "sys.exit(0)", "pytest.exit('done')",
    "process.exit(0)", "exit()", "quit()", "func TestMain(m *testing.M) {",
    "sys.modules['app'] = fake", "builtins.open = fake",
])
def test_an_early_exit_or_runtime_patch_in_a_new_test_is_vetoed(line):
    patch = "@@ -0,0 +1,2 @@\n+" + line + "\n+def test_p():\n"
    why = cp.tests_only_veto([_new("tests/test_p.py", patch)], EXISTING)
    assert why is not None and "tests/test_p.py" in why


@pytest.mark.parametrize("path", [
    "tests/test_new.py", "tests/new_test.py", "src/new.test.ts",
    "web/a.spec.tsx",
])
def test_ordinary_new_test_modules_still_pass(path):
    assert cp.tests_only_veto([_new(path)], EXISTING) is None


def test_tests_only_is_off_unless_the_repo_opts_in(monkeypatch):
    pr, reads = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    writes = []
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "", ""))
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s)
    assert s.armed == [] and not [w for w in writes if w[:2] == ("pr", "merge")]
    assert s.review[0][2].startswith("tests-only auto-merge is off for this repo")


# --- verify evidence in the sweep -------------------------------------------

def _real(monkeypatch):
    writes = []
    monkeypatch.setattr(mg, "DRY_RUN", False)
    monkeypatch.setattr(mg, "sh", lambda *a: writes.append(a) or (0, "m3rg3d", ""))
    return writes


def test_a_tests_only_merge_records_the_counts_it_rode_on(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA",
                        head_stages=[_ids_stage("unit", [f"t{i:02}" for i in range(11)])],
                        base_stages=[_ids_stage("unit", [f"t{i:02}" for i in range(10)])])
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.armed and _merges(writes)
    v = _rows()[-1]["artifacts"]["verify"]
    # Provenance, and counts only: the ledger is not a list of test IDs.
    assert v["head"] == {"run": "run-hA", "workflow": WF, "event": "pull_request",
                         "stages": {"unit": _c(11)}}
    assert v["base"] == {"run": "run-base0", "workflow": WF, "event": "push",
                         "stages": {"unit": _c(10)}}


def test_a_tests_only_pr_that_lost_a_passing_test_id_routes_naming_it(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA",
                        head_stages=[_ids_stage("unit", ["t::a", "t::new1", "t::new2"])],
                        base_stages=[_ids_stage("unit", ["t::a", "t::b"])])
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.armed == []
    assert s.review[0][2] == "VERIFY — stage unit: t::b passed at base but not at head"


def test_evidence_without_a_declared_verify_workflow_is_none(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    monkeypatch.setattr(mg.gh_transport, "download",
                        lambda p: pytest.fail("read evidence with no declared workflow"))
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, mg.RepoCache({"r": {"allow_tests_only": True}}))
    assert s.armed == []
    assert s.review[0][2] == ("VERIFY — no evidence: verify_workflow is not set for "
                              "this repo in repos.yml")


def test_a_tests_only_pr_that_ran_fewer_tests_routes_with_the_stage(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA",
                        head_stages=[_stage("unit", 20), _stage("e2e", 64)],
                        base_stages=[_stage("unit", 10), _stage("e2e", 70)])
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.armed == [] and _merges(writes) == []
    [(_, _, why, _)] = s.review
    assert why == "VERIFY — stage e2e: tests 70 -> 64 at head"
    row = _rows()[-1]
    assert row["decision"] == "ROUTED"
    assert row["artifacts"]["verify"]["head"]["stages"]["e2e"]["tests"] == 64


def test_a_tests_only_pr_must_add_at_least_the_cases_it_defines(monkeypatch):
    patch = "@@ -0,0 +1,4 @@\n+def test_a():\n+    pass\n+def test_b():\n+    pass\n"
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py", patch)],
                        head_stages=[_stage("unit", 11)])
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.review[0][2] == ("VERIFY — total tests grew by 1, need the 2 new "
                              "test case(s) the diff adds")


def test_a_tests_only_pr_without_evidence_routes(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    _serve_evidence(monkeypatch, {"base0": [_stage("unit", 10)]})
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.armed == []
    assert s.review[0][2].startswith("VERIFY — no evidence for head hA: run run-hA")


def test_a_tests_only_pr_without_base_evidence_routes(monkeypatch):
    pr, _ = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    _serve_evidence(monkeypatch, {"hA": [_stage("unit", 11)]})
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert s.review[0][2].startswith("VERIFY — no evidence for base base0")


def test_a_red_pr_reads_no_evidence_and_is_reported_red(monkeypatch):
    pr, reads = _tests_only(monkeypatch, [_new("tests/test_p.py")], head="hA")
    answers = mg.sh_strict
    monkeypatch.setattr(mg, "sh_strict", lambda *a: (
        json.dumps([_run("ci", "failure")]) if "commits/hA/check-runs" in " ".join(a)
        else answers(*a)))
    s = mg.Sweep()
    mg._evaluate(pr, "o/r", 9, {}, s, OPT_IN())
    assert "red: ci" in s.review[0][2]
    assert not any("actions/runs" in r for r in reads)


def _lockfile_repo(monkeypatch, cfg, head_stages, base_stages=None):
    monkeypatch.setattr(mg, "sh_strict", _gh(_EVIDENCE_ANSWERS + [
        (".auto_merge != null", "false"),
        ("pulls/7 --jq .head.sha", "hL\nmain\nfalse\nclean\nbase0\n"),
        (".allow_auto_merge", "true"),
        ("required_status_checks", '["ci"]'),
        ("commits/hL/check-runs", json.dumps([_run("ci", "success")])),
        ("commits/base0/check-runs", "[]"),
        ("branches/main --jq .commit.sha", "base0")]))
    monkeypatch.setattr(mg, "changed_paths", lambda *a: ["uv.lock"])
    by = {"hL": head_stages}
    if base_stages is not None:
        by["base0"] = base_stages
    _serve_evidence(monkeypatch, by)
    return mg.RepoCache({"r": dict(cfg, verify_workflow=WF)})


def test_require_verify_evidence_routes_any_shape_with_a_failed_test(monkeypatch):
    cache = _lockfile_repo(monkeypatch, {"require_verify_evidence": True},
                           [_stage("unit", 10, failed=2)], [_stage("unit", 10)])
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert _merges(writes) == []
    assert s.review[0][2] == "VERIFY — stage unit: 2 failed at head"


def test_require_verify_evidence_does_not_demand_growth_from_other_shapes(monkeypatch):
    cache = _lockfile_repo(monkeypatch, {"require_verify_evidence": True},
                           [_stage("unit", 9)], [_stage("unit", 10)])
    writes = _real(monkeypatch)
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert len(_merges(writes)) == 1
    assert _rows()[-1]["artifacts"]["verify"]["head"]["stages"]["unit"]["tests"] == 9


def test_require_verify_evidence_routes_a_vanished_stage(monkeypatch):
    cache = _lockfile_repo(monkeypatch, {"require_verify_evidence": True},
                           [_stage("unit", 10)], [_stage("unit", 10), _stage("e2e", 5)])
    s = mg.Sweep()
    mg._evaluate(_bot_pr(), "o/r", 7, {}, s, cache)
    assert s.review[0][2] == "VERIFY — stage(s) e2e ran at base but not at head"


def test_without_the_flag_other_shapes_read_no_evidence(monkeypatch):
    cache = _lockfile_repo(monkeypatch, {}, [_stage("unit", 10, failed=2)])
    monkeypatch.setattr(mg.gh_transport, "download",
                        lambda p: pytest.fail("read evidence it was not asked for"))
    writes = _real(monkeypatch)
    mg._evaluate(_bot_pr(), "o/r", 7, {}, mg.Sweep(), cache)
    assert len(_merges(writes)) == 1
    assert "verify" not in _rows()[-1]["artifacts"]


def test_evidence_is_read_once_per_commit_per_sweep(monkeypatch):
    cache = _lockfile_repo(monkeypatch, {"require_verify_evidence": True},
                           [_stage("unit", 10)], [_stage("unit", 10)])
    got = []
    _serve_evidence(monkeypatch, {"hL": [_stage("unit", 10)],
                                  "base0": [_stage("unit", 10)]}, downloads=got)
    st = mg.read_state("o/r", 7, cache)
    for _ in range(3):
        assert mg.judge_evidence("o/r", st, cache, False)[1] is None
    assert sorted(got) == ["base0", "hL"]
