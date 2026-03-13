"""Cognitive Bias Engine — Systematic Distortions as First-Class Primitives.

Implements three biases from cognitive psychology (paper §7):
- Recency bias (§7.1): recent memories boosted exponentially
- Confirmation bias (§7.2): mood-congruent emotions amplified
- Availability heuristic (§7.3): frequently accessed = more significant

All biases composed via geometric mean to prevent single-bias dominance.
"""
from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Cortex, Memory


# Mood → emotion amplification map (paper Table 7.2)
CONFIRMATION_MAP: dict[str, dict[str, float]] = {
    "vigilant":    {"fear": 1.4, "surprise": 1.2, "frustration": 1.1},
    "agitated":    {"frustration": 1.4, "fear": 1.2},
    "exploratory": {"curiosity": 1.4, "surprise": 1.2},
    "confident":   {"satisfaction": 1.4, "curiosity": 1.1},
    "alert":       {"surprise": 1.4, "fear": 1.3},
    "neutral":     {},
}

# Mood → attention salience threshold (paper Table 7.5)
ATTENTION_THRESHOLDS: dict[str, float] = {
    "alert":       0.10,
    "vigilant":    0.15,
    "agitated":    0.20,
    "exploratory": 0.25,
    "neutral":     0.30,
    "confident":   0.35,
}

VALID_MOODS = frozenset(CONFIRMATION_MAP.keys())

# Recency bias half-life
RECENCY_HALF_LIFE_HOURS = 24.0


class CognitiveBiasEngine:
    """Applies cognitive biases to memory retrieval.

    Args:
        cortex: Reference to the Cortex engine for recall.
    """

    def __init__(self, cortex: Cortex | None = None):
        self._cortex = cortex

    def recency_bias(self, memory: Memory) -> float:
        """Exponentially decaying boost for recent memories.

        B = 1 + e^{-0.693 * t_h / T_half}
        1-hour-old → ~1.97×, 72-hour-old → ~1.12×
        """
        age_hours = max(0, (time.time() - memory.created_at) / 3600.0)
        return 1.0 + math.exp(-0.693 * age_hours / RECENCY_HALF_LIFE_HOURS)

    def confirmation_bias(self, memory: Memory, mood: str) -> float:
        """Mood-congruent emotion amplification."""
        if mood not in VALID_MOODS:
            return 1.0
        return CONFIRMATION_MAP.get(mood, {}).get(memory.emotion, 1.0)

    def availability_bias(self, memory: Memory) -> float:
        """Frequently accessed memories appear more significant.

        B = min(1.5, 1 + ln(1 + n_access) / 7)
        5 accesses ≈ 1.23×, 20 accesses ≈ 1.43×
        """
        return min(1.5, 1.0 + math.log(1 + memory.access_count) / 7.0)

    def composite_bias(self, memory: Memory, mood: str = "neutral") -> float:
        """Geometric mean of all three biases × base importance.

        I_biased = I_base * (B_recency * B_confirm * B_avail)^(1/3)
        """
        b_recency = self.recency_bias(memory)
        b_confirm = self.confirmation_bias(memory, mood)
        b_avail = self.availability_bias(memory)

        geometric = (b_recency * b_confirm * b_avail) ** (1.0 / 3.0)
        return memory.importance * geometric

    def biased_recall(
        self,
        query: str,
        mood: str = "neutral",
        limit: int = 20,
    ) -> list[Memory]:
        """Full-bias-stack recall: recency + confirmation + availability."""
        if not self._cortex:
            return []

        raw = self._cortex.recall(query, limit=limit * 3)
        scored = [(m, self.composite_bias(m, mood)) for m in raw]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [m for m, _ in scored[:limit]]

    def attention_gate(self, salience: float, mood: str = "neutral") -> bool:
        """Mood-adaptive threshold for working memory intake.

        Returns True if salience exceeds the mood-adjusted threshold.
        """
        threshold = ATTENTION_THRESHOLDS.get(mood, 0.30)
        return salience >= threshold
