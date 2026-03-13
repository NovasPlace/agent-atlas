"""SovereignStorage — Local File Storage Engine.

UUID-keyed file store with size limits, metadata sidecar files,
and path-traversal protection.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class FileMeta:
    """Metadata for a stored file."""
    file_id: str
    original_name: str
    size_bytes: int
    content_type: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SovereignStorage:
    """Local file storage with UUID keys and size enforcement.

    Args:
        root: Directory where files and metadata are stored.
        max_object_size: Maximum upload size in bytes.
    """

    _FILES_DIR = "objects"
    _META_DIR = "meta"

    def __init__(self, root: str, max_object_size: int = 500 * 1024 * 1024) -> None:
        self._root = Path(root)
        self._max_size = max_object_size

        self._objects = self._root / self._FILES_DIR
        self._meta = self._root / self._META_DIR
        self._objects.mkdir(parents=True, exist_ok=True)
        self._meta.mkdir(parents=True, exist_ok=True)

    # ── Write ────────────────────────────────────────────

    def upload(
        self,
        filename: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> FileMeta:
        """Store a file. Returns metadata on success."""
        if not data:
            raise ValueError("Upload data must not be empty")
        if len(data) > self._max_size:
            raise ValueError(
                f"File exceeds maximum size of {self._max_size} bytes "
                f"(got {len(data)} bytes)"
            )

        file_id = uuid.uuid4().hex
        safe_name = self._safe_filename(filename)

        # Write file
        file_path = self._objects / file_id
        file_path.write_bytes(data)

        # Write metadata sidecar
        meta = FileMeta(
            file_id=file_id,
            original_name=safe_name,
            size_bytes=len(data),
            content_type=content_type,
            created_at=time.time(),
        )
        meta_path = self._meta / f"{file_id}.json"
        meta_path.write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        return meta

    # ── Read ─────────────────────────────────────────────

    def download(self, file_id: str) -> tuple[bytes, FileMeta]:
        """Retrieve file content and metadata. Raises FileNotFoundError."""
        self._validate_id(file_id)
        meta = self.get_metadata(file_id)
        file_path = self._objects / file_id
        if not file_path.exists():
            raise FileNotFoundError(f"File object missing: {file_id}")
        return file_path.read_bytes(), meta

    def get_metadata(self, file_id: str) -> FileMeta:
        """Retrieve metadata without file content."""
        self._validate_id(file_id)
        meta_path = self._meta / f"{file_id}.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No file with ID: {file_id}")
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return FileMeta(**raw)

    def list_files(self, limit: int = 50, offset: int = 0) -> list[FileMeta]:
        """Paginated listing of stored files, newest first."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        meta_files = sorted(
            self._meta.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        results: list[FileMeta] = []
        for mf in meta_files[offset : offset + limit]:
            try:
                raw = json.loads(mf.read_text(encoding="utf-8"))
                results.append(FileMeta(**raw))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        return results

    def file_count(self) -> int:
        """Total number of stored files."""
        return len(list(self._meta.glob("*.json")))

    # ── Delete ───────────────────────────────────────────

    def delete(self, file_id: str) -> bool:
        """Delete a file and its metadata. Returns True if it existed."""
        self._validate_id(file_id)
        file_path = self._objects / file_id
        meta_path = self._meta / f"{file_id}.json"

        existed = meta_path.exists()
        if file_path.exists():
            file_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        return existed

    # ── Internal ─────────────────────────────────────────

    @staticmethod
    def _safe_filename(raw: str) -> str:
        """Strip path components to prevent traversal attacks."""
        name = Path(raw).name
        if not name or name in (".", ".."):
            return "unnamed"
        return name

    @staticmethod
    def _validate_id(file_id: str) -> None:
        """Reject IDs containing path separators or special characters."""
        if not file_id or "/" in file_id or "\\" in file_id or ".." in file_id:
            raise ValueError(f"Invalid file ID: {file_id}")
