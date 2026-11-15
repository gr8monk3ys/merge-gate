"""Property tests for evidence_reason: what a tests-only PR must show to merge.

The rule needed three fixes in a row (0.10.3-0.10.5), each a way to look like
a new test without adding one. These generate base/head report pairs instead
of listing cases, so the next such way is found here, not in a review.
"""
from hypothesis import given, strategies as st

import merge_gate as mg

ID = st.text("abcdefgh", min_size=1, max_size=3)


@st.composite
def stage(draw, ids=None):
    ids = sorted(draw(st.sets(ID, min_size=1, max_size=6))) if ids is None else ids
    reruns = draw(st.integers(0, 4))          # passes beyond one per ID
    skipped = draw(st.integers(0, 2))
    passed = len(ids) + reruns
    return {"tests": passed + skipped, "passed": passed, "failed": 0,
            "skipped": skipped, "passed_ids": ids}


reports = st.dictionaries(st.sampled_from(["unit", "e2e", "api"]), stage(),
                          min_size=1, max_size=3)


def rerun_only(draw, base):
    """Head = base with the same IDs, only more reruns of them."""
    head = {}
    for name, b in base.items():
        extra = draw(st.integers(0, 5))
        head[name] = dict(b, tests=b["tests"] + extra, passed=b["passed"] + extra)
    return head


@given(st.data(), reports, st.one_of(st.none(), st.integers(1, 5)))
def test_rerunning_existing_tests_never_licenses_a_merge(data, base, new):
    head = rerun_only(data.draw, base)
    assert mg.evidence_reason(head, base, True, new) is not None


@given(st.data(), reports)
def test_renaming_reruns_into_new_ids_never_licenses_a_merge(data, base):
    head = {}
    for name, b in base.items():
        split = min(data.draw(st.integers(0, 3)), b["passed"] - len(b["passed_ids"]))
        fresh = [f"z{i}" for i in range(split)]       # "z" sorts after ID chars
        head[name] = dict(b, passed_ids=sorted(b["passed_ids"] + fresh))
    assert mg.evidence_reason(head, base, True, None) is not None


@given(st.data(), reports, st.integers(1, 5))
def test_k_genuinely_new_tests_license_exactly_when_k_meets_the_need(data, base, need):
    name = data.draw(st.sampled_from(sorted(base)))
    k = data.draw(st.integers(0, 6))
    b = base[name]
    head = dict(base)
    head[name] = dict(b, tests=b["tests"] + k, passed=b["passed"] + k,
                      passed_ids=sorted(b["passed_ids"] + [f"z{i}" for i in range(k)]))
    assert (mg.evidence_reason(head, base, True, need) is None) == (k >= need)


@given(st.data(), reports)
def test_a_license_implies_nothing_regressed(data, base):
    head = {}
    for name, b in base.items():
        drop = data.draw(st.booleans())
        ids = b["passed_ids"][1:] if drop and len(b["passed_ids"]) > 1 else b["passed_ids"]
        k = data.draw(st.integers(0, 3))
        ids = sorted(ids + [f"z{i}" for i in range(k)])
        fail = data.draw(st.integers(0, 1))
        head[name] = {"tests": b["tests"] + k + fail, "passed": len(ids) + b["passed"] - len(b["passed_ids"]),
                      "failed": fail, "skipped": b["skipped"], "passed_ids": ids}
        head[name]["tests"] = head[name]["passed"] + fail + b["skipped"]
    if mg.evidence_reason(head, base, True, None) is None:
        for name, b in base.items():
            h = head[name]
            assert h["failed"] == 0
            assert set(b["passed_ids"]) <= set(h["passed_ids"])
            assert h["tests"] >= b["tests"] and h["passed"] >= b["passed"]
