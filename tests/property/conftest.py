"""Hypothesis profiles for the property tests.

`default` keeps the whole directory to a few seconds and is deterministic (`derandomize`), so a normal
`uv run pytest -q` never flakes. `fuzz` (HYPOTHESIS_PROFILE=fuzz) runs many more, random examples.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings


_CHECKS = [HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much]
settings.register_profile(
	"default", max_examples=40, derandomize=True, deadline=None, database=None, suppress_health_check=_CHECKS
)
settings.register_profile(
	"fuzz", max_examples=1500, derandomize=False, deadline=None, database=None, suppress_health_check=_CHECKS
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
