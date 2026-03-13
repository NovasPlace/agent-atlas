#!/usr/bin/env bash
# install.sh — Agent Memory Kit installer
#
# One-command setup for the full 16-daemon agent memory stack.
# Tested on: Ubuntu 22.04+, Debian 12+, Arch Linux (systemd required)
#
# Usage:
#   bash install.sh              # full install
#   bash install.sh --uninstall  # remove services (keeps data)
#   bash install.sh --verify     # run integration test suite
#   bash install.sh --update     # pull latest + restart daemons

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
info() { echo -e "${CYAN}  →${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗ FATAL:${NC} $*" >&2; exit 1; }

echo -e "\n${BOLD}╔══════════════════════════════════════════╗"
echo -e "║          Atlas — Anamnesis for AI        ║"
echo -e "║              Agent Memory Kit            ║"
echo -e "╚══════════════════════════════════════════╝${NC}\n"

# ── Config ────────────────────────────────────────────────────
MEMORY_DIR="${AGENT_MEMORY_DIR:-$HOME/.gemini/memory}"
CORTEX_DIR="${AGENT_CORTEX_DIR:-$HOME/.cortexdb}"
CORTEX_ROOT="${AGENT_CORTEX_ROOT:-$HOME/.cortexdb}"
SOCKET_DIR="${AGENT_SOCKET_DIR:-/tmp}"
SERVICE_DIR="$HOME/.config/systemd/user"
PYTHON="${PYTHON:-python3}"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Mode ──────────────────────────────────────────────────────
MODE="${1:-install}"

# ── Pre-flight checks ─────────────────────────────────────────
preflight() {
    info "Running pre-flight checks..."

    command -v "$PYTHON" >/dev/null 2>&1 \
        || die "Python 3 not found. Install python3 (3.11+ required)."

    PY_VER=$("$PYTHON" -c "import sys; print(sys.version_info[:2])")
    [[ "$PY_VER" < "(3, 11)" ]] || true  # just informational

    command -v git >/dev/null 2>&1 \
        || warn "git not found — GitWatcherDaemon will have reduced functionality."

    command -v systemctl >/dev/null 2>&1 \
        || die "systemd not found. This kit requires a systemd-based Linux system."

    systemctl --user status >/dev/null 2>&1 \
        || die "systemd user session not available. Enable with: loginctl enable-linger $USER"

    ok "Pre-flight checks passed"
}

# ── Directory setup ───────────────────────────────────────────
setup_dirs() {
    info "Creating directories..."
    mkdir -p "$MEMORY_DIR"
    mkdir -p "$MEMORY_DIR/projects"
    mkdir -p "$CORTEX_DIR"
    mkdir -p "$SERVICE_DIR"
    ok "Directories ready"
}

# ── Python dependencies ───────────────────────────────────────
install_deps() {
    info "Installing Python dependencies..."
    "$PYTHON" -m pip install --quiet --user psycopg2-binary \
        || warn "psycopg2-binary install failed — pg_broadcast will be disabled."
    ok "Python dependencies installed"

    # Verify CortexDB is importable
    info "Checking CortexDB..."
    if "$PYTHON" -c "import sys; sys.path.insert(0,'$CORTEX_ROOT'); from cortex.engine import Cortex" 2>/dev/null; then
        ok "CortexDB found at $CORTEX_ROOT"
    else
        warn "CortexDB not found at $CORTEX_ROOT"
        warn "Install CortexDB or set AGENT_CORTEX_ROOT env var."
        warn "Continuing — daemons that don't need CortexDB will still work."
    fi
}

# ── Copy kit files ────────────────────────────────────────────
install_files() {
    info "Installing memory kit files to $MEMORY_DIR..."

    # List of all daemon and support files to install
    KIT_FILES=(
        agent_memory_daemon.py
        agent_memory_api.py
        md_reader.py
        md_writer.py
        md_indexer.py
        context_recall.py
        subconscious.py
        lesson_engine.py
        session_journal.py
        session_briefing.py
        memory_bridge.py
        memory_sync.py
        agent_coord.py
        agent_taskqueue.py
        pg_broadcast.py
        loop_detector.py
        git_watcher.py
        context_pressure.py
        agent_msgqueue.py
        directive_indexer.py
        consolidator.py
        compact.py
        freshness.py
        maintain.py
        hallucination_scanner.py
        config.py
        requirements.txt
    )

    INSTALLED=0
    SKIPPED=0
    for f in "${KIT_FILES[@]}"; do
        src="$KIT_DIR/$f"
        dst="$MEMORY_DIR/$f"
        if [[ -f "$src" ]]; then
            cp "$src" "$dst"
            ((INSTALLED++))
        else
            warn "Skipping missing file: $f"
            ((SKIPPED++))
        fi
    done

    ok "Installed $INSTALLED file(s) — $SKIPPED skipped"
}

# ── hot.md bootstrap ──────────────────────────────────────────
bootstrap_hot_md() {
    HOT="$MEMORY_DIR/hot.md"
    if [[ -f "$HOT" ]]; then
        info "hot.md already exists — skipping bootstrap"
        return
    fi

    info "Bootstrapping hot.md..."
    cat > "$HOT" << 'EOF'
# AGENT MEMORY — HOT CONTEXT

## ACTIVE PROJECTS
| Name | Path | Status | Warm File |
|------|------|--------|-----------|

## OPERATOR NOTES
- Stack: (set your preferred stack here)
- Preferences: (add your preferences here)

## RECENT LESSONS
(populated automatically by lesson_engine)

## SESSION SUMMARY
Fresh install — no sessions yet.
EOF
    ok "hot.md bootstrapped"
}

# ── systemd services ──────────────────────────────────────────
install_services() {
    info "Installing systemd user services..."

    # Main daemon service
    cat > "$SERVICE_DIR/agent-memory-daemon.service" << EOF
[Unit]
Description=Agent Memory Daemon (16-daemon memory stack)
Documentation=file://${MEMORY_DIR}/README.md
After=network.target

[Service]
Type=simple
WorkingDirectory=${MEMORY_DIR}
ExecStart=${PYTHON} ${MEMORY_DIR}/agent_memory_daemon.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${CORTEX_DIR}/memory-daemon.log
StandardError=append:${CORTEX_DIR}/memory-daemon.log
TimeoutStopSec=15
Environment=AGENT_MEMORY_DIR=${MEMORY_DIR}
Environment=AGENT_CORTEX_DIR=${CORTEX_DIR}
Environment=AGENT_CORTEX_ROOT=${CORTEX_ROOT}
Environment=AGENT_SOCKET_DIR=${SOCKET_DIR}

[Install]
WantedBy=default.target
EOF

    # Maintenance timer (runs maintain.py daily)
    cat > "$SERVICE_DIR/agent-maintain.service" << EOF
[Unit]
Description=Agent Memory Maintenance
After=agent-memory-daemon.service

[Service]
Type=oneshot
WorkingDirectory=${MEMORY_DIR}
ExecStart=${PYTHON} ${MEMORY_DIR}/maintain.py
Environment=AGENT_MEMORY_DIR=${MEMORY_DIR}
Environment=AGENT_CORTEX_DIR=${CORTEX_DIR}
Environment=AGENT_CORTEX_ROOT=${CORTEX_ROOT}
EOF

    cat > "$SERVICE_DIR/agent-maintain.timer" << EOF
[Unit]
Description=Daily Agent Memory Maintenance

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable agent-memory-daemon.service
    systemctl --user enable agent-maintain.timer

    ok "Services installed and enabled"
}

# ── Start daemons ─────────────────────────────────────────────
start_daemons() {
    info "Starting agent-memory-daemon..."
    systemctl --user restart agent-memory-daemon.service
    systemctl --user start agent-maintain.timer

    # Wait for sockets to appear
    info "Waiting for daemons to come up..."
    MAX_WAIT=30
    elapsed=0
    while [[ $elapsed -lt $MAX_WAIT ]]; do
        if [[ -S "$SOCKET_DIR/agent-memory-reader.sock" ]] && \
           [[ -S "$SOCKET_DIR/agent-memory-writer.sock" ]]; then
            break
        fi
        sleep 1
        ((elapsed++))
    done

    if [[ -S "$SOCKET_DIR/agent-memory-reader.sock" ]]; then
        ok "Daemons are up (${elapsed}s)"
    else
        warn "Sockets not detected after ${MAX_WAIT}s — check logs:"
        warn "  journalctl --user -u agent-memory-daemon -n 30"
    fi
}

# ── Index directives ──────────────────────────────────────────
index_directives() {
    DIRECTIVE="$HOME/.gemini/GEMINI.md"
    if [[ ! -f "$DIRECTIVE" ]]; then
        warn "No directive found at $DIRECTIVE — skipping directive indexing"
        return
    fi
    info "Indexing agent directives into CortexDB..."
    PYTHONPATH="$CORTEX_ROOT:$MEMORY_DIR" \
        "$PYTHON" "$MEMORY_DIR/directive_indexer.py" \
        --directive "$DIRECTIVE" \
        --db "$CORTEX_DIR/agent_system.db" 2>&1 | grep -E "Indexed|SKIP|Done" || true
    ok "Directives indexed"
}

# ── Verify installation ───────────────────────────────────────
verify_install() {
    info "Running integration test suite..."
    sleep 3   # Brief settle time
    if PYTHONPATH="$CORTEX_ROOT:$MEMORY_DIR" \
        "$PYTHON" "$MEMORY_DIR/agent_memory_api.py" --test-mode 2>&1; then
        ok "All checks passed — installation verified"
    else
        warn "Some checks failed. Check logs: $CORTEX_DIR/memory-daemon.log"
        return 1
    fi
}

# ── Uninstall ─────────────────────────────────────────────────
uninstall() {
    info "Stopping and disabling services..."
    systemctl --user stop agent-memory-daemon.service 2>/dev/null || true
    systemctl --user stop agent-maintain.timer 2>/dev/null || true
    systemctl --user disable agent-memory-daemon.service 2>/dev/null || true
    systemctl --user disable agent-maintain.timer 2>/dev/null || true
    rm -f "$SERVICE_DIR/agent-memory-daemon.service"
    rm -f "$SERVICE_DIR/agent-maintain.service"
    rm -f "$SERVICE_DIR/agent-maintain.timer"
    systemctl --user daemon-reload
    ok "Services removed (data preserved in $MEMORY_DIR and $CORTEX_DIR)"
    info "To fully remove: rm -rf $MEMORY_DIR $CORTEX_DIR"
}

# ── Update ────────────────────────────────────────────────────
update() {
    info "Stopping daemon for update..."
    systemctl --user stop agent-memory-daemon.service 2>/dev/null || true
    install_files
    index_directives
    systemctl --user start agent-memory-daemon.service
    ok "Update complete"
}

# ── Main ──────────────────────────────────────────────────────
case "$MODE" in
    --uninstall)
        uninstall
        ;;
    --verify)
        verify_install
        ;;
    --update)
        preflight
        update
        ;;
    install|--install|"")
        preflight
        setup_dirs
        install_deps
        install_files
        bootstrap_hot_md
        install_services
        start_daemons
        index_directives
        echo ""
        if verify_install; then
            echo -e "\n${BOLD}${GREEN}╔══════════════════════════════════════════╗"
            echo -e "║      Agent Memory Kit — INSTALLED  ✓     ║"
            echo -e "╚══════════════════════════════════════════╝${NC}"
            echo ""
            echo -e "  Start a new agent session and run:"
            echo -e "  ${CYAN}python3 $MEMORY_DIR/agent_memory_api.py context${NC}"
            echo -e ""
            echo -e "  Logs: ${CYAN}$CORTEX_DIR/memory-daemon.log${NC}"
            echo -e "  Docs: ${CYAN}$MEMORY_DIR/README.md${NC}"
        else
            echo -e "\n${YELLOW}Installation complete — some checks need attention.${NC}"
            echo -e "Review: $CORTEX_DIR/memory-daemon.log"
        fi
        ;;
    *)
        die "Unknown mode: $MODE. Use: install | --uninstall | --verify | --update"
        ;;
esac
