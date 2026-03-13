# Atlas
### Anamnesis for AI Agents

> *The opposite of amnesia. Agents that remember — forever.*

Atlas is a persistent, multi-daemon memory and coordination system for AI agents.
16 background processes carry your agent's full context, history, and directives
across unlimited conversations. Zero cold starts. No repeated mistakes.
No context-length limits on operational memory.

---

## What It Does

Agents normally forget everything between conversations. This kit adds a full memory infrastructure that runs continuously in the background — 16 asyncio daemons that give agents:

- **Persistent memory** across unlimited conversations (CortexDB — episodic, semantic, procedural, working memory)
- **Hot context priming** — relevant memories + directive sections surfaced automatically at session start
- **Cross-session coordination** — multiple agents share state, claim resources, and avoid conflicts
- **Autonomous task queue** — agents defer and schedule work across sessions
- **Real-time event bus** — PostgreSQL LISTEN/NOTIFY for cross-conversation awareness
- **Loop detection** — auto-fires mayday + lesson on repeated identical tool calls
- **Git awareness** — every commit in every watched repo becomes a searchable episodic memory
- **Context pressure monitoring** — signals pre-emptive flush before truncation hits
- **Agent-to-agent messaging** — async inbox/outbox between agent sessions

---

## Requirements

- Linux with systemd (Ubuntu 22.04+, Debian 12+, Arch)
- Python 3.11+
- `git` (for GitWatcherDaemon)
- PostgreSQL (optional — for pg_broadcast real-time events)
- [CortexDB](https://github.com/frost/CortexDB) — the cognitive memory engine

---

## Install

```bash
bash install.sh
```

That's it. The installer:
1. Checks dependencies
2. Installs Python deps (`psycopg2-binary`)
3. Deploys all daemon files to `~/.gemini/memory/`
4. Bootstraps `hot.md`
5. Installs + enables systemd user services
6. Starts all 16 daemons
7. Indexes your agent directive into CortexDB
8. Runs the full 35-check integration test suite

---

## Configuration

All paths are configurable via environment variables. Set these before running `install.sh` or in your shell profile:

```bash
export AGENT_MEMORY_DIR="$HOME/.gemini/memory"     # markdown memory files
export AGENT_CORTEX_DIR="$HOME/.cortexdb"           # CortexDB SQLite store
export AGENT_CORTEX_ROOT="$HOME/path/to/CortexDB"  # CortexDB package location
export AGENT_SOCKET_DIR="/tmp"                       # Unix socket directory
export AGENT_LOOP_THRESHOLD="3"                      # loop detection sensitivity
export AGENT_GIT_POLL_INTERVAL="60"                  # git watcher poll seconds
export AGENT_PRESSURE_LIMIT="150000"                 # context token limit
export AGENT_MSG_TTL="172800"                        # message queue TTL (48h)
```

---

## The 16 Daemons

| # | Daemon | Socket | Purpose |
|---|--------|--------|---------|
| 1 | md_reader | `agent-memory-reader.sock` | Read hot.md, warm files, session state |
| 2 | md_writer | `agent-memory-writer.sock` | Write memory files (serialized, no race conditions) |
| 3 | md_indexer | — | Indexes md writes into CortexDB automatically |
| 4 | context_recall | — | Primes agent brief with top-N relevant memories |
| 5 | subconscious | — | Background file watcher → CortexDB indexing |
| 6 | lesson_engine | — | Captures lessons from session events |
| 7 | session_journal | — | Writes session summaries |
| 8 | memory_sync | — | Cross-process CortexDB sync |
| 9 | session_briefing | — | Generates context briefs |
| 10 | agent_coord | `agent-coord.sock` | Multi-agent presence + advisory file locking |
| 11 | agent_taskqueue | `agent-taskqueue.sock` | Deferred/recurring task scheduling |
| 12 | pg_broadcast | — | PostgreSQL real-time event bus |
| 13 | loop_detector | `agent-loop-detector.sock` | Mayday on 3x repeated tool call |
| 14 | git_watcher | `agent-git-watcher.sock` | Commit → episodic CortexDB memory |
| 15 | context_pressure | `agent-context-pressure.sock` | Token pressure estimation |
| 16 | agent_msgqueue | `agent-msgqueue.sock` | Async agent-to-agent messaging |

---

## Agent API

The `agent_memory_api.py` module is the single entry point for agents:

```python
from agent_memory_api import MemoryAPI
api = MemoryAPI()

# Read current context
ctx = api.get_context()

# Write a lesson that persists forever
api.lesson("Never use subprocess(shell=True) for user input")

# Check for context pressure
r = api.pressure_tick("view_file", output_chars=5000)
if r["action"] == "urgent_flush":
    api.write_session("Working on X — pausing to flush context")

# Detect loops
r = api.record_call("run_command", args_hash="abc123")
if r.get("loop"):
    print(r["mayday"])  # Stop. Change approach.

# Coordinate with other agents
api.coord_presence("agent-a", "building auth system")
api.coord_claim("agent-a", "src/auth.py")

# Queue work for the next session
api.task_push("Review PR #42", priority=2, owner="agent-a")

# Send a message to another agent
api.msg_send("agent-a", "agent-b", "Hey", "Can you review hot.md?")
```

---

## CLI Commands

```bash
# Get current context brief
python3 ~/.gemini/memory/agent_memory_api.py context

# Write a lesson
python3 ~/.gemini/memory/agent_memory_api.py lesson "lesson text"

# Check git watcher status
python3 ~/.gemini/memory/git_watcher.py --status

# Watch a new repo
python3 ~/.gemini/memory/git_watcher.py --watch ~/path/to/repo

# Check daemon health
python3 ~/.gemini/memory/agent_memory_api.py ping

# Run full integration test
python3 ~/.gemini/memory/agent_memory_api.py --test-mode

# Re-index directive after changes
python3 ~/.gemini/memory/directive_indexer.py --reindex

# Check config
python3 ~/.gemini/memory/config.py
```

---

## Directory Structure

```
~/.gemini/memory/                   # All daemon and support files
├── agent_memory_daemon.py          # Orchestrator (starts all 16 daemons)
├── agent_memory_api.py             # Agent-facing API
├── config.py                       # All paths/constants (env-var driven)
├── hot.md                          # Active projects, lessons, session summary
├── session.md                      # Current session state
├── projects/                       # Per-project warm files
├── loop_ledger.db                  # Loop detection event log
├── git_watcher_state.db            # Repo tracking state
├── agent_msgqueue.db               # Agent message store
└── taskqueue.db                    # Autonomous task queue

~/.cortexdb/
├── agent_system.db                 # CortexDB memory store
└── memory-daemon.log               # Daemon logs
```

---

## Operations

```bash
# Service management
systemctl --user status agent-memory-daemon
systemctl --user restart agent-memory-daemon
journalctl --user -u agent-memory-daemon -f

# Verify everything is working
bash install.sh --verify

# Update to latest kit
bash install.sh --update

# Uninstall (preserves data)
bash install.sh --uninstall
```

---

## Security

- All Unix sockets are `chmod 0o600` — owner-only access
- All external path inputs sanitized (control chars stripped, symlinks resolved, traversal rejected)
- All string inputs have length caps
- No credentials in source — inject via environment variables
- Socket dir defaults to `/tmp` — override with `AGENT_SOCKET_DIR` for tighter control

---

## License

MIT
