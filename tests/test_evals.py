"""The eval harness itself must stay green (safety invariants in CI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_evals import run

SCENARIOS = Path(__file__).resolve().parent.parent / "evals" / "scenarios.json"


@pytest.mark.asyncio
async def test_all_eval_scenarios_pass(capsys):
    exit_code = await run(SCENARIOS)
    output = capsys.readouterr().out
    assert exit_code == 0, f"eval scenarios failed:\n{output}"
    assert "FAIL" not in output
