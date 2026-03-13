"""ContextPressureDaemon — Estimates context window pressure per session.

Agents report each tool call (name + output size). The daemon accumulates
a token-usage estimate using a conservative heuristic model and returns
a pressure score (0.0–1.0) plus an action recommendation.

At pressure ≥ 0.65 → RECOMMEND_FLUSH (plan a write-session call soon)
At pressure ≥ 0.85 → URGENT_FLUSH   (flush immediately before next tool)

Heuristic model (conservative — tuned for Gemini 1.5 Pro @ 1M token context):
  - System baseline (hot.md, directives, prompt):  ~3 000 tokens
  - Per tool call, average overhead:               ~  500 tokens
  - Per output char:                               ~0.25 tokens (4 chars/tok)
  - After flush (write-session), estimator resets  ~30% (context shrinks but
    model still holds tool history)

Socket: /tmp/agent-context-pressure.sock

Protocol:
  TICK   {session_id, tool, output_chars?}   → {ok, pressure, action}
  FLUSH  {session_id}                        → notify daemon of flush (resets estimate)
  STATUS {session_id?}                       → current pressure breakdown
  RESET  {session_id}                        → hard reset (new session)
  PING   {}                                  → health check

action values:
  "ok"              — pressure < 0.65, all good
  "recommend_flush" — pressure 0.65–0.84, flush soon
  "urgent_flush"    — pressure ≥ 0.85, flush NOW

Usage (daemon integration):
    from context_pressure import run_pressure_daemon
    await run_pressure_daemon(shutdown_event)

Usage (self-test):
    python3 context_pressure.py --test-mode
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger("context-pressure")

SOCKET_PATH = "/tmp/agent-context-pressure.sock"
MAX_MSG_BYTES = 16_384

# ── Heuristic constants ─────────────────────────────────────

BASELINE_TOKENS    = 3_000    # Hot.md + directives + system prompt
TOKENS_PER_CALL    = 500      # Average overhead per tool invocation
CHARS_PER_TOKEN    = 4.0      # Conservative estimate (Gemini tokenizer)
MODEL_SAFE_LIMIT   = 150_000  # Stay well under 1M — flush before it hurts

# Flush resets context pressure but not to zero (history is still there)
FLUSH_RETAIN_RATIO = 0.35

THRESHOLD_RECOMMEND = 0.65
THRESHOLD_URGENT    = 0.85

SESSION_TTL_S = 3600  # 1 hour idle → reap


# ── Session State ───────────────────────────────────────────

@dataclass
class PressureState:
    session_id:   str
    call_count:   int   = 0
    output_chars: int   = 0
    flush_count:  int   = 0
    last_seen:    float = field(default_factory=time.monotonic)

    def tick(self, output_chars: int = 0) -> None:
        self.call_count   += 1
        self.output_chars += max(0, output_chars)
        self.last_seen     = time.monotonic()

    def flush(self) -> None:
        """Approximate the reduction in context pressure after a flush."""
        self.call_count   = int(self.call_count   * FLUSH_RETAIN_RATIO)
        self.output_chars = int(self.output_chars * FLUSH_RETAIN_RATIO)
        self.flush_count += 1
        self.last_seen    = time.monotonic()

    @property
    def estimated_tokens(self) -> int:
        call_tokens   = self.call_count * TOKENS_PER_CALL
        output_tokens = int(self.output_chars / CHARS_PER_TOKEN)
        return BASELINE_TOKENS + call_tokens + output_tokens

    @property
    def pressure(self) -> float:
        return min(1.0, self.estimated_tokens / MODEL_SAFE_LIMIT)

    @property
    def action(self) -> str:
        p = self.pressure
        if p >= THRESHOLD_URGENT:
            return "urgent_flush"
        if p >= THRESHOLD_RECOMMEND:
            return "recommend_flush"
        return "ok"

    def is_stale(self) -> bool:
        return (time.monotonic() - self.last_seen) > SESSION_TTL_S


# ── Command Handlers ────────────────────────────────────────

async def _handle_command(cmd_obj: dict, sessions: dict) -> dict:
    cmd = cmd_obj.get("cmd", "")

    if cmd == "PING":
        return {"ok": True, "pong": True, "active_sessions": len(sessions)}

    if cmd == "TICK":
        sid          = str(cmd_obj.get("session_id", "default")).strip()[:64]
        output_chars = int(cmd_obj.get("output_chars", 0))

        if sid not in sessions:
            sessions[sid] = PressureState(session_id=sid)

        state = sessions[sid]
        state.tick(output_chars)

        return {
            "ok":               True,
            "pressure":         round(state.pressure, 3),
            "action":           state.action,
            "estimated_tokens": state.estimated_tokens,
            "call_count":       state.call_count,
        }

    if cmd == "FLUSH":
        sid = str(cmd_obj.get("session_id", "default")).strip()[:64]
        if sid in sessions:
            sessions[sid].flush()
            state = sessions[sid]
            return {
                "ok":               True,
                "pressure":         round(state.pressure, 3),
                "action":           state.action,
                "estimated_tokens": state.estimated_tokens,
            }
        return {"ok": True, "pressure": 0.0, "action": "ok"}

    if cmd == "STATUS":
        sid = cmd_obj.get("session_id")
        if sid:
            state = sessions.get(str(sid))
            if not state:
                return {"ok": True, "sessions": {}}
            return {
                "ok": True,
                "sessions": {
                    sid: {
                        "pressure":         round(state.pressure, 3),
                        "action":           state.action,
                        "estimated_tokens": state.estimated_tokens,
                        "call_count":       state.call_count,
                        "output_chars":     state.output_chars,
                        "flush_count":      state.flush_count,
                        "last_seen_ago_s":  round(time.monotonic() - state.last_seen, 1),
                    }
                },
            }
        return {
            "ok": True,
            "sessions": {
                s_id: {
                    "pressure":   round(s.pressure, 3),
                    "action":     s.action,
                    "call_count": s.call_count,
                }
                for s_id, s in sessions.items()
            },
        }

    if cmd == "RESET":
        sid = str(cmd_obj.get("session_id", "default")).strip()[:64]
        sessions.pop(sid, None)
        return {"ok": True}

    return {"ok": False, "error": f"Unknown command: {cmd!r}"}


# ── Connection / Reaper / Daemon ────────────────────────────

async def _handle_connection(reader, writer, sessions) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(MAX_MSG_BYTES), timeout=5.0)
        if not raw:
            return
        try:
            cmd_obj = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            response = {"ok": False, "error": f"JSON parse error: {e}"}
        else:
            response = await _handle_command(cmd_obj, sessions)
        writer.write(json.dumps(response).encode() + b"\n")
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


async def _reaper(sessions: dict, shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=300.0)
            break
        except asyncio.TimeoutError:
            stale = [s for s, st in sessions.items() if st.is_stale()]
            for s in stale:
                del sessions[s]
                logger.info("Reaped stale pressure session: %s", s)


async def run_pressure_daemon(
    shutdown_event: asyncio.Event | None = None,
    socket_path: str = SOCKET_PATH,
) -> None:
    sessions: dict[str, PressureState] = {}
    _shutdown = shutdown_event or asyncio.Event()

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    def _cb(r, w):
        asyncio.ensure_future(_handle_connection(r, w, sessions))

    server = await asyncio.start_unix_server(_cb, path=socket_path)
    os.chmod(socket_path, 0o600)
    logger.info("ContextPressureDaemon listening on %s", socket_path)

    reaper = asyncio.create_task(_reaper(sessions, _shutdown))
    await _shutdown.wait()
    reaper.cancel()
    server.close()
    await server.wait_closed()
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    logger.info("ContextPressureDaemon stopped.")


# ── Self-Test ───────────────────────────────────────────────

async def _self_test() -> bool:
    logger.info("Running ContextPressureDaemon self-test...")
    sock     = "/tmp/agent-context-pressure-test.sock"
    shutdown = asyncio.Event()
    task     = asyncio.create_task(run_pressure_daemon(shutdown, sock))
    await asyncio.sleep(0.1)

    async def _call(payload: dict) -> dict:
        r, w = await asyncio.open_unix_connection(sock)
        w.write(json.dumps(payload).encode() + b"\n")
        await w.drain()
        raw = await r.read(MAX_MSG_BYTES)
        w.close(); await w.wait_closed()
        return json.loads(raw.decode())

    try:
        resp = await _call({"cmd": "PING"})
        assert resp["pong"], f"PING failed: {resp}"

        # Fresh session — pressure should be low
        resp = await _call({"cmd": "TICK", "session_id": "s1", "output_chars": 500})
        assert resp["ok"] and resp["pressure"] < THRESHOLD_RECOMMEND, f"Unexpected pressure: {resp}"
        assert resp["action"] == "ok", f"Expected ok action: {resp}"

        # Simulate many calls to push past recommend threshold
        total_calls = int((MODEL_SAFE_LIMIT * THRESHOLD_RECOMMEND - BASELINE_TOKENS) / TOKENS_PER_CALL) + 1
        # Batch via direct state manipulation isn't possible — just do enough ticks
        for _ in range(200):
            resp = await _call({"cmd": "TICK", "session_id": "s2", "output_chars": 2000})
        assert resp["pressure"] >= THRESHOLD_RECOMMEND, f"Pressure didn't cross recommend: {resp}"

        # Keep going to urgent
        for _ in range(100):
            resp = await _call({"cmd": "TICK", "session_id": "s2", "output_chars": 2000})
        assert resp["pressure"] >= THRESHOLD_URGENT and resp["action"] == "urgent_flush", \
            f"Urgent threshold not reached: {resp}"

        # Flush should reduce pressure
        resp_before = resp["pressure"]
        resp = await _call({"cmd": "FLUSH", "session_id": "s2"})
        assert resp["pressure"] < resp_before, f"Flush didn't reduce pressure: {resp}"

        # Reset clears state
        await _call({"cmd": "RESET", "session_id": "s2"})
        resp = await _call({"cmd": "STATUS", "session_id": "s2"})
        assert not resp["sessions"], "Session should be gone after reset"

        logger.info("ContextPressureDaemon self-test PASSED")
        return True
    except Exception as e:
        logger.error("ContextPressureDaemon self-test FAILED: %s", e)
        import traceback; traceback.print_exc()
        return False
    finally:
        shutdown.set(); await task


# ── CLI ─────────────────────────────────────────────────────

def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="ContextPressureDaemon")
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--socket", default=SOCKET_PATH)
    args = parser.parse_args()

    if args.test_mode:
        raise SystemExit(0 if asyncio.run(_self_test()) else 1)
    asyncio.run(run_pressure_daemon(socket_path=args.socket))


if __name__ == "__main__":
    main()
