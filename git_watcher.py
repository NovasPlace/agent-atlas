"""GitWatcherDaemon — Watches git repos for new commits → CortexDB episodic memories.

Polls all registered repos every POLL_INTERVAL_S seconds. On each new
commit, stores an episodic memory in CortexDB so agents can recall what
changed across all active projects without running git log manually.

Memory format:
    type="episodic"
    tags=["git", <repo_name>, "commit"]
    content="[<repo>] <hash>: <subject> (author, files changed)"

Also serves a socket API for dynamic watch/unwatch and status queries.

Socket: /tmp/agent-git-watcher.sock
State:  ~/.gemini/memory/git_watcher_state.db  (last-seen SHAs per repo)

Protocol (newline-delimited JSON):
  PING                                     → health check
  STATUS                                   → watched repos + last commit
  WATCH   {path}                           → add a repo to watch list
  UNWATCH {path}                           → remove from watch list
  COMMITS {path, limit?}                   → recent stored commits for repo
  SCAN                                     → trigger immediate scan (no wait)

Usage (daemon integration):
    from git_watcher import run_git_watcher
    await run_git_watcher(shutdown_event)

Usage (self-test):
    python3 git_watcher.py --test-mode

Usage (standalone):
    python3 git_watcher.py                 # runs forever
    python3 git_watcher.py --status        # show watched repos
    python3 git_watcher.py --watch /path   # add a repo (persistent)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("git-watcher")

SOCKET_PATH     = "/tmp/agent-git-watcher.sock"
STATE_DB        = os.path.expanduser("~/.gemini/memory/git_watcher_state.db")
CORTEX_DB       = os.path.expanduser("~/.cortexdb/agent_system.db")
HOT_MD          = os.path.expanduser("~/.gemini/memory/hot.md")
POLL_INTERVAL_S = 60     # How often to scan all repos
MAX_MSG_BYTES   = 32_768


# ── State DB ──────────────────────────────────────────────────

def _open_state(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_repos (
            path       TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            last_sha   TEXT DEFAULT '',
            added_at   INTEGER NOT NULL,
            last_scan  INTEGER DEFAULT 0,
            commit_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            sha       TEXT NOT NULL,
            subject   TEXT NOT NULL,
            author    TEXT NOT NULL,
            ts        INTEGER NOT NULL,
            files_changed INTEGER DEFAULT 0,
            UNIQUE (repo_path, sha)
        )
    """)
    conn.commit()
    return conn


def _load_watched(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT path, name, last_sha, last_scan, commit_count FROM watched_repos").fetchall()
    return {
        r[0]: {"name": r[1], "last_sha": r[2], "last_scan": r[3], "commit_count": r[4]}
        for r in rows
    }


def _upsert_repo(conn: sqlite3.Connection, path: str, name: str) -> None:
    conn.execute(
        "INSERT INTO watched_repos (path, name, added_at) VALUES (?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET name=excluded.name",
        (path, name, int(time.time())),
    )
    conn.commit()


def _remove_repo(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM watched_repos WHERE path=?", (path,))
    conn.commit()


def _update_last_sha(conn: sqlite3.Connection, path: str, sha: str) -> None:
    conn.execute(
        "UPDATE watched_repos SET last_sha=?, last_scan=?, commit_count=commit_count+1 WHERE path=?",
        (sha, int(time.time()), path),
    )
    conn.commit()


def _log_commit(conn: sqlite3.Connection, repo_path: str, repo_name: str,
                sha: str, subject: str, author: str, ts: int, files: int) -> bool:
    """Insert commit. Returns False if already logged (idempotent)."""
    try:
        conn.execute(
            "INSERT INTO commit_log (repo_path, repo_name, sha, subject, author, ts, files_changed) "
            "VALUES (?,?,?,?,?,?,?)",
            (repo_path, repo_name, sha, subject[:500], author[:100], ts, files),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # Already stored


# ── Git Helpers ───────────────────────────────────────────────

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

def _sanitize_watch_path(raw: str) -> str:
    """Normalize and validate a path supplied via the socket API.

    - Strips null bytes and control characters
    - Expands ~ and environment variables
    - Resolves to an absolute path (no symlink traversal tricks)
    - Rejects paths longer than 512 chars
    Returns empty string if the path is unacceptable.
    """
    cleaned = _CTRL_RE.sub("", str(raw)).strip()
    if not cleaned or len(cleaned) > 512:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(cleaned))
    try:
        resolved = str(Path(expanded).resolve())
    except Exception:
        return ""
    return resolved


def _repo_name(path: str) -> str:
    """Derive a clean repo name from path."""
    return Path(path).name or path


def _is_git_repo(path: str) -> bool:
    return (Path(path) / ".git").exists()


def _get_new_commits(repo_path: str, since_sha: str) -> list[dict]:
    """Return commits newer than since_sha. Empty list on any error."""
    try:
        # Format: SHA|subject|author|unix_ts|files_changed_count
        fmt = "%H|%s|%an|%at"
        if since_sha:
            cmd = ["git", "log", f"{since_sha}..HEAD", f"--format={fmt}", "--no-merges"]
        else:
            # First scan — only grab the last 5 to avoid flooding CortexDB
            cmd = ["git", "log", "-5", f"--format={fmt}", "--no-merges"]

        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []

        commits = []
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            sha, subject, author, ts_str = parts
            # Count changed files for this commit
            try:
                stat_result = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", sha],
                    cwd=repo_path, capture_output=True, text=True, timeout=5
                )
                files_changed = len(stat_result.stdout.strip().splitlines())
            except Exception:
                files_changed = 0
            commits.append({
                "sha": sha[:12],
                "full_sha": sha,
                "subject": subject.strip(),
                "author": author.strip(),
                "ts": int(ts_str.strip()),
                "files_changed": files_changed,
            })
        return commits
    except subprocess.TimeoutExpired:
        logger.warning("git log timed out for %s", repo_path)
        return []
    except Exception as e:
        logger.debug("git error on %s: %s", repo_path, e)
        return []


def _get_head_sha(repo_path: str) -> str:
    """Get current HEAD SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()[:40] if result.returncode == 0 else ""
    except Exception:
        return ""


# ── CortexDB Writer ───────────────────────────────────────────

def _load_cortex():
    """Lazy-import Cortex — same path pattern as other daemons."""
    _CORTEX_ROOT = os.path.expanduser("~/Desktop/Agent_System/DB-Memory/CortexDB")
    if _CORTEX_ROOT not in sys.path:
        sys.path.insert(0, _CORTEX_ROOT)
    from cortex.engine import Cortex  # type: ignore
    return Cortex(CORTEX_DB)


_cortex_instance = None

def _get_cortex():
    global _cortex_instance
    if _cortex_instance is None:
        try:
            _cortex_instance = _load_cortex()
        except Exception as e:
            logger.warning("CortexDB unavailable: %s", e)
    return _cortex_instance


def _store_commit_memory(repo_name: str, commit: dict) -> bool:
    """Write an episodic CortexDB memory for a commit. Best-effort."""
    cortex = _get_cortex()
    if not cortex:
        return False
    try:
        content = (
            f"[{repo_name}] {commit['sha']}: {commit['subject']} "
            f"(by {commit['author']}, {commit['files_changed']} file(s) changed)"
        )
        cortex.remember(
            content,
            type="episodic",
            tags=["git", repo_name, "commit"],
            importance=0.6,
            emotion="neutral",
            source="experienced",
            confidence=1.0,
            context=f"Git commit in {repo_name}",
        )
        return True
    except Exception as e:
        logger.warning("CortexDB write failed for commit %s: %s", commit["sha"], e)
        return False


# ── Hot.md Parser for Auto-Discovery ─────────────────────────

def _discover_repos_from_hot() -> list[tuple[str, str]]:
    """Parse hot.md ACTIVE PROJECTS table for repo paths.

    Returns list of (path, name) tuples for paths that exist and are git repos.
    """
    repos = []
    try:
        content = Path(HOT_MD).read_text(encoding="utf-8")
        # Match table rows: | Name | `path` | ...
        pattern = re.compile(r"\|\s*([^|]+?)\s*\|\s*`([^`]+)`\s*\|")
        for m in pattern.finditer(content):
            name = m.group(1).strip()
            raw_path = m.group(2).strip()
            # Expand ~ and env vars
            expanded = os.path.expanduser(raw_path.rstrip("/"))
            if os.path.isdir(expanded) and _is_git_repo(expanded):
                repos.append((expanded, name))
    except Exception as e:
        logger.debug("hot.md parse error: %s", e)
    return repos


# ── Scanner Loop ──────────────────────────────────────────────

async def _scan_all(conn: sqlite3.Connection, watched: dict) -> int:
    """Scan all watched repos for new commits. Returns total new commits found."""
    total_new = 0
    loop = asyncio.get_event_loop()

    for repo_path, meta in list(watched.items()):
        if not os.path.isdir(repo_path):
            continue

        repo_name = meta["name"]
        last_sha  = meta["last_sha"]

        # Run blocking git calls in thread pool
        commits = await loop.run_in_executor(
            None, _get_new_commits, repo_path, last_sha
        )

        if not commits:
            # Update scan timestamp even if no new commits
            conn.execute(
                "UPDATE watched_repos SET last_scan=? WHERE path=?",
                (int(time.time()), repo_path),
            )
            conn.commit()
            continue

        new_head = commits[0]["full_sha"]

        for commit in reversed(commits):  # Oldest first
            is_new = _log_commit(
                conn, repo_path, repo_name,
                commit["sha"], commit["subject"],
                commit["author"], commit["ts"], commit["files_changed"],
            )
            if is_new:
                _store_commit_memory(repo_name, commit)
                total_new += 1
                logger.info(
                    "[%s] new commit: %s: %s",
                    repo_name, commit["sha"], commit["subject"][:60]
                )

        # Update last SHA and scan time
        _update_last_sha(conn, repo_path, new_head)
        watched[repo_path]["last_sha"] = new_head

    return total_new


async def _scanner_loop(
    conn: sqlite3.Connection,
    watched: dict,
    shutdown: asyncio.Event,
) -> None:
    """Periodic scan loop — runs every POLL_INTERVAL_S seconds."""
    while not shutdown.is_set():
        try:
            new = await _scan_all(conn, watched)
            if new:
                logger.info("Scan complete — %d new commit(s) stored", new)
        except Exception as e:
            logger.error("Scanner error: %s", e)

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=POLL_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            pass


# ── Command Handlers ──────────────────────────────────────────

async def _handle_command(
    cmd_obj: dict,
    conn: sqlite3.Connection,
    watched: dict,
) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True, "watching": len(watched)}

    if cmd == "STATUS":
        repos = conn.execute(
            "SELECT path, name, last_sha, last_scan, commit_count FROM watched_repos"
        ).fetchall()
        return {
            "ok": True,
            "repos": [
                {
                    "path": r[0], "name": r[1],
                    "last_sha": r[2][:12] if r[2] else "",
                    "last_scan": r[3], "commit_count": r[4],
                }
                for r in repos
            ],
        }

    if cmd == "WATCH":
        path = _sanitize_watch_path(cmd_obj.get("path", ""))
        if not path:
            return {"ok": False, "error": "invalid or empty path"}
        if not os.path.isdir(path):
            return {"ok": False, "error": f"Directory not found: {path}"}
        if not _is_git_repo(path):
            return {"ok": False, "error": f"Not a git repo: {path}"}
        name = _CTRL_RE.sub("", str(cmd_obj.get("name", "") or _repo_name(path)))[:60]
        _upsert_repo(conn, path, name)
        watched[path] = {"name": name, "last_sha": "", "last_scan": 0, "commit_count": 0}
        logger.info("Now watching: %s (%s)", name, path)
        return {"ok": True, "name": name}

    if cmd == "UNWATCH":
        path = _sanitize_watch_path(cmd_obj.get("path", ""))
        if not path:
            return {"ok": False, "error": "invalid or empty path"}
        _remove_repo(conn, path)
        watched.pop(path, None)
        return {"ok": True}

    if cmd == "COMMITS":
        path  = _sanitize_watch_path(cmd_obj.get("path", ""))
        if not path:
            return {"ok": False, "error": "invalid or empty path"}
        limit = min(int(cmd_obj.get("limit", 20)), 100)
        rows  = conn.execute(
            "SELECT sha, subject, author, ts, files_changed FROM commit_log "
            "WHERE repo_path=? ORDER BY ts DESC LIMIT ?",
            (path, limit),
        ).fetchall()
        return {
            "ok": True,
            "commits": [
                {"sha": r[0], "subject": r[1], "author": r[2],
                 "ts": r[3], "files_changed": r[4]}
                for r in rows
            ],
        }

    if cmd == "SCAN":
        new = await _scan_all(conn, watched)
        return {"ok": True, "new_commits": new}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection Handler ────────────────────────────────────────

async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    conn: sqlite3.Connection,
    watched: dict,
) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(MAX_MSG_BYTES), timeout=5.0)
        if not raw:
            return
        try:
            cmd_obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"JSON parse error: {e}"}
        else:
            response = await _handle_command(cmd_obj, conn, watched)

        writer.write(json.dumps(response).encode("utf-8") + b"\n")
        await writer.drain()
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        logger.error("Connection error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ── Daemon Entry ──────────────────────────────────────────────

async def run_git_watcher(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
    state_db: str = STATE_DB,
    auto_discover: bool = True,
) -> None:
    conn    = _open_state(state_db)
    watched = _load_watched(conn)
    _shutdown = shutdown_event or asyncio.Event()

    # Auto-discover repos from hot.md on startup
    if auto_discover:
        for path, name in _discover_repos_from_hot():
            if path not in watched:
                _upsert_repo(conn, path, name)
                watched[path] = {"name": name, "last_sha": "", "last_scan": 0, "commit_count": 0}
                logger.info("Auto-discovered repo: %s (%s)", name, path)

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, conn, watched))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)  # Owner-only
    logger.info("GitWatcherDaemon listening on %s, watching %d repo(s)", socket_path, len(watched))

    scanner = asyncio.create_task(_scanner_loop(conn, watched, _shutdown))

    await _shutdown.wait()

    scanner.cancel()
    server.close()
    await server.wait_closed()
    conn.close()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("GitWatcherDaemon stopped.")


# ── Self-Test ─────────────────────────────────────────────────

async def _self_test() -> bool:
    import tempfile
    logger.info("Running GitWatcherDaemon self-test...")

    # Use a temp dir with a real git repo
    with tempfile.TemporaryDirectory() as tmpdir:
        sock = "/tmp/agent-git-watcher-test.sock"
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # Init a real git repo with a commit
        try:
            subprocess.run(["git", "init", tmpdir], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"],
                          cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"],
                          cwd=tmpdir, check=True, capture_output=True)
            (Path(tmpdir) / "readme.txt").write_text("hello")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"],
                          cwd=tmpdir, check=True, capture_output=True)
        except Exception as e:
            logger.error("Could not create test git repo: %s", e)
            return False

        shutdown = asyncio.Event()
        server_task = asyncio.create_task(
            run_git_watcher(shutdown, sock, db_path, auto_discover=False)
        )
        await asyncio.sleep(0.1)

        async def _call(payload: dict) -> dict:
            r, w = await asyncio.open_unix_connection(sock)
            w.write(json.dumps(payload).encode() + b"\n")
            await w.drain()
            raw = await r.read(MAX_MSG_BYTES)
            w.close()
            await w.wait_closed()
            return json.loads(raw.decode())

        try:
            # PING
            resp = await _call({"cmd": "PING"})
            assert resp["ok"] and resp["pong"], f"PING failed: {resp}"

            # WATCH
            resp = await _call({"cmd": "WATCH", "path": tmpdir})
            assert resp["ok"], f"WATCH failed: {resp}"

            # SCAN — should pick up the initial commit
            resp = await _call({"cmd": "SCAN"})
            assert resp["ok"], f"SCAN failed: {resp}"
            assert resp["new_commits"] >= 1, f"No commits found: {resp}"

            # COMMITS
            resp = await _call({"cmd": "COMMITS", "path": tmpdir})
            assert resp["ok"] and len(resp["commits"]) >= 1, f"COMMITS failed: {resp}"
            assert "Initial commit" in resp["commits"][0]["subject"]

            # Add another commit and scan again
            (Path(tmpdir) / "file2.txt").write_text("world")
            subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Second commit"],
                          cwd=tmpdir, capture_output=True)
            resp = await _call({"cmd": "SCAN"})
            assert resp["new_commits"] >= 1, f"Second commit not detected: {resp}"

            # STATUS
            resp = await _call({"cmd": "STATUS"})
            assert resp["ok"] and len(resp["repos"]) == 1

            # UNWATCH
            resp = await _call({"cmd": "UNWATCH", "path": tmpdir})
            assert resp["ok"]

            logger.info("GitWatcherDaemon self-test PASSED")
            return True

        except Exception as e:
            logger.error("GitWatcherDaemon self-test FAILED: %s", e)
            import traceback; traceback.print_exc()
            return False
        finally:
            shutdown.set()
            await server_task
            try:
                os.unlink(db_path)
            except OSError:
                pass


# ── CLI ───────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="GitWatcherDaemon")
    parser.add_argument("--test-mode",  action="store_true")
    parser.add_argument("--status",     action="store_true", help="Show watched repos")
    parser.add_argument("--watch",      metavar="PATH",   help="Add repo to watch list")
    parser.add_argument("--unwatch",    metavar="PATH",   help="Remove repo from watch list")
    parser.add_argument("--commits",    metavar="PATH",   help="Show recent commits for repo")
    parser.add_argument("--scan",       action="store_true", help="Trigger immediate scan")
    parser.add_argument("--socket",     default=SOCKET_PATH)
    parser.add_argument("--db",         default=STATE_DB)
    args = parser.parse_args()

    if args.test_mode:
        success = asyncio.run(_self_test())
        raise SystemExit(0 if success else 1)

    import socket as _socket, json as _json

    def _call_running(payload: dict) -> dict:
        """Call the running daemon's socket synchronously."""
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(args.socket)
            s.sendall(_json.dumps(payload).encode() + b"\n")
            raw = s.recv(MAX_MSG_BYTES)
            s.close()
            return _json.loads(raw.decode())
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if args.status:
        r = _call_running({"cmd": "STATUS"})
        if not r.get("ok"):
            print(f"Error: {r.get('error')}"); raise SystemExit(1)
        repos = r.get("repos", [])
        print(f"Watching {len(repos)} repo(s):")
        for repo in repos:
            print(f"  {repo['name']:30s}  last={repo['last_sha'] or 'none':12s}  "
                  f"commits={repo['commit_count']}")
        raise SystemExit(0)

    if args.watch:
        r = _call_running({"cmd": "WATCH", "path": args.watch})
        print("Watching." if r.get("ok") else f"Error: {r.get('error')}")
        raise SystemExit(0 if r.get("ok") else 1)

    if args.unwatch:
        r = _call_running({"cmd": "UNWATCH", "path": args.unwatch})
        print("Removed." if r.get("ok") else f"Error: {r.get('error')}")
        raise SystemExit(0 if r.get("ok") else 1)

    if args.commits:
        r = _call_running({"cmd": "COMMITS", "path": args.commits})
        if not r.get("ok"):
            print(f"Error: {r.get('error')}"); raise SystemExit(1)
        for c in r.get("commits", []):
            print(f"  {c['sha']:12s}  {c['subject'][:60]:60s}  ({c['author']})")
        raise SystemExit(0)

    if args.scan:
        r = _call_running({"cmd": "SCAN"})
        print(f"Scanned. New commits: {r.get('new_commits', 0)}")
        raise SystemExit(0 if r.get("ok") else 1)

    # Default: run as daemon
    asyncio.run(run_git_watcher(socket_path=args.socket, state_db=args.db))


if __name__ == "__main__":
    main()
