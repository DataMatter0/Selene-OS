"""
selene_brain/agent_protocol.py — AgentState structural Protocol
────────────────────────────────────────────────────────────────
Defines the minimal interface that tools expect from the active agent.
Use this instead of importing LLMChat directly in tool files — it keeps
tools decoupled from the concrete brain implementation so any future agent
class satisfies the contract without inheriting from LLMChat.

Usage in tools (TYPE_CHECKING only — zero runtime cost):

    from __future__ import annotations
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from selene_brain.agent_protocol import AgentState
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class AgentState(Protocol):
    """
    Structural interface for the active agent passed to tools as agent_state.
    All attributes here are set by LLMChat.__init__ or swap_agent().
    Tools must only access attributes declared here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    active_agent_name:  str
    active_agent_slug:  str
    active_agent_title: str
    allowed_tools:      List[str]

    # ── Concurrency ───────────────────────────────────────────────────────────
    lock: threading.RLock

    # ── Memory & state paths ──────────────────────────────────────────────────
    MEMORY_DIR:           str   # agents/{slug}/ — all per-agent files live here
    USER_PROFILE_FILE:    str   # agents/{slug}/user_profile.md
    CHARACTER_PROFILE_FILE: str # agents/{slug}/character_profile.md

    # ── Database handle ───────────────────────────────────────────────────────
    db: Any   # AgentMemoryStore — typed as Any to avoid circular import

    # ── LLM access ───────────────────────────────────────────────────────────
    llm_caller:  Any   # LLMCaller
    tool_router: Any   # ToolRouter

    # ── Runtime state ────────────────────────────────────────────────────────
    working_memory:          List[Dict[str, Any]]
    creative_energy:         int
    is_writing_autonomously: bool

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _read_file_safe(self, path: str, default: str = "") -> str: ...
