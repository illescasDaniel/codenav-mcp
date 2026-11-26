"""The refactor fuzzer on a small fixed-seed budget (slow: `uv run pytest -m fuzz tests/test_fuzz_refactors.py`)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from hardening import fuzz_refactors
from hardening.fuzz_checks import load_corpus


pytestmark = pytest.mark.fuzz

# One fast project, a few cases of every kind: rename (default and sites=all), change_signature, move, delete.
SEED = 7
COUNT = 8


@pytest.mark.timeout(900)
def test_fixed_seed_budget_holds_every_invariant(tmp_path: Path) -> None:
	project = load_corpus(fuzz_refactors.CORPUS_FILE)["tomlkit"]
	if not (project.path / ".venv").is_dir():
		pytest.skip(
			f"corpus project missing at {project.path}; run `uv run python -m hardening.fuzz_refactors --setup`"
		)
	args = argparse.Namespace(seed=SEED, count=COUNT, projects="tomlkit", ops="", out=str(tmp_path))
	results = asyncio.run(fuzz_refactors.fuzz(args))
	assert len(results) == COUNT
	failing = [
		(r.id, r.op, r.symbol, r.notes[:2]) for r in results if "fail" in r.checks.values() or r.status == "crash"
	]
	assert not failing, failing
	assert fuzz_refactors.dirty(project) == []
