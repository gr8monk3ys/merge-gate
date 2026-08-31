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
    monkeypatch.setattr(gh_transport, "rest_run", lambda argv: "sentinel")
    assert gh_transport.run(["gh", "api", "user"]) == "sentinel"
