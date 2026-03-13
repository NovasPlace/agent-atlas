"""Lesson Engine — Self-improving failure memory via CortexDB.

Manages engineering lessons as CortexDB memories with reinforcement,
decay, priming, context-aware surfacing, and automatic consolidation.

Consolidation prevents negative constraint paralysis: when 3+ lessons
share a domain tag, they're merged into a single generalized constraint
so the agent's context isn't diluted by redundant specific rules.

Usage:
    from lesson_engine import LessonEngine
    engine = LessonEngine()
    engine.add("Always kill zombie processes before binding ports")
    relevant = engine.surface("deploying server on port 8080")
    engine.consolidate()  # Merge redundant lessons
"""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# CortexDB import via sys.path
_CORTEX_ROOT = Path(os.path.expanduser(
    "$AGENT_CORTEX_ROOT"
))
if str(_CORTEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORTEX_ROOT))

from cortex.engine import Cortex, Memory  # noqa: E402
from cortex.priming import PrimingEngine  # noqa: E402

DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")

# Lesson importance thresholds
CRITICAL_IMPORTANCE = 0.9       # Lessons tagged critical
HIGH_IMPORTANCE = 0.7           # Default lesson importance
REINFORCEMENT_DELTA = 0.1       # Importance bump per reinforcement
STALE_ACCESS_THRESHOLD = 3      # Below this access count = stale candidate
STALE_AGE_HOURS = 720           # 30 days without access = stale

# Consolidation parameters
CONSOLIDATION_THRESHOLD = 3     # Merge when N+ lessons share a domain tag
CONSOLIDATED_IMPORTANCE = 0.85  # Importance for the merged lesson
ARCHIVED_IMPORTANCE = 0.15      # Archived granular lessons decay fast

# Tags that represent structure (vs actionable domains like "security")
STRUCTURAL_TAGS = frozenset({"lesson", "reaper", "kill-lesson", "consolidated"})

# Minimum tag length to be considered a meaningful domain
MIN_TAG_LENGTH = 4

# Generic tags that should NEVER trigger consolidation — learned the hard way
GENERIC_TAG_BLOCKLIST = frozenset({
    "medium", "high", "low", "critical",
    "scaling", "optimization", "perf", "performance",
    "frontend", "backend", "ui", "dashboard",
    "emergent", "architecture", "deployment",
    "infra", "learning", "reasoning", "memory",
    "persistence", "storage", "database",
    "messaging", "protocol", "evolution", "blueprint",
    "auth", "hardening", "a2a",
})

# Domain tag → generalized constraint templates
DOMAIN_TEMPLATES: dict[str, str] = {
    "security": (
        "Security-critical: {count} processes were terminated for security "
        "violations. Do not spawn unauthorized network tools, scanners, or "
        "processes that access restricted resources. Specific examples: {examples}"
    ),
    "port_zombie": (
        "Port management: {count} zombie processes were killed holding ports. "
        "Always verify ports are free before binding. Kill existing listeners "
        "explicitly. Examples: {examples}"
    ),
    "runaway_cpu": (
        "Resource safety: {count} processes were killed for runaway CPU. "
        "Avoid unbounded loops, recursive operations without depth limits, "
        "or CPU-intensive operations without timeouts. Examples: {examples}"
    ),
    "hung_io": (
        "I/O safety: {count} processes hung on I/O and were killed. "
        "Always use timeouts on I/O operations. Pipe to files rather than "
        "streaming slow APIs directly. Examples: {examples}"
    ),
}

DEFAULT_TEMPLATE = (
    "{domain}: {count} related lessons consolidated. "
    "Avoid repeating these patterns. Examples: {examples}"
)


class LessonEngine:
    """Self-improving lesson management backed by CortexDB.

    Lessons are procedural memories with the 'lesson' tag.
    The engine provides:
    - Context-aware surfacing via FTS + priming
    - Reinforcement when lessons prevent failures
    - Staleness detection for lessons that aren't relevant
    - Automatic consolidation to prevent context dilution
    - Export to hot.md format for backward compatibility
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._cortex = Cortex(db_path)
        self._priming = PrimingEngine(self._cortex, ttl=300)

    def add(
        self,
        content: str,
        tags: list[str] | None = None,
        emotion: str = "frustration",
        importance: float = HIGH_IMPORTANCE,
        linked_ids: list[str] | None = None,
    ) -> Memory:
        """Add a new lesson. Returns the stored memory.

        Args:
            content: The lesson text.
            tags: Additional tags (always includes 'lesson').
            emotion: Emotional valence — frustration by default.
            importance: Initial importance (0.0-1.0).
            linked_ids: IDs of related memories for priming cascade.
        """
        all_tags = ["lesson"] + (tags or [])
        return self._cortex.remember(
            content,
            type="procedural",
            tags=all_tags,
            importance=importance,
            emotion=emotion,
            source="experienced",
            confidence=0.9,
            context="engineering lesson from session",
            linked_ids=linked_ids or [],
        )

    def surface(
        self,
        task_description: str,
        limit: int = 5,
        min_importance: float = 0.1,
    ) -> list[Memory]:
        """Surface relevant lessons for a task context.

        Multi-stage retrieval:
        1. FTS search for direct term matches
        2. Word-by-word FTS fallback (individual terms)
        3. Tag-based search on individual words
        4. Recency fallback (most recently accessed lessons)

        Results are group-capped: no single tag domain can contribute
        more than ceil(limit/2) results to prevent one category from
        monopolizing the context window.

        Top results are primed so related lessons cascade.

        Args:
            task_description: Natural language description of the task.
            limit: Maximum lessons to return.
            min_importance: Floor for importance filter.
        """
        # Stage 1: Full FTS search
        candidates = self._cortex.recall(
            task_description, limit=limit * 3, min_importance=min_importance
        )
        lessons = [m for m in candidates if "lesson" in m.tags]

        # Stage 2: Word-by-word FTS fallback
        if not lessons:
            words = [w for w in task_description.split() if len(w) > 3]
            seen_ids: set[str] = set()
            for word in words:
                hits = self._cortex.recall(word, limit=limit, min_importance=min_importance)
                for h in hits:
                    if "lesson" in h.tags and h.id not in seen_ids:
                        lessons.append(h)
                        seen_ids.add(h.id)

        # Stage 3: Tag-based matching
        if not lessons:
            all_lessons = self.get_all(limit=limit * 3)
            words_lower = {w.lower() for w in task_description.split() if len(w) > 2}
            for lesson in all_lessons:
                tag_set = {t.lower() for t in lesson.tags}
                if tag_set & words_lower:
                    lessons.append(lesson)

        # Stage 4: Recency fallback — return most important lessons
        if not lessons:
            lessons = self.get_all(limit=limit)

        if not lessons:
            return []

        # Group-cap: prevent one domain from monopolizing results
        lessons = _cap_by_group(lessons, limit)

        # Prime the top result for cascade
        self._priming.prime(lessons[0].id, boost=0.15, max_hops=2)

        # Re-query with priming boosts
        primed = self._priming.primed_recall(task_description, limit=limit * 2)
        primed_lessons = [m for m in primed if "lesson" in m.tags]

        if primed_lessons:
            primed_lessons = _cap_by_group(primed_lessons, limit)
            return primed_lessons[:limit]
        return lessons[:limit]

    def consolidate(self) -> list[Memory]:
        """Merge redundant lessons sharing a domain tag.

        When 3+ lessons share the same domain tag (security, port_zombie,
        etc), they get consolidated into one generalized constraint.
        The specific granular lessons have their importance reduced to
        ARCHIVED_IMPORTANCE so they decay naturally.

        Only tags passing the specificity gate are eligible:
        - Must be >= MIN_TAG_LENGTH characters
        - Must not be in GENERIC_TAG_BLOCKLIST
        - Must not be in STRUCTURAL_TAGS

        Returns list of newly created consolidated lesson memories.
        """
        all_lessons = self.get_all(limit=500)
        groups = _group_by_domain(all_lessons)

        created: list[Memory] = []
        for domain, members in groups.items():
            if not _is_consolidatable_tag(domain):
                continue

            if len(members) < CONSOLIDATION_THRESHOLD:
                continue

            # Skip if a consolidated lesson for this domain already exists
            if any("consolidated" in m.tags for m in members):
                continue

            # Extract short examples from member content
            examples = []
            for m in members:
                snippet = m.content[:60].strip()
                if snippet not in examples:
                    examples.append(snippet)

            template = DOMAIN_TEMPLATES.get(domain, DEFAULT_TEMPLATE)
            consolidated_text = template.format(
                domain=domain,
                count=len(members),
                examples="; ".join(examples[:5]),
            )

            # Create the consolidated lesson
            new_lesson = self.add(
                consolidated_text,
                tags=[domain, "consolidated"],
                importance=CONSOLIDATED_IMPORTANCE,
                emotion=members[0].emotion,
                linked_ids=[m.id for m in members[:10]],
            )
            created.append(new_lesson)

            # Archive the granular lessons
            self._archive_lessons(members)

        return created

    def purge_junk_consolidations(self) -> int:
        """Remove consolidated lessons created from generic/noisy tags.

        Deletes any consolidated lesson whose domain tag is in the
        blocklist or fails the specificity gate. Returns count removed.
        """
        all_lessons = self.get_all(limit=500)
        junk = []
        for lesson in all_lessons:
            if "consolidated" not in lesson.tags:
                continue
            domain_tags = [
                t for t in lesson.tags
                if t not in STRUCTURAL_TAGS and t != "consolidated"
            ]
            if not domain_tags or any(
                not _is_consolidatable_tag(t) for t in domain_tags
            ):
                junk.append(lesson)

        now = time.time()
        with self._cortex._lock:
            for lesson in junk:
                self._cortex._conn.execute(
                    "DELETE FROM memories WHERE id = ?", (lesson.id,)
                )
                self._cortex._conn.execute(
                    "DELETE FROM memories_fts WHERE content = ?",
                    (lesson.content,),
                )
            if junk:
                self._cortex._conn.commit()

        return len(junk)

    def _archive_lessons(self, lessons: list[Memory]) -> None:
        """Reduce importance of granular lessons after consolidation."""
        now = time.time()
        with self._cortex._lock:
            for lesson in lessons:
                self._cortex._conn.execute(
                    "UPDATE memories SET importance = ?, updated_at = ? "
                    "WHERE id = ? AND importance > ?",
                    (ARCHIVED_IMPORTANCE, now, lesson.id, ARCHIVED_IMPORTANCE),
                )
            self._cortex._conn.commit()

    def reinforce(self, lesson_id: str) -> Memory | None:
        """Reinforce a lesson that prevented a real failure.

        Bumps importance, updates access metadata. Makes the lesson
        more resistant to Ebbinghaus decay.
        """
        mem = self._cortex.get(lesson_id)
        if not mem or "lesson" not in mem.tags:
            return None

        new_importance = min(1.0, mem.importance + REINFORCEMENT_DELTA)
        now = time.time()

        with self._cortex._lock:
            self._cortex._conn.execute(
                "UPDATE memories SET importance = ?, last_accessed = ?, "
                "access_count = access_count + 1, updated_at = ? "
                "WHERE id = ?",
                (new_importance, now, now, lesson_id),
            )
            self._cortex._conn.commit()

        return self._cortex.get(lesson_id)

    def stale_check(self) -> list[Memory]:
        """Find stale lessons — low access count and old.

        Returns lessons that haven't been accessed in 30+ days
        with fewer than 3 accesses. These are candidates for
        consolidation or removal.
        """
        cutoff = time.time() - (STALE_AGE_HOURS * 3600)
        all_memories = self._cortex.list_all(limit=500)

        stale = [
            m for m in all_memories
            if "lesson" in m.tags
            and m.access_count < STALE_ACCESS_THRESHOLD
            and m.last_accessed < cutoff
            and not m.is_identity
            and not m.is_flashbulb
        ]

        stale.sort(key=lambda m: m.importance)
        return stale

    def get_all(self, limit: int = 50) -> list[Memory]:
        """Get all lessons sorted by importance (descending)."""
        all_memories = self._cortex.list_all(limit=limit * 3)
        lessons = [m for m in all_memories if "lesson" in m.tags]
        lessons.sort(key=lambda m: m.importance, reverse=True)
        return lessons[:limit]

    def export_hot(self, limit: int = 10) -> str:
        """Generate RECENT LESSONS section for hot.md.

        Gets top N lessons by importance and formats as markdown.
        Backward compatible with the existing hot.md format.
        """
        lessons = self.get_all(limit=limit)
        if not lessons:
            return "## RECENT LESSONS\n\n(none)\n"

        lines = ["## RECENT LESSONS\n"]
        for lesson in lessons:
            lines.append(f"- {lesson.content}")
        lines.append("")
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        """Lesson engine statistics."""
        all_lessons = self.get_all(limit=500)
        stale = self.stale_check()

        if not all_lessons:
            return {
                "total": 0,
                "stale": 0,
                "consolidated": 0,
                "avg_importance": 0.0,
                "avg_access_count": 0.0,
                "by_emotion": {},
            }

        consolidated = sum(1 for l in all_lessons if "consolidated" in l.tags)

        return {
            "total": len(all_lessons),
            "stale": len(stale),
            "consolidated": consolidated,
            "avg_importance": sum(l.importance for l in all_lessons) / len(all_lessons),
            "avg_access_count": sum(l.access_count for l in all_lessons) / len(all_lessons),
            "by_emotion": _count_emotions(all_lessons),
        }

    def close(self) -> None:
        """Close the CortexDB connection."""
        self._cortex.close()


# ── Helpers ────────────────────────────────────────────────

def _count_emotions(memories: list[Memory]) -> dict[str, int]:
    """Count emotion distribution across memories."""
    counts: dict[str, int] = {}
    for m in memories:
        counts[m.emotion] = counts.get(m.emotion, 0) + 1
    return counts


def _group_by_domain(lessons: list[Memory]) -> dict[str, list[Memory]]:
    """Group lessons by their domain tags (excluding structural tags)."""
    groups: dict[str, list[Memory]] = defaultdict(list)
    for lesson in lessons:
        domain_tags = [t for t in lesson.tags if t not in STRUCTURAL_TAGS]
        for tag in domain_tags:
            groups[tag].append(lesson)
    return dict(groups)


def _is_consolidatable_tag(tag: str) -> bool:
    """Check if a tag is specific enough to warrant consolidation."""
    if len(tag) < MIN_TAG_LENGTH:
        return False
    if tag in STRUCTURAL_TAGS or tag in GENERIC_TAG_BLOCKLIST:
        return False
    return True


def _cap_by_group(lessons: list[Memory], limit: int) -> list[Memory]:
    """Cap lessons per domain group to prevent one category from dominating.

    Each domain tag group can contribute at most ceil(limit/2) results.
    Lessons without domain tags are uncapped.
    """
    max_per_group = max(1, (limit + 1) // 2)
    group_counts: dict[str, int] = defaultdict(int)
    result: list[Memory] = []

    for lesson in lessons:
        domain_tags = [t for t in lesson.tags if t not in STRUCTURAL_TAGS]
        if not domain_tags:
            result.append(lesson)
            continue

        at_cap = any(group_counts[t] >= max_per_group for t in domain_tags)
        if not at_cap:
            result.append(lesson)
            for t in domain_tags:
                group_counts[t] += 1

    return result
