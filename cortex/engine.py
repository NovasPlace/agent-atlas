"""Cortex Memory Complex — Biologically-Inspired Cognitive Memory Engine.

Implements the Cortex Memory Complex architecture (Frost & Nirvash, 2026):
- 4 memory types (episodic, procedural, semantic, relational)
- Ebbinghaus forgetting curves with spaced repetition
- Flashbulb memory (decay immunity for high-emotion events)
- 6 emotional valences with retrieval boost multipliers
- Reconsolidation (confidence degradation on recall)
- Source monitoring (4 provenance tiers with confidence penalties)
- Identity-tagged wipe-proof memories
- Episodic → semantic consolidation
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ── Constants ──────────────────────────────────────────────

# Emotional valence boost multipliers (paper §4.4)
EMOTION_BOOSTS: dict[str, float] = {
    "fear": 2.0,
    "surprise": 1.8,
    "frustration": 1.5,
    "curiosity": 1.3,
    "satisfaction": 1.1,
    "neutral": 1.0,
}

# Source monitoring confidence penalties (paper §4.5)
SOURCE_PENALTIES: dict[str, float] = {
    "experienced": 1.00,
    "told": 0.85,
    "generated": 0.75,
    "inferred": 0.65,
}

MEMORY_TYPES = frozenset({"episodic", "procedural", "semantic", "relational"})
EMOTIONS = frozenset(EMOTION_BOOSTS.keys())
SOURCE_TYPES = frozenset(SOURCE_PENALTIES.keys())

# Ebbinghaus decay parameters (paper §4.1)
DECAY_BASE_STABILITY_S = 3600      # 1 hour base half-life
DECAY_FLOOR = 0.01                 # Below this → eligible for deletion

# Flashbulb criteria (paper §4.2, Brown & Kulik 1977)
FLASHBULB_EMOTIONS = frozenset({"fear", "surprise"})
FLASHBULB_IMPORTANCE_THRESHOLD = 0.8

# Consolidation parameters (paper §11.1)
CONSOLIDATION_AGE_HOURS = 72
CONSOLIDATION_IMPORTANCE_CEILING = 0.7

# Reconsolidation (paper §4.3, Nader 2003)
RECONSOLIDATION_FACTOR = 0.95      # 5% confidence loss per recall

# Synaptic pathway parameters (Novel Invention #1/#2 integration)
PATHWAY_STRENGTHEN_STEP = 0.1      # Hebbian co-recall increment
PATHWAY_MAX_STRENGTH = 1.0         # Ceiling for pathway strength
PATHWAY_RECENCY_BONUS = 0.05       # Bonus for recent co-activation
PATHWAY_RECENCY_WINDOW_S = 3600    # 1 hour window for recency bonus
RECONSOLIDATION_FLOOR = 0.1


# ── Data Model ─────────────────────────────────────────────

@dataclass
class Memory:
    """A single stored memory with cognitive metadata (17 fields)."""
    id: str
    content: str
    type: str = "episodic"
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    created_at: float = 0.0
    updated_at: float = 0.0
    last_accessed: float = 0.0
    access_count: int = 0
    source: str = "session"
    linked_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: bytes | None = None
    emotion: str = "neutral"
    confidence: float = 1.0
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("embedding", None)
        return d

    @property
    def is_flashbulb(self) -> bool:
        return self.metadata.get("flashbulb", False)

    @property
    def is_identity(self) -> bool:
        return "identity" in self.tags


# ── Helpers ────────────────────────────────────────────────

_FTS5_SPECIAL = re.compile(r'["*()+-:^]')


def _sanitize_fts_query(raw: str) -> str:
    """Escape special FTS5 characters so user input is treated as literals."""
    cleaned = _FTS5_SPECIAL.sub(" ", raw).strip()
    if not cleaned:
        return '""'
    tokens = cleaned.split()
    return " ".join(f'"{t}"' for t in tokens if t)


def _load_json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    try:
        result = json.loads(raw) if isinstance(raw, str) else []
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _load_json_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        result = json.loads(raw) if isinstance(raw, str) else {}
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _safe_row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Safely get a value from a Row, returning default if column missing."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


# ── Engine ─────────────────────────────────────────────────

class Cortex:
    """SQLite-backed cognitive memory engine with biological dynamics.

    Args:
        db_path: Path to SQLite database file. Created if absent.
        max_memory_count: Upper bound on stored memories.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS memories (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            type          TEXT NOT NULL DEFAULT 'episodic',
            tags          TEXT NOT NULL DEFAULT '[]',
            importance    REAL NOT NULL DEFAULT 0.5,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            last_accessed REAL NOT NULL DEFAULT 0.0,
            access_count  INTEGER NOT NULL DEFAULT 0,
            source        TEXT NOT NULL DEFAULT 'session',
            linked_ids    TEXT NOT NULL DEFAULT '[]',
            metadata      TEXT NOT NULL DEFAULT '{}',
            embedding     BLOB,
            emotion       TEXT NOT NULL DEFAULT 'neutral',
            confidence    REAL NOT NULL DEFAULT 1.0,
            context       TEXT NOT NULL DEFAULT ''
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
            USING fts5(content, tags, tokenize='porter');

        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, new.tags);
        END;

        CREATE TABLE IF NOT EXISTS synaptic_pathways (
            source_id      TEXT NOT NULL,
            target_id      TEXT NOT NULL,
            strength       REAL NOT NULL DEFAULT 0.1,
            co_recall_count INTEGER NOT NULL DEFAULT 1,
            last_activated REAL NOT NULL,
            PRIMARY KEY (source_id, target_id),
            FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
        );
    """

    # Columns added in the cognitive upgrade (for schema migration)
    _NEW_COLUMNS = [
        ("type", "TEXT NOT NULL DEFAULT 'episodic'"),
        ("last_accessed", "REAL NOT NULL DEFAULT 0.0"),
        ("access_count", "INTEGER NOT NULL DEFAULT 0"),
        ("source", "TEXT NOT NULL DEFAULT 'session'"),
        ("linked_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ("embedding", "BLOB"),
        ("emotion", "TEXT NOT NULL DEFAULT 'neutral'"),
        ("confidence", "REAL NOT NULL DEFAULT 1.0"),
        ("context", "TEXT NOT NULL DEFAULT ''"),
    ]

    def __init__(self, db_path: str, max_memory_count: int = 10_000) -> None:
        self._db_path = db_path
        self._max = max_memory_count
        self._lock = threading.Lock()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads during writes — critical for
        # multi-component access (MCP server, IonicHalo, Reaper, lessons).
        # busy_timeout prevents 'database is locked' under write contention.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    # ── Schema Management ────────────────────────────────

    def _init_db(self) -> None:
        existing = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()

        if existing:
            self._migrate_schema()
        else:
            self._conn.executescript(self._SCHEMA)
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """Add new columns to pre-upgrade databases."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(memories)").fetchall()
        }

        for col_name, col_def in self._NEW_COLUMNS:
            if col_name not in cols:
                try:
                    self._conn.execute(
                        f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}"
                    )
                except sqlite3.OperationalError:
                    pass

        # Rebuild FTS if it doesn't have the tags column
        try:
            self._conn.execute("SELECT tags FROM memories_fts LIMIT 0")
        except sqlite3.OperationalError:
            self._rebuild_fts()

        # Drop legacy UPDATE/DELETE triggers that cause SQL logic errors
        for trigger in ("memories_ad", "memories_au"):
            self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        # Ensure synaptic_pathways table exists (migration for existing DBs)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS synaptic_pathways (
                source_id      TEXT NOT NULL,
                target_id      TEXT NOT NULL,
                strength       REAL NOT NULL DEFAULT 0.1,
                co_recall_count INTEGER NOT NULL DEFAULT 1,
                last_activated REAL NOT NULL,
                PRIMARY KEY (source_id, target_id),
                FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
            )
        """)

        self._conn.commit()

    def _rebuild_fts(self) -> None:
        """Drop and recreate FTS table + insert trigger.

        UPDATE/DELETE triggers are intentionally omitted — TEXT PRIMARY KEY
        causes rowid mismatch with FTS5's delete command. Content and tags
        are immutable after INSERT, so the update trigger is unnecessary.
        Deletes are synced manually via _delete_from_fts().
        """
        self._conn.execute("DROP TABLE IF EXISTS memories_fts")
        for trigger in ("memories_ai", "memories_ad", "memories_au"):
            self._conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, tags, tokenize='porter');

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.rowid, new.content, new.tags);
            END;

            INSERT INTO memories_fts(rowid, content, tags)
                SELECT rowid, content, COALESCE(tags, '[]') FROM memories;
        """)

    # ── Write ────────────────────────────────────────────

    def remember(
        self,
        content: str,
        *,
        type: str = "episodic",
        tags: list[str] | None = None,
        importance: float = 0.5,
        emotion: str = "neutral",
        source: str = "session",
        confidence: float = 1.0,
        context: str = "",
        linked_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        """Store a new memory with cognitive metadata.

        Automatically applies:
        - Source monitoring confidence penalty
        - Flashbulb detection for high-emotion events
        - Emotional encoding boost to importance
        """
        if not content or not content.strip():
            raise ValueError("Memory content must not be empty")

        if type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {type}. Use: {MEMORY_TYPES}")

        emotion = emotion if emotion in EMOTIONS else "neutral"
        source = source if source in SOURCE_TYPES else "session"
        importance = max(0.0, min(1.0, importance))
        confidence = max(0.0, min(1.0, confidence))

        # Source monitoring: penalize non-experienced sources
        confidence *= SOURCE_PENALTIES.get(source, 1.0)

        now = time.time()
        tags = tags or []
        meta = metadata or {}

        # Flashbulb detection (Brown & Kulik, 1977)
        if emotion in FLASHBULB_EMOTIONS and importance >= FLASHBULB_IMPORTANCE_THRESHOLD:
            meta["flashbulb"] = True

        # Emotional encoding boost (moderate, not full retrieval multiplier)
        emotion_boost = EMOTION_BOOSTS.get(emotion, 1.0)
        importance = min(1.0, importance * (1.0 + (emotion_boost - 1.0) * 0.1))

        mem = Memory(
            id=uuid.uuid4().hex,
            content=content.strip(),
            type=type,
            tags=tags,
            importance=importance,
            created_at=now,
            updated_at=now,
            last_accessed=now,
            access_count=0,
            source=source,
            linked_ids=linked_ids or [],
            metadata=meta,
            embedding=None,
            emotion=emotion,
            confidence=confidence,
            context=context,
        )

        with self._lock:
            self._conn.execute(
                "INSERT INTO memories "
                "(id, content, type, tags, importance, created_at, updated_at, "
                "last_accessed, access_count, source, linked_ids, metadata, "
                "embedding, emotion, confidence, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mem.id, mem.content, mem.type, json.dumps(mem.tags),
                    mem.importance, mem.created_at, mem.updated_at,
                    mem.last_accessed, mem.access_count, mem.source,
                    json.dumps(mem.linked_ids), json.dumps(mem.metadata),
                    mem.embedding, mem.emotion, mem.confidence, mem.context,
                ),
            )
            self._conn.commit()
            self._evict_if_needed()

        return mem

    # ── Read ─────────────────────────────────────────────

    def recall(
        self,
        query: str,
        limit: int = 20,
        min_importance: float = 0.0,
    ) -> list[Memory]:
        """FTS5 search ranked by relevance × importance. Updates access metadata."""
        safe_q = _sanitize_fts_query(query)
        if safe_q == '""':
            return []

        limit = max(1, min(limit, 200))

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, bm25(memories_fts) AS rank
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                  AND m.importance >= ?
                ORDER BY (rank * (1.0 + m.importance)) ASC
                LIMIT ?
                """,
                (safe_q, min_importance, limit),
            ).fetchall()

            now = time.time()
            for row in rows:
                self._conn.execute(
                    "UPDATE memories SET last_accessed = ?, "
                    "access_count = access_count + 1 WHERE id = ?",
                    (now, row["id"]),
                )
            if rows:
                self._conn.commit()

        memories = [self._row_to_memory(r) for r in rows]

        # Hebbian pathway strengthening: co-recalled memories form pathways
        if len(memories) >= 2:
            self._strengthen_pathways([m.id for m in memories])

        return memories

    def emotional_recall(
        self,
        query: str,
        emotion: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """Retrieve memories with emotional valence boosting.

        Fear memories surface first. If emotion specified, that emotion
        is specifically boosted.
        """
        safe_q = _sanitize_fts_query(query)
        if safe_q == '""':
            return []

        limit = max(1, min(limit, 200))

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, bm25(memories_fts) AS rank
                FROM memories_fts f
                JOIN memories m ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                LIMIT ?
                """,
                (safe_q, limit * 3),
            ).fetchall()

            now = time.time()
            for row in rows:
                self._conn.execute(
                    "UPDATE memories SET last_accessed = ?, "
                    "access_count = access_count + 1 WHERE id = ?",
                    (now, row["id"]),
                )
            if rows:
                self._conn.commit()

        memories = [self._row_to_memory(r) for r in rows]

        # Strengthen pathways for emotionally-recalled memories
        if len(memories) >= 2:
            self._strengthen_pathways([m.id for m in memories[:limit]])

        # Re-rank by emotional boost (optionally filtered)
        target_emotion = emotion if emotion and emotion in EMOTIONS else None
        memories.sort(
            key=lambda m: m.importance * (
                EMOTION_BOOSTS.get(m.emotion, 1.0)
                * (2.0 if target_emotion and m.emotion == target_emotion else 1.0)
            ),
            reverse=True,
        )
        return memories[:limit]

    def get(self, memory_id: str) -> Memory | None:
        """Retrieve a single memory by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_memory(row) if row else None

    def list_all(self, limit: int = 50, offset: int = 0) -> list[Memory]:
        """Paginated listing, newest first."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

        return [self._row_to_memory(r) for r in rows]

    def count(self) -> int:
        """Total number of stored memories."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0] if row else 0

    # ── Synaptic Pathways ─────────────────────────────────

    def _strengthen_pathways(self, memory_ids: list[str]) -> None:
        """Hebbian learning: strengthen pathways between co-recalled memories.

        For every pair in the recall batch, increment co_recall_count
        and increase strength. Recent co-activations get a bonus.
        """
        if len(memory_ids) < 2:
            return

        now = time.time()
        with self._lock:
            for i, source_id in enumerate(memory_ids):
                for target_id in memory_ids[i + 1:]:
                    # Ensure consistent ordering (source < target)
                    a, b = (source_id, target_id) if source_id < target_id else (target_id, source_id)

                    existing = self._conn.execute(
                        "SELECT strength, co_recall_count, last_activated "
                        "FROM synaptic_pathways WHERE source_id = ? AND target_id = ?",
                        (a, b),
                    ).fetchone()

                    if existing:
                        new_count = existing["co_recall_count"] + 1
                        recency = 0.0
                        if (now - existing["last_activated"]) < PATHWAY_RECENCY_WINDOW_S:
                            recency = PATHWAY_RECENCY_BONUS
                        new_strength = min(
                            PATHWAY_MAX_STRENGTH,
                            existing["strength"] + PATHWAY_STRENGTHEN_STEP + recency,
                        )
                        self._conn.execute(
                            "UPDATE synaptic_pathways SET strength = ?, "
                            "co_recall_count = ?, last_activated = ? "
                            "WHERE source_id = ? AND target_id = ?",
                            (new_strength, new_count, now, a, b),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO synaptic_pathways "
                            "(source_id, target_id, strength, co_recall_count, last_activated) "
                            "VALUES (?, ?, ?, 1, ?)",
                            (a, b, PATHWAY_STRENGTHEN_STEP, now),
                        )

            self._conn.commit()

    def recall_associative(
        self,
        memory_id: str,
        limit: int = 10,
        min_strength: float = 0.05,
    ) -> list[tuple[Memory, float]]:
        """Follow strongest synaptic pathways from a memory.

        Returns list of (Memory, pathway_strength) tuples sorted
        by strength descending. Only returns memories that still exist.
        """
        limit = max(1, min(limit, 50))

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    CASE WHEN source_id = ? THEN target_id ELSE source_id END AS linked_id,
                    strength
                FROM synaptic_pathways
                WHERE (source_id = ? OR target_id = ?)
                  AND strength >= ?
                ORDER BY strength DESC
                LIMIT ?
                """,
                (memory_id, memory_id, memory_id, min_strength, limit),
            ).fetchall()

        results: list[tuple[Memory, float]] = []
        for row in rows:
            mem = self.get(row["linked_id"])
            if mem:
                results.append((mem, row["strength"]))

        return results

    def get_pathway_strength(self, id_a: str, id_b: str) -> float:
        """Get the synaptic pathway strength between two memories."""
        a, b = (id_a, id_b) if id_a < id_b else (id_b, id_a)
        with self._lock:
            row = self._conn.execute(
                "SELECT strength FROM synaptic_pathways "
                "WHERE source_id = ? AND target_id = ?",
                (a, b),
            ).fetchone()
        return row["strength"] if row else 0.0

    # ── Memory Dynamics ──────────────────────────────────

    def decay(self) -> int:
        """Run Ebbinghaus forgetting curves on all non-protected memories.

        R(t) = e^{-t/S}  where  S = S_base * (1+n)^1.5 * (1+I*2.0)

        Returns count of memories pruned.
        """
        now = time.time()
        pruned = 0

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, importance, last_accessed, created_at, "
                "access_count, metadata, tags FROM memories"
            ).fetchall()

            for row in rows:
                tags = _load_json_list(row["tags"])
                meta = _load_json_dict(row["metadata"])

                if "identity" in tags or meta.get("flashbulb"):
                    continue

                last = row["last_accessed"] or row["created_at"] or now
                t = now - last
                if t <= 0:
                    continue

                base_imp = row["importance"]
                n = row["access_count"] or 0

                stability = (
                    DECAY_BASE_STABILITY_S
                    * ((1 + n) ** 1.5)
                    * (1 + base_imp * 2.0)
                )
                retention = math.exp(-t / stability)
                effective = base_imp * retention

                if effective < DECAY_FLOOR:
                    self._delete_memory_row(row["id"])
                    pruned += 1
                else:
                    self._conn.execute(
                        "UPDATE memories SET importance = ?, updated_at = ? "
                        "WHERE id = ?",
                        (effective, now, row["id"]),
                    )

            self._conn.commit()

        return pruned

    def consolidate(self) -> int:
        """Compress old episodic memories into semantic knowledge.

        Episodic memories older than 72h with importance < 0.7 are
        converted to semantic type with truncated content.

        Returns count of consolidated memories.
        """
        now = time.time()
        threshold = now - (CONSOLIDATION_AGE_HOURS * 3600)
        consolidated = 0

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, content FROM memories "
                "WHERE type = 'episodic' AND created_at < ? AND importance < ?",
                (threshold, CONSOLIDATION_IMPORTANCE_CEILING),
            ).fetchall()

            for row in rows:
                content = row["content"]
                compressed = content[:200] + ("..." if len(content) > 200 else "")

                # Update FTS if content changed
                if compressed != content:
                    fts_row = self._conn.execute(
                        "SELECT rowid FROM memories WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    if fts_row:
                        self._conn.execute(
                            "DELETE FROM memories_fts WHERE rowid = ?",
                            (fts_row["rowid"],),
                        )

                self._conn.execute(
                    "UPDATE memories SET type = 'semantic', content = ?, "
                    "updated_at = ? WHERE id = ?",
                    (compressed, now, row["id"]),
                )

                # Re-index if content changed
                if compressed != content:
                    fts_row = self._conn.execute(
                        "SELECT rowid, tags FROM memories WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    if fts_row:
                        self._conn.execute(
                            "INSERT INTO memories_fts(rowid, content, tags) "
                            "VALUES (?, ?, ?)",
                            (fts_row["rowid"], compressed, fts_row["tags"]),
                        )

                consolidated += 1

            self._conn.commit()

        return consolidated

    def reconsolidate(self, memory_id: str) -> Memory | None:
        """Degrade confidence of a recalled memory (Nader 2003).

        Each reconsolidation reduces confidence by 5%.
        After 20 reconsolidations, confidence ≈ 36% of original.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT confidence FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if not row:
                return None

            new_conf = max(RECONSOLIDATION_FLOOR, row["confidence"] * RECONSOLIDATION_FACTOR)
            now = time.time()

            self._conn.execute(
                "UPDATE memories SET confidence = ?, last_accessed = ?, "
                "access_count = access_count + 1, updated_at = ? WHERE id = ?",
                (new_conf, now, now, memory_id),
            )
            self._conn.commit()

            updated = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()

        return self._row_to_memory(updated) if updated else None

    # ── Delete ───────────────────────────────────────────

    def forget(self, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if it existed."""
        with self._lock:
            deleted = self._delete_memory_row(memory_id)
            self._conn.commit()
        return deleted

    # ── Stats ────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Extended statistics: type/emotion distribution, protected counts."""
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
            types = dict(self._conn.execute(
                "SELECT type, COUNT(*) FROM memories GROUP BY type"
            ).fetchall())
            emotions = dict(self._conn.execute(
                "SELECT emotion, COUNT(*) FROM memories GROUP BY emotion"
            ).fetchall())
            flashbulb = self._conn.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE metadata LIKE '%\"flashbulb\": true%'"
            ).fetchone()[0]
            identity = self._conn.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE tags LIKE '%\"identity\"%'"
            ).fetchone()[0]

        db_path = Path(self._db_path)
        return {
            "count": count,
            "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "by_type": types,
            "by_emotion": emotions,
            "flashbulb_count": flashbulb,
            "identity_count": identity,
        }

    # ── Lifecycle ────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    # ── Internal ─────────────────────────────────────────

    def _evict_if_needed(self) -> None:
        """Remove lowest-importance, oldest non-protected memories."""
        count = self._conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0]
        overflow = count - self._max
        if overflow <= 0:
            return

        rows = self._conn.execute(
            """SELECT id FROM memories
            WHERE tags NOT LIKE '%"identity"%'
              AND metadata NOT LIKE '%"flashbulb": true%'
            ORDER BY importance ASC, created_at ASC
            LIMIT ?""",
            (overflow,),
        ).fetchall()
        for row in rows:
            self._delete_memory_row(row["id"])
        self._conn.commit()

    def _delete_memory_row(self, memory_id: str) -> bool:
        """Delete a memory and its FTS index entry. Must hold self._lock."""
        row = self._conn.execute(
            "SELECT rowid, content, tags FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if not row:
            return False
        # Remove FTS entry first (must match rowid + content exactly)
        self._conn.execute(
            "DELETE FROM memories_fts WHERE rowid = ?", (row["rowid"],)
        )
        self._conn.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        return True

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> Memory:
        keys = row.keys()
        return Memory(
            id=row["id"],
            content=row["content"],
            type=row["type"] if "type" in keys else "episodic",
            tags=_load_json_list(row["tags"]),
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed=row["last_accessed"] if "last_accessed" in keys else 0.0,
            access_count=row["access_count"] if "access_count" in keys else 0,
            source=row["source"] if "source" in keys else "session",
            linked_ids=_load_json_list(
                row["linked_ids"] if "linked_ids" in keys else "[]"
            ),
            metadata=_load_json_dict(
                row["metadata"] if "metadata" in keys else "{}"
            ),
            embedding=row["embedding"] if "embedding" in keys else None,
            emotion=row["emotion"] if "emotion" in keys else "neutral",
            confidence=row["confidence"] if "confidence" in keys else 1.0,
            context=row["context"] if "context" in keys else "",
        )
