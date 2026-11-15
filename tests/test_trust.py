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
