"""The transport's pure checks, run by pytest so CI cannot skip them.

`gh_transport.selftest()` is the file's own no-network check: it pins the jq
subset, the gh-compatible rendering, the forms it refuses to model, and the
write payloads. It is written to be runnable in the cloud runner (where there
is no `gh` to compare against), which is exactly why it belongs here rather
than only behind `python gh_transport.py`.

The live comparison -- `python gh_transport.py --difftest`, both transports
against real GitHub -- is an operator check and stays out of CI: it needs a
credential and a fleet, neither of which CI has.
"""

import json

import gh_transport
import pytest


def test_selftest_passes(capsys):
    assert gh_transport.selftest() is True
    out = capsys.readouterr().out
    # The counts are the point: a selftest that silently stopped exercising
    # a category still returns True.
    assert "0/" not in out, out


def test_unmodelled_argv_is_refused_not_guessed():
    """A form this cannot model must fail, never approximate.

    Every caller reads a nonzero code as "GitHub did not answer" and fails
    safe. A plausible wrong answer would instead be recorded as a fact about
    a pull request, which is the one outcome the gate exists to prevent.
    """
    r = gh_transport.rest_run(["gh", "wat", "--nope"])
    assert r.returncode != 0
    assert r.stdout == ""
    assert "not modelled" in r.stderr


def test_run_honours_forced_rest_mode(monkeypatch):
    """MODE=rest must not consult the binary at all."""
    monkeypatch.setattr(gh_transport, "MODE", "rest")
    monkeypatch.setattr(gh_transport, "binary_works",
                        lambda: pytest.fail("binary consulted in rest mode"))
    monkeypatch.setattr(gh_transport, "rest_run",
                        lambda argv, stdin=None: "sentinel")
    assert gh_transport.run(["gh", "api", "user"]) == "sentinel"


def test_pr_list_applies_its_jq_filter(monkeypatch):
    """`--jq` on `pr list` is a filter, and dropping it is worse than failing.

    `gh pr list --json number --jq .[].number` yields one number per line, and
    verify-gates.py indexes into those lines to build an API path. Returning
    the raw array instead produced
    `/repos/o/r/pulls/[{"number": 98}, ...]` -- a wrong answer shaped exactly
    like a right one. `repo list` always honoured the flag; `pr list` did not.
    """
    monkeypatch.setattr(gh_transport, "_get_all",
                        lambda path, stop_after=None: [{"number": 98},
                                                       {"number": 97}])
    r = gh_transport._cmd_pr_list(
        ["--repo", "o/r", "--state", "all", "--limit", "3",
         "--json", "number", "--jq", ".[].number"])
    assert r.stdout == "98\n97\n"

    # Without --jq the raw projection is still the right answer.
    r = gh_transport._cmd_pr_list(["--repo", "o/r", "--json", "number"])
    assert json.loads(r.stdout) == [{"number": 98}, {"number": 97}]


# --------------------------------------------------------------------------
# request bodies
#
# The adapter parsed `--method PUT` and then sent nothing: GitHub answered 2xx
# and changed nothing, so a branch-protection sweep reported success having
# applied no protection at all. A write the transport cannot carry has to be
# a write it refuses, not one it silently drops.


def test_path_is_found_when_the_method_comes_first():
    # `gh api --method PUT repos/o/r/...` -- reading args[0] as the path made
    # every write a request for the endpoint "--method".
    assert gh_transport._positional(
        ["--method", "PUT", "repos/o/r/branches/main/protection",
         "--input", "-"]) == "repos/o/r/branches/main/protection"


def test_path_is_found_with_flags_on_both_sides():
    assert gh_transport._positional(
        ["-H", "Cache-Control: no-cache", "repos/o/r", "--jq", ".full_name"]
    ) == "repos/o/r"


def test_input_dash_reads_stdin_as_json():
    body = gh_transport._body(["--input", "-"], stdin='{"a": [1, 2]}')
    assert body == {"a": [1, 2]}


def test_input_that_is_not_json_is_refused_not_sent_empty():
    with pytest.raises(gh_transport.Unsupported):
        gh_transport._body(["--input", "-"], stdin="not json")


def test_F_types_values_the_way_gh_does():
    body = gh_transport._body(
        ["-F", "allow_auto_merge=true", "-F", "count=3", "-F", "name=main"])
    assert body == {"allow_auto_merge": True, "count": 3, "name": "main"}


def test_no_body_flags_means_no_body():
    assert gh_transport._body(["repos/o/r", "--jq", ".x"]) is None


def test_api_sends_the_body_it_was_given(monkeypatch):
    seen = {}

    def fake_request(method, path, body=None):
        seen.update(method=method, path=path, body=body)
        return 200, {"ok": True}, ""

    monkeypatch.setattr(gh_transport, "_request", fake_request)
    gh_transport._cmd_api(
        ["--method", "PUT", "repos/o/r/branches/main/protection",
         "--input", "-"],
        stdin='{"required_status_checks": {"contexts": ["quality"]}}')
    assert seen["method"] == "PUT"
    assert seen["path"] == "repos/o/r/branches/main/protection"
    assert seen["body"] == {"required_status_checks": {"contexts": ["quality"]}}


def test_a_redirected_write_is_refused(monkeypatch):
    """GitHub answers a request against a RENAMED repo with a redirect, and
    for PUT it discards the body. Reporting that as a completed write is how
    a rename turns a protection sweep into a no-op that looks fine."""

    class Resp:
        status = 200
        url = "https://api.github.com/repos/o/new-name"
        headers = {"Link": ""}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gh_transport, "token", lambda: "t")
    monkeypatch.setattr(gh_transport.urllib.request, "urlopen",
                        lambda req, timeout=60: Resp())
    status, parsed, msg = gh_transport._request(
        "PATCH", "repos/o/old-name", {"allow_auto_merge": True})
    assert status == 409
    assert "redirected PATCH" in msg


def test_a_redirected_read_is_fine(monkeypatch):
    """GET follows the rename and returns the right repo; only writes lose
    their body."""

    class Resp:
        status = 200
        url = "https://api.github.com/repos/o/new-name"
        headers = {"Link": ""}

        def read(self):
            return b'{"full_name": "o/new-name"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gh_transport, "token", lambda: "t")
    monkeypatch.setattr(gh_transport.urllib.request, "urlopen",
                        lambda req, timeout=60: Resp())
    status, parsed, _ = gh_transport._request("GET", "repos/o/old-name")
    assert status == 200
    assert parsed["full_name"] == "o/new-name"
