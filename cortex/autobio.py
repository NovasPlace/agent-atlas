"""Autobiographical Memory — Life Story & Identity Synthesis.

Constructs coherent narratives from cortex contents (paper §10).
The identity_summary() method produces a first-person self-description
from accumulated memory — the organism's self-concept.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Cortex, Memory

# Temporal periods for get_chapter()
PERIODS: dict[str, float] = {
    "last_hour":  3600,
    "today":      86400,
    "this_week":  604800,
    "this_month": 2592000,
    "all_time":   float("inf"),
}

TOP_MEMORY_LIMIT = 200


@dataclass
class Chapter:
    """A temporal segment of the life story."""
    period: str
    event_count: int
    top_themes: list[str]
    emotional_tone: str
    key_events: list[str]


@dataclass
class Intention:
    """A future-oriented memory — fires when trigger time passes."""
    id: str
    action: str
    trigger_time: float
    created_at: float
    fired: bool = False


class AutobiographicalMemory:
    """Constructs identity narratives from cortex contents.

    Args:
        cortex: Reference to the Cortex engine for memory queries.
    """

    def __init__(self, cortex: Cortex | None = None):
        self._cortex = cortex
        self._intentions: list[Intention] = []
        self._counter = 0

    def get_life_story(self, limit: int = 50) -> list[Memory]:
        """Chronological timeline of key memories, sorted by importance then time."""
        if not self._cortex:
            return []

        memories = self._cortex.list_all(limit=limit * 2)
        memories.sort(key=lambda m: m.importance, reverse=True)
        top = memories[:limit]
        top.sort(key=lambda m: m.created_at)
        return top

    def get_chapter(self, period: str = "today") -> Chapter:
        """Temporal window analysis: event count, themes, tone, key events."""
        empty = Chapter(
            period=period, event_count=0, top_themes=[],
            emotional_tone="neutral", key_events=[],
        )

        if not self._cortex:
            return empty

        window = PERIODS.get(period, PERIODS["today"])
        cutoff = time.time() - window if window != float("inf") else 0

        all_memories = self._cortex.list_all(limit=200)
        memories = [m for m in all_memories if m.created_at >= cutoff]

        if not memories:
            return empty

        # Top themes from tag frequency
        tag_counts: Counter[str] = Counter()
        for m in memories:
            tag_counts.update(m.tags)
        top_themes = [tag for tag, _ in tag_counts.most_common(5)]

        # Emotional tone — dominant non-neutral emotion
        emotion_counts: Counter[str] = Counter()
        for m in memories:
            if m.emotion != "neutral":
                emotion_counts[m.emotion] += 1
        emotional_tone = (
            emotion_counts.most_common(1)[0][0] if emotion_counts else "neutral"
        )

        # Key events — highest importance
        key = sorted(memories, key=lambda m: m.importance, reverse=True)[:5]
        key_events = [m.content[:100] for m in key]

        return Chapter(
            period=period,
            event_count=len(memories),
            top_themes=top_themes,
            emotional_tone=emotional_tone,
            key_events=key_events,
        )

    def identity_summary(self) -> str:
        """Synthesize a first-person identity statement from top memories.

        Analyzes top 200 memories for:
        - Core focus areas (top 5 recurring tags)
        - Dominant emotional signature (top 3 non-neutral emotions)
        - Knowledge inventory (procedural + semantic counts)
        """
        if not self._cortex:
            return "No memories available. Identity not yet formed."

        memories = self._cortex.list_all(limit=TOP_MEMORY_LIMIT)
        if not memories:
            return "No memories available. Identity not yet formed."

        # Focus areas from tags
        tag_counts: Counter[str] = Counter()
        for m in memories:
            tag_counts.update(m.tags)
        focus_areas = [tag for tag, _ in tag_counts.most_common(5)]

        # Emotional signature
        emotion_counts: Counter[str] = Counter()
        for m in memories:
            if m.emotion != "neutral":
                emotion_counts[m.emotion] += 1
        top_emotions = [e for e, _ in emotion_counts.most_common(3)]

        # Knowledge inventory
        procedural = sum(1 for m in memories if m.type == "procedural")
        semantic = sum(1 for m in memories if m.type == "semantic")

        # Build identity statement
        parts = [f"I am an organism with {len(memories)} core memories."]

        if focus_areas:
            parts.append(f"My focus areas: {', '.join(focus_areas)}.")

        if top_emotions:
            parts.append(
                f"Dominant emotional signature: {', '.join(top_emotions)}."
            )

        if procedural:
            parts.append(f"I have {procedural} learned procedures.")

        if semantic:
            parts.append(f"I hold {semantic} semantic facts.")

        return " ".join(parts)

    def intend(self, action: str, trigger_time: float) -> Intention:
        """Create a future-oriented memory (prospective memory).

        The intention fires when trigger_time passes. Call check_intentions()
        periodically to detect due intentions.
        """
        self._counter += 1
        intention = Intention(
            id=f"intent-{self._counter}",
            action=action,
            trigger_time=trigger_time,
            created_at=time.time(),
        )
        self._intentions.append(intention)
        return intention

    def check_intentions(self) -> list[Intention]:
        """Return and mark-as-fired any intentions whose trigger time has passed."""
        now = time.time()
        due = []
        for intent in self._intentions:
            if not intent.fired and now >= intent.trigger_time:
                intent.fired = True
                due.append(intent)
        return due

    def pending_intentions(self) -> list[Intention]:
        """List all unfired intentions."""
        return [i for i in self._intentions if not i.fired]
