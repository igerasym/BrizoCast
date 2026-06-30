"""Shared scoring value objects (pure, no I/O).

Holds the small, reusable scoring value object that the activity scorers and
the explainable-alert formatter both depend on: :class:`FactorContribution`,
the per-factor contribution to a weighted score.

The full scoring machinery — ``ScoreCategory`` (task 2.5), the ``SurfScorer``
factor curves, ``ScoreBreakdown``, and ``ScoreResult`` (task 2.7) — lives with
the scoring engine and the surf activity. This module contributes only the
shared contribution type so ports and later scoring code can reference a common
shape without importing the scorer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FactorContribution(BaseModel):
    """One factor's contribution to a weighted score.

    ``value`` is the factor's normalized sub-score in ``[0, 1]`` and ``weight``
    is its share of the overall score, also in ``[0, 1]``. :attr:`weighted`
    returns the product, i.e. the factor's actual contribution to the weighted
    total.
    """

    model_config = ConfigDict(frozen=True)

    value: float = Field(ge=0.0, le=1.0, description="Normalized factor sub-score in [0, 1].")
    weight: float = Field(ge=0.0, le=1.0, description="Factor weight in [0, 1].")

    @property
    def weighted(self) -> float:
        """Return ``value * weight`` — this factor's contribution to the total."""

        return self.value * self.weight
