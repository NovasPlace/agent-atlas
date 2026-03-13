"""Governance Client — Interface to Token Metabolism, Consistency, and Grounding daemons.

Fail-open design: if any daemon is unreachable, operations proceed normally.
The system continues working even when governance is down.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger("agent-memory.governance")

# Port registry integration (falls back to hardcoded defaults)
try:
    import sys
    sys.path.insert(0, os.path.expanduser("~/projects"))
    from sovereign_ports import get_service_url
    _METABOLISM_URL = get_service_url("token-metabolism")
    _CONSISTENCY_URL = get_service_url("consistency-daemon")
    _GROUNDING_URL = get_service_url("grounding-daemon")
except Exception:
    _METABOLISM_URL = "http://127.0.0.1:8417"
    _CONSISTENCY_URL = "http://127.0.0.1:8418"
    _GROUNDING_URL = "http://127.0.0.1:8419"

TIMEOUT = 3.0  # seconds — governance must never block the agent


@dataclass
class VerificationResult:
    """Result of a consistency or grounding check."""
    passed: bool
    score: float = 1.0  # 0.0 = failed, 1.0 = passed
    reason: str = ""
    daemon_available: bool = True


@dataclass
class AllocationResult:
    """Result of a metabolism allocation request."""
    granted: bool = True
    delay_ms: float = 0.0
    budget_remaining: int = -1
    daemon_available: bool = True


def _safe_post(url: str, payload: dict) -> dict | None:
    """POST to a daemon. Returns None on any failure (fail-open)."""
    try:
        resp = httpx.post(url, json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        log.warning("Governance %s returned %d", url, resp.status_code)
        return None
    except Exception as e:
        log.debug("Governance %s unreachable: %s", url, e)
        return None


def _safe_get(url: str) -> dict | None:
    """GET from a daemon. Returns None on any failure."""
    try:
        resp = httpx.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


# ── Consistency Daemon (port 8418) ──────────────────────

def verify_consistency(text: str, context: str = "") -> VerificationResult:
    """Check text for reasoning consistency before persisting.

    Fail-open: if daemon is down, returns passed=True.
    """
    data = _safe_post(f"{_CONSISTENCY_URL}/verify", {
        "text": text,
        "context": context,
    })
    if data is None:
        return VerificationResult(passed=True, reason="daemon unavailable", daemon_available=False)

    return VerificationResult(
        passed=data.get("consistent", True),
        score=data.get("score", 1.0),
        reason=data.get("reason", ""),
        daemon_available=True,
    )


# ── Grounding Daemon (port 8419) ───────────────────────

def verify_grounding(text: str, context: str = "") -> VerificationResult:
    """Check text for factual grounding before persisting.

    Fail-open: if daemon is down, returns passed=True.
    """
    data = _safe_post(f"{_GROUNDING_URL}/ground", {
        "text": text,
        "context": context,
    })
    if data is None:
        return VerificationResult(passed=True, reason="daemon unavailable", daemon_available=False)

    return VerificationResult(
        passed=data.get("grounded", True),
        score=data.get("score", 1.0),
        reason=data.get("reason", ""),
        daemon_available=True,
    )


# ── Token Metabolism (port 8417) ────────────────────────

# Priority tiers matching token-metabolism PriorityTier enum
PRIORITY_CRITICAL = 0
PRIORITY_HIGH = 1
PRIORITY_STANDARD = 2
PRIORITY_LOW = 3
PRIORITY_IDLE = 4


def allocate_tokens(
    caller: str = "agent-memory",
    priority: int = PRIORITY_STANDARD,
    estimated_tokens: int = 500,
) -> AllocationResult:
    """Request token allocation from metabolism.

    Fail-open: if daemon is down, returns granted=True.
    """
    data = _safe_post(f"{_METABOLISM_URL}/allocate", {
        "caller_id": caller,
        "priority": priority,
        "estimated_tokens": estimated_tokens,
    })
    if data is None:
        return AllocationResult(daemon_available=False)

    return AllocationResult(
        granted=data.get("granted", True),
        delay_ms=data.get("delay_ms", 0.0),
        budget_remaining=data.get("budget_remaining", -1),
        daemon_available=True,
    )


def report_usage(
    caller: str = "agent-memory",
    tokens: int = 0,
    latency_ms: float = 0.0,
    cache_hit: bool = False,
) -> bool:
    """Report actual token usage to metabolism. Returns True on success."""
    data = _safe_post(f"{_METABOLISM_URL}/report", {
        "caller_id": caller,
        "tokens": tokens,
        "latency_ms": latency_ms,
        "cache_hit": cache_hit,
    })
    return data is not None


# ── Combined verification gate ──────────────────────────

def verify_before_persist(
    content: str,
    context: str = "",
    check_consistency: bool = True,
    check_grounding: bool = True,
) -> tuple[bool, str]:
    """Run all applicable verification checks before persisting content.

    Returns (should_persist, reason).
    Fail-open: if all daemons are down, returns (True, "governance unavailable").
    """
    reasons = []

    if check_consistency:
        result = verify_consistency(content, context)
        if not result.passed and result.daemon_available:
            reasons.append(f"consistency: {result.reason}")
            log.warning("Content failed consistency check: %s", result.reason)
            return False, "; ".join(reasons)

    if check_grounding:
        result = verify_grounding(content, context)
        if not result.passed and result.daemon_available:
            reasons.append(f"grounding: {result.reason}")
            log.warning("Content failed grounding check: %s", result.reason)
            return False, "; ".join(reasons)

    return True, ""


# ── Health check ────────────────────────────────────────

def governance_health() -> dict[str, bool]:
    """Check which governance daemons are reachable."""
    return {
        "metabolism": _safe_get(f"{_METABOLISM_URL}/health") is not None,
        "consistency": _safe_get(f"{_CONSISTENCY_URL}/health") is not None,
        "grounding": _safe_get(f"{_GROUNDING_URL}/health") is not None,
    }
