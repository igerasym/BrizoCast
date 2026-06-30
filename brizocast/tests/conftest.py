"""Shared pytest fixtures and Hypothesis profile registration.

Hypothesis does not read its settings from ``pyproject.toml``, so the project's
defaults are registered here as named profiles and loaded at import time. Every
property-based test in the suite therefore runs a minimum of 100 examples
(per the design's correctness-property convention).
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

# Default profile used for local development and CI: at least 100 examples per
# property, with no per-example deadline (property tests may compose slow pure
# functions and we care about coverage, not latency).
settings.register_profile(
    "default",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# A heavier profile for thorough/nightly runs.
settings.register_profile(
    "thorough",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# A quick profile for fast iteration; still respects the spec by never dropping
# below the configured floor when the default profile is selected.
settings.register_profile(
    "dev",
    max_examples=100,
    deadline=None,
)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))
