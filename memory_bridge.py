"""Memory Bridge — Connects ~/.gemini/memory/ scripts to CortexDB.

Bridges the flat-file tiered memory system (hot/warm/archive) with
CortexDB's biologically-inspired cognitive engine. All operations
go through a shared Cortex instance backed by a single SQLite DB.

Usage:
    from memory_bridge import get_bridge
    bridge = get_bridge()
    bridge.store_project_state("Reaper", file_count=17, status="Active")
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

# CortexDB import via sys.path
_CORTEX_ROOT = Path(os.path.expanduser(
    os.environ.get("AGENT_CORTEX_ROOT", os.path.expanduser("~/.cortexdb"))
))
if str(_CORTEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORTEX_ROOT))

from cortex.engine import Cortex, Memory  # noqa: E402
from cortex.priming import PrimingEngine  # noqa: E402

# Default shared database path
DEFAULT_DB_PATH = os.path.expanduser("~/.cortexdb/agent_system.db")

# Singleton instance
_bridge_instance: MemoryBridge | None = None


class MemoryBridge:
    """Bridge between flat-file memory and CortexDB cognitive engine.

    Provides typed methods for storing project states, lessons,
    and retrieving context-aware memories via priming.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self._cortex = Cortex(db_path)
        self._priming = PrimingEngine(self._cortex, ttl=300)

    # ── Project State ────────────────────────────────────

    def store_project_state(
        self,
        project_name: str,
        file_count: int,
        status: str,
        location: str = "",
    ) -> Memory:
        """Store a project state snapshot as a semantic memory."""
        content = (
            f"Project '{project_name}' has {file_count} key files. "
            f"Status: {status}."
        )
        if location:
            content += f" Location: {location}"

        return self._cortex.remember(
            content,
            type="semantic",
            tags=["project-state", _slugify(project_name)],
            importance=0.4,
            emotion="neutral",
            source="experienced",
            context=f"memory_sync auto-snapshot for {project_name}",
        )

    def get_project_history(
        self, project_name: str, limit: int = 10
    ) -> list[Memory]:
        """Retrieve recent state snapshots for a project."""
        return self._cortex.recall(
            f"project {project_name}",
            limit=limit,
            min_importance=0.0,
        )

    # ── Lesson Storage ───────────────────────────────────

    def store_lesson(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: float = 0.7,
        emotion: str = "frustration",
    ) -> Memory:
        """Store an engineering lesson as a procedural memory.

        Lessons default to high importance and frustration emotion
        (negativity bias — failures are more important to remember).
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
            context="agent failure memory",
        )

    def recall_lessons(
        self, task_context: str, limit: int = 5
    ) -> list[Memory]:
        """Surface relevant lessons for a task using FTS + priming.

        Multi-stage retrieval:
        1. FTS search for direct term matches
        2. Word-by-word FTS fallback
        3. Tag-based matching
        4. Recency fallback (most important lessons)
        """
        # Stage 1: Direct FTS search
        direct = self._cortex.recall(task_context, limit=limit * 2)
        lessons = [m for m in direct if "lesson" in m.tags]

        # Stage 2: Word-by-word FTS fallback
        if not lessons:
            words = [w for w in task_context.split() if len(w) > 3]
            seen_ids: set[str] = set()
            for word in words:
                hits = self._cortex.recall(word, limit=limit)
                for h in hits:
                    if "lesson" in h.tags and h.id not in seen_ids:
                        lessons.append(h)
                        seen_ids.add(h.id)

        # Stage 3: Tag-based matching
        if not lessons:
            all_lessons = self.get_all_lessons(limit=limit * 3)
            words_lower = {w.lower() for w in task_context.split() if len(w) > 2}
            for lesson in all_lessons:
                tag_set = {t.lower() for t in lesson.tags}
                if tag_set & words_lower:
                    lessons.append(lesson)

        # Stage 4: Recency fallback
        if not lessons:
            lessons = self.get_all_lessons(limit=limit)

        if not lessons:
            return []

        # Prime the top result to cascade through linked memories
        self._priming.prime(lessons[0].id, boost=0.15, max_hops=2)

        # Re-query with priming active
        primed = self._priming.primed_recall(task_context, limit=limit)
        primed_lessons = [m for m in primed if "lesson" in m.tags]

        return primed_lessons if primed_lessons else lessons[:limit]

    def reinforce_lesson(self, memory_id: str) -> Memory | None:
        """Reinforce a lesson that prevented a real failure.

        Bumps importance and access count, making the lesson
        more resistant to Ebbinghaus decay.
        """
        mem = self._cortex.get(memory_id)
        if not mem:
            return None

        # Bump importance (cap at 1.0)
        new_importance = min(1.0, mem.importance + 0.1)

        with self._cortex._lock:
            self._cortex._conn.execute(
                "UPDATE memories SET importance = ?, last_accessed = ?, "
                "access_count = access_count + 1, updated_at = ? "
                "WHERE id = ?",
                (new_importance, time.time(), time.time(), memory_id),
            )
            self._cortex._conn.commit()

        return self._cortex.get(memory_id)

    def get_all_lessons(self, limit: int = 50) -> list[Memory]:
        """Retrieve all lesson memories sorted by importance."""
        all_memories = self._cortex.list_all(limit=limit * 3)
        lessons = [m for m in all_memories if "lesson" in m.tags]
        lessons.sort(key=lambda m: m.importance, reverse=True)
        return lessons[:limit]

    # ── Migration ────────────────────────────────────────

    def import_hot_lessons(self, hot_path: str | None = None) -> int:
        """One-time migration: parse RECENT LESSONS from hot.md into CortexDB.

        Returns count of lessons imported.
        """
        if hot_path is None:
            hot_path = str(Path(__file__).parent / "hot.md")

        hot_file = Path(hot_path)
        if not hot_file.exists():
            return 0

        content = hot_file.read_text()
        in_lessons = False
        imported = 0

        for line in content.splitlines():
            stripped = line.strip()

            if "## RECENT LESSONS" in stripped:
                in_lessons = True
                continue

            if in_lessons and stripped.startswith("## "):
                break

            if in_lessons and stripped.startswith("- "):
                lesson_text = stripped[2:].strip()
                if lesson_text:
                    # Check if already imported (avoid duplicates)
                    existing = self._cortex.recall(lesson_text[:50], limit=1)
                    already = any(
                        "lesson" in m.tags and m.content[:50] == lesson_text[:50]
                        for m in existing
                    )
                    if not already:
                        self.store_lesson(lesson_text)
                        imported += 1

        return imported

    def export_hot_lessons(self, limit: int = 10) -> str:
        """Generate RECENT LESSONS markdown from top CortexDB lessons.

        Returns formatted markdown section for hot.md.
        """
        lessons = self.get_all_lessons(limit=limit)
        if not lessons:
            return "## RECENT LESSONS\n\n(none)\n"

        lines = ["## RECENT LESSONS\n"]
        for lesson in lessons:
            lines.append(f"- {lesson.content}")
        lines.append("")

        return "\n".join(lines)

    # ── Stats ────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Memory bridge statistics."""
        cx_stats = self._cortex.stats()
        lessons = self.get_all_lessons(limit=200)
        return {
            "cortex": cx_stats,
            "lessons": {
                "total": len(lessons),
                "avg_importance": (
                    sum(l.importance for l in lessons) / len(lessons)
                    if lessons else 0
                ),
                "emotions": _count_field(lessons, "emotion"),
            },
            "priming_active": self._priming.active_count(),
        }

    # ── Lifecycle ────────────────────────────────────────

    @property
    def cortex(self) -> Cortex:
        """Direct access to the underlying Cortex engine."""
        return self._cortex

    @property
    def priming(self) -> PrimingEngine:
        """Direct access to the priming engine."""
        return self._priming

    def close(self) -> None:
        """Close the CortexDB connection."""
        self._cortex.close()


# ── Helpers ──────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Convert project name to slug tag."""
    return name.lower().replace(" ", "-").replace("_", "-")


def _count_field(memories: list[Memory], field: str) -> dict[str, int]:
    """Count occurrences of a field value across memories."""
    counts: dict[str, int] = {}
    for m in memories:
        val = getattr(m, field, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return counts


def get_bridge(db_path: str = DEFAULT_DB_PATH) -> MemoryBridge:
    """Get or create the singleton MemoryBridge instance."""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = MemoryBridge(db_path)
    return _bridge_instance
