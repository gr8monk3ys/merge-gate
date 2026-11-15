import os
import sys

import pytest

# The scripts live at the repo root and import each other by bare name.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """No test may append to the operator's real merge ledger."""
    monkeypatch.setenv("MERGE_GATE_LEDGER", str(tmp_path / "ledger.jsonl"))
    return tmp_path / "ledger.jsonl"
