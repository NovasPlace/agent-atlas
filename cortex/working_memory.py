"""Working Memory — Fixed-Capacity Sliding Window.

A salience-gated short-term buffer that holds the organism's immediate
awareness. Explicitly ephemeral — does not survive instance death.
Analogous to the contents of biological consciousness.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CAPACITY = 64

# Biological decay constants (Novel Invention #2 integration)
DECAY_BASE_HALF_LIFE_S = 300       # 5 minute base half-life
DECAY_SALIENCE_MULTIPLIER = 4.0    # High salience = longer half-life
DECAY_FLOOR = 0.02                 # Below this = eligible for sweep
SPACED_REP_BOOST = 0.20            # 20% half-life increase per recall

CATEGORIES = frozenset({
    "event", "decision", "outcome", "reflex",
    "alert", "goal", "dream", "metabolism",
})


@dataclass
class WorkingMemoryItem:
    """A single item in the working memory buffer."""
    id: str
    content: str
    category: str
    salience: float
    added_at: float
    metadata: dict[str, Any] = field(default_factory=dict)
    half_life_s: float = DECAY_BASE_HALF_LIFE_S
    recall_count: int = 0


class WorkingMemory:
    """Fixed-capacity sliding window with salience-based eviction.

    When at capacity, the lowest-salience item is evicted (not FIFO),
    unless the incoming item has lower salience than all existing items.

    Args:
        capacity: Maximum number of items. Default 64.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = max(1, capacity)
        self._items: list[WorkingMemoryItem] = []
        self._counter = 0

    def add(
        self,
        content: str,
        category: str = "event",
        salience: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> WorkingMemoryItem | None:
        """Add an item. Returns the item if added, None if rejected."""
        if not content or not content.strip():
            return None

        category = category if category in CATEGORIES else "event"
        salience = max(0.0, min(1.0, salience))

        self._counter += 1
        item = WorkingMemoryItem(
            id=f"wm-{self._counter}",
            content=content.strip(),
            category=category,
            salience=salience,
            added_at=time.time(),
            metadata=metadata or {},
        )

        if len(self._items) >= self._capacity:
            min_item = min(self._items, key=lambda x: x.salience)
            if item.salience <= min_item.salience:
                return None
            self._items.remove(min_item)

        self._items.append(item)
        return item

    def attend(
        self,
        category: str | None = None,
        min_salience: float = 0.0,
    ) -> list[WorkingMemoryItem]:
        """Filtered retrieval sorted by effective salience descending.

        Grants a spaced-repetition boost to retrieved items:
        each recall increases half_life by 20%, slowing future decay.
        """
        now = time.time()
        result = self._items
        if category:
            result = [i for i in result if i.category == category]
        if min_salience > 0:
            result = [i for i in result if self.decay_score(i, now) >= min_salience]

        # Spaced repetition: boost half-life on recall
        for item in result:
            item.recall_count += 1
            item.half_life_s *= (1.0 + SPACED_REP_BOOST)

        return sorted(result, key=lambda x: self.decay_score(x, now), reverse=True)

    def summarize(self) -> str:
        """Natural language context string for LLM prompt injection."""
        if not self._items:
            return "Working memory is empty."

        by_cat: dict[str, list[str]] = {}
        for item in sorted(self._items, key=lambda x: x.salience, reverse=True):
            by_cat.setdefault(item.category, []).append(item.content)

        parts = []
        for cat, items in by_cat.items():
            entries = "; ".join(items[:5])
            parts.append(f"[{cat}] {entries}")

        return " | ".join(parts)

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def capacity(self) -> int:
        return self._capacity

    def clear(self) -> None:
        """Clear all items from working memory."""
        self._items.clear()
        self._counter = 0

    # ── Biological Decay ──────────────────────────────────

    @staticmethod
    def decay_score(item: WorkingMemoryItem, now: float | None = None) -> float:
        """Compute current effective salience with biological decay.

        Uses exponential decay: effective = salience * 0.5^(age / half_life)
        Half-life scales with original salience (high salience decays slower).
        """
        if now is None:
            now = time.time()
        age_s = max(0.0, now - item.added_at)
        if age_s <= 0:
            return item.salience
        return item.salience * math.pow(0.5, age_s / item.half_life_s)

    def decay_sweep(self) -> int:
        """Remove items whose effective salience has decayed below threshold.

        Returns count of items removed.
        """
        now = time.time()
        before = len(self._items)
        self._items = [
            item for item in self._items
            if self.decay_score(item, now) >= DECAY_FLOOR
        ]
        return before - len(self._items)
