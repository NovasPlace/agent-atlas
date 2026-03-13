"""Spreading Activation (Priming) — Collins & Loftus, 1975.

When a memory is recalled, linked memories receive temporary activation
boosts that decay exponentially across hops. Priming is ephemeral —
it does not survive instance death, analogous to short-term neural
facilitation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Cortex, Memory

# Cascade retains 70% per hop (paper §6)
CASCADE_FACTOR = 0.70
DEFAULT_BOOST = 0.15
MAX_BOOST = 0.50
DEFAULT_TTL = 300       # 5 minutes
DEFAULT_MAX_HOPS = 3
MIN_CASCADE_BOOST = 0.001


@dataclass
class Activation:
    """A temporary priming activation on a memory."""
    memory_id: str
    boost: float
    expires_at: float


class PrimingEngine:
    """In-memory spreading activation tracker.

    Args:
        cortex: Reference to the Cortex engine for linked_ids lookup.
        ttl: Seconds before an activation expires.
    """

    def __init__(self, cortex: Cortex | None = None, ttl: float = DEFAULT_TTL):
        self._cortex = cortex
        self._ttl = ttl
        self._activations: dict[str, Activation] = {}

    def prime(
        self,
        memory_id: str,
        boost: float = DEFAULT_BOOST,
        max_hops: int = DEFAULT_MAX_HOPS,
    ) -> int:
        """Spread activation from a memory through its linked neighbors.

        Cascade: hop 0 = boost, hop 1 = 70%, hop 2 = 49%, hop 3 = 34%.
        Returns count of memories primed.
        """
        if not self._cortex:
            return 0

        visited: set[str] = set()
        queue: list[tuple[str, float, int]] = [(memory_id, boost, 0)]
        primed_count = 0
        now = time.time()

        while queue:
            mid, current_boost, hop = queue.pop(0)

            if mid in visited or hop > max_hops:
                continue
            visited.add(mid)

            existing = self._activations.get(mid)
            new_boost = min(MAX_BOOST, (existing.boost if existing else 0) + current_boost)

            self._activations[mid] = Activation(
                memory_id=mid,
                boost=new_boost,
                expires_at=now + self._ttl,
            )
            primed_count += 1

            # Cascade to linked memories
            mem = self._cortex.get(mid)
            if mem and hop < max_hops:
                next_boost = current_boost * CASCADE_FACTOR
                if next_boost > MIN_CASCADE_BOOST:
                    for linked_id in mem.linked_ids:
                        if linked_id not in visited:
                            queue.append((linked_id, next_boost, hop + 1))

        return primed_count

    def get_boost(self, memory_id: str) -> float:
        """Current priming boost for a memory. 0 if expired or not primed."""
        act = self._activations.get(memory_id)
        if not act:
            return 0.0
        if time.time() > act.expires_at:
            del self._activations[memory_id]
            return 0.0
        return act.boost

    def expire(self) -> int:
        """Remove expired activations. Returns count removed."""
        now = time.time()
        expired = [mid for mid, act in self._activations.items() if now > act.expires_at]
        for mid in expired:
            del self._activations[mid]
        return len(expired)

    def primed_recall(self, query: str, limit: int = 20) -> list[Memory]:
        """Recall with priming boost applied to importance ranking."""
        if not self._cortex:
            return []

        self.expire()
        memories = self._cortex.recall(query, limit=limit * 2)

        boosted = []
        for mem in memories:
            boost = self.get_boost(mem.id)
            effective = mem.importance * (1.0 + boost)
            boosted.append((mem, effective))

        boosted.sort(key=lambda x: x[1], reverse=True)
        return [m for m, _ in boosted[:limit]]

    def active_count(self) -> int:
        """Count of currently active (non-expired) activations."""
        self.expire()
        return len(self._activations)

    def clear(self) -> None:
        """Clear all activations."""
        self._activations.clear()
