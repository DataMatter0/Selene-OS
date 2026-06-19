"""
server/state.py — Live server state and WebSocket broadcast
─────────────────────────────────────────────────────────────
Owns:
  clients          — set of connected WebSocket instances
  _prev_writing    — autonomy state change tracking
  _cached_emotion  — post-turn emotion snapshot (avoids neutral polling noise)
  get_state()      — JSON-serialisable snapshot of Selene's live state
  broadcast()      — fan-out to all connected clients
  _state_broadcaster() — background task: 2s poll → state + autonomy events
"""

import asyncio
from typing import TYPE_CHECKING, Set

from fastapi import WebSocket

# Populated at startup by startup.py
selene_ref = None   # set by startup._init_selene via set_selene()

clients: Set[WebSocket] = set()
_prev_writing: bool = False
_cached_emotion: dict = {"mood_index": 0, "emotion": ""}


def set_selene(instance) -> None:
    """Called by startup after LLMChat is constructed."""
    global selene_ref
    selene_ref = instance


def get_state() -> dict:
    """JSON-serialisable snapshot of Selene's live state."""
    selene = selene_ref
    if selene is None:
        return {
            "status":            "offline",
            "creative_energy":   0,
            "memory_count":      0,
            "is_running":        False,
            "conversation_id":   None,
            "conversation_name": "New Conversation",
            "active_agent":      "selene",
            "tools": [
                "chronicle_manager",
                "memory_tool",
                "status_checker",
                "manifest_manager",
                "todo",
                "schedule_manager",
                "knowledge_manager",
                "file_manager",
                "document_reader",
                "runereader",
                "maps",
                "notion",
                "youtube"
            ],
            "dashboard_layout": {
                "left": "fused_manifest",
                "center": "main_chat",
                "right": "status_panel"
            }
        }
    with selene.lock:
        active_agent = getattr(selene, "active_agent_name", "Selene").lower()
        layout = getattr(selene, "dashboard_layout", {
            "left": "fused_manifest",
            "center": "main_chat",
            "right": "status_panel"
        })
        return {
            "status":            "writing" if selene.is_writing_autonomously else "idle",
            "creative_energy":   selene.creative_energy,
            "memory_count":      len(selene.working_memory) // 2,
            "is_running":        selene.is_running,
            "conversation_id":   selene.active_conversation_id,
            "conversation_name": selene.active_conversation_name,
            "active_agent":      active_agent,
            "agent_meta": {
                "name":          getattr(selene, "active_agent_name",  active_agent.capitalize()),
                "title":         getattr(selene, "active_agent_title", ""),
                "domain":        getattr(selene, "active_agent_domain", ""),
                "color_primary": getattr(selene, "active_agent_color", "#ffffff"),
                "slug":          getattr(selene, "active_agent_slug",  active_agent),
            },
            "tools":             getattr(selene, "allowed_tools", []),
            "dashboard_layout":  layout,
            "mood_index":        _cached_emotion["mood_index"],
            "emotion":           _cached_emotion["emotion"],
        }


async def broadcast(message: dict) -> None:
    """Send a JSON payload to every connected client, pruning dead sockets."""
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for d in dead:
        clients.discard(d)


async def _state_broadcaster() -> None:
    """
    Background asyncio task.
    Every 2s: broadcasts current state and fires autonomy_start / autonomy_end
    events when Selene's writing status changes.
    """
    global _prev_writing
    while True:
        await asyncio.sleep(2)
        if not clients:
            continue

        state      = get_state()
        is_writing = state["status"] == "writing"

        if is_writing != _prev_writing:
            _prev_writing = is_writing
            await broadcast({"type": "autonomy_start" if is_writing else "autonomy_end"})

        await broadcast({"type": "state", "data": state})
