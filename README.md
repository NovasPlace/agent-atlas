# Atlas

### Anamnesis for AI Agents

The opposite of amnesia. Agents that remember — forever.

Atlas is a persistent, multi-daemon memory and coordination system for AI agents. Background processes carry your agent's full context, history, and directives across unlimited conversations. Zero cold starts. No repeated mistakes. No context-length limits on operational memory.

## What It Does

Agents normally forget everything between conversations. This kit adds a full memory infrastructure that runs continuously in the background:

- **Persistent memory** across unlimited conversations (CortexDB — episodic, semantic, procedural, working memory)
- **Biological memory dynamics** — Ebbinghaus decay, Hebbian learning, emotional encoding, source monitoring, spreading activation
- **Hot context priming** — relevant memories + directive sections surfaced automatically at session start
- **Cross-session coordination** — multiple agents share state, claim resources, and avoid conflicts
- **Autonomous task queue** — agents defer and schedule work across sessions
- **Real-time event bus** — PostgreSQL LISTEN/NOTIFY for cross-conversation awareness
- **Loop detection** — auto-fires mayday + lesson on repeated identical tool calls
- **Git awareness** — every commit in every watched repo becomes a searchable episodic memory
- **Context pressure monitoring** — signals pre-emptive flush before truncation hits
- **Agent-to-agent messaging** — async inbox/outbox between agent sessions
- **Self-improving lesson engine** — consolidation, reinforcement, stale detection, domain templates
- **Hallucination scanner** — AST-based import verification on recent file changes

## CortexDB — Cognitive Memory Engine

The core of Atlas. An 884-line SQLite-backed engine implementing biologically-inspired memory dynamics:

| Feature | Description |
|---------|-------------|
| **4 Memory Types** | Episodic, procedural, semantic, relational |
| **Ebbinghaus Decay** | Memories fade over time unless reinforced |
| **Emotional Encoding** | Fear memories surface first, emotion boosts importance |
| **Hebbian Learning** | Memories co-recalled together strengthen their pathways |
| **Source Monitoring** | Confidence penalties: experienced(1.0) > told(0.85) > generated(0.75) > inferred(0.65) |
| **Flashbulb Detection** | High-emotion events get permanent elevated importance |
| **FTS5 Search** | Full-text search ranked by relevance × importance |
| **Spreading Activation** | Related memories prime each other via the PrimingEngine |
| **Working Memory** | Short-term task context buffer |
| **Cognitive Biases** | Recency bias, availability bias modeling |
| **Autobiographical Memory** | Self-narrative construction from episodic traces |

## Requirements

- Linux with systemd (Ubuntu 22.04+, Debian 12+, Arch)
- Python 3.11+
- git (for GitWatcherDaemon)
- PostgreSQL (optional — for pg_broadcast real-time events)

## Install

```bash
bash install.sh
```

The installer:
1. Checks dependencies
2. Installs Python deps
3. Deploys all daemon files to `~/.gemini/memory/`
4. Bootstraps `hot.md`
5. Installs + enables systemd user services
6. Starts all daemons
7. Indexes your agent directive into CortexDB
8. Runs the full integration test suite

## Configuration

All paths are configurable via environment variables:

```bash
export AGENT_MEMORY_DIR="$HOME/.gemini/memory"     # markdown memory files
export AGENT_CORTEX_ROOT="$HOME/.cortexdb"          # CortexDB SQLite store
export AGENT_SOCKET_DIR="/tmp"                      # Unix socket directory
export AGENT_LOOP_THRESHOLD="3"                     # loop detection sensitivity
export AGENT_GIT_POLL_INTERVAL="60"                 # git watcher poll seconds
export AGENT_PRESSURE_LIMIT="150000"                # context token limit
export AGENT_MSG_TTL="172800"                       # message queue TTL (48h)
export AGENT_SYSTEM_DIR="$HOME/projects"            # agent project root
```

## The Daemons

| Daemon | Socket/Service | Function |
|--------|---------------|----------|
| **MemoryReader** | `agent-memory-reader.sock` | Fast reads from tiered memory |
| **MemoryWriter** | `agent-memory-writer.sock` | Atomic writes with validation |
| **Coordinator** | `agent-coord.sock` | Multi-agent presence + resource claims |
| **TaskQueue** | `agent-taskqueue.sock` | Cross-session task scheduling |
| **LoopDetector** | `agent-loop-detector.sock` | Repeated tool call detection |
| **GitWatcher** | `agent-git-watcher.sock` | Commit → episodic memory pipeline |
| **ContextPressure** | `agent-context-pressure.sock` | Token budget monitoring |
| **MessageQueue** | `agent-msgqueue.sock` | Agent-to-agent async messaging |
| **MemorySync** | systemd timer | Warm file synchronization |
| **SessionJournal** | systemd timer | Session continuity file |
| **SessionBriefing** | on-demand | Session start context priming |
| **Subconscious** | background | File watcher → CortexDB episodic memory |
| **HallucinationScanner** | background | AST-verified import checking |
| **LessonEngine** | on-demand | Self-improving failure memory |
| **Consolidator** | background | Redundant lesson merging |
| **Compactor** | on-demand | Hot memory budget enforcement |

## Agent API

The `agent_memory_api.py` module is the single entry point:

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
```

## Directory Structure

```
~/.gemini/memory/                    # All daemon and support files
├── agent_memory_daemon.py           # Orchestrator (starts all daemons)
├── agent_memory_api.py              # Agent-facing unified API
├── config.py                        # All paths/constants (env-var driven)
├── hot.md                           # Active projects, lessons, session summary
├── session.md                       # Current session state
├── projects/                        # Per-project warm files
├── cortex/                          # CortexDB cognitive engine
│   ├── engine.py                    # 884-line memory engine (decay, Hebbian, FTS5)
│   ├── priming.py                   # Spreading activation
│   ├── working_memory.py            # Short-term task context
│   ├── autobio.py                   # Autobiographical narrative
│   ├── cognitive_biases.py          # Bias modeling
│   └── trace.py                     # Execution tracing
├── loop_ledger.db                   # Loop detection event log
├── git_watcher_state.db             # Repo tracking state
├── agent_msgqueue.db                # Agent message store
└── taskqueue.db                     # Autonomous task queue

~/.cortexdb/
├── agent_system.db                  # CortexDB memory store
└── memory-daemon.log                # Daemon logs
```

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

## Security

- All Unix sockets are `chmod 0o600` — owner-only access
- All external path inputs sanitized (control chars stripped, symlinks resolved, traversal rejected)
- All string inputs have length caps
- No credentials in source — inject via environment variables
- Socket dir defaults to `/tmp` — override with `AGENT_SOCKET_DIR` for tighter control

## License

MIT
