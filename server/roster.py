"""
server/roster.py — Dynamic agent roster built from agents/*/config.json

The roster is the single source of truth for which agents exist and what
they can do. No agent slug is hardcoded anywhere else in the system.

Usage:
    from server.roster import get_roster, get_agent, agent_has_cap, default_agent_slug

    roster = get_roster()               # full list, ordered by config order
    agent  = get_agent("sage")          # one agent dict, or None
    ok     = agent_has_cap("sage", "grant_access")
    slug   = default_agent_slug()       # slug with "default_boot" capability, or first
"""

import json
import os
from typing import Optional

_AGENTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "agents")
)

# In-memory cache — rebuilt on each call to reload_roster()
_roster: list = []
_roster_by_slug: dict = {}


def reload_roster() -> list:
    """
    Scan agents/*/config.json, build the roster list, cache it.
    Called once at startup (startup.py) and available for hot-reload.
    """
    global _roster, _roster_by_slug

    agents = []
    try:
        entries = sorted(os.listdir(_AGENTS_DIR))
    except FileNotFoundError:
        print(f"[Roster]: agents dir not found — {_AGENTS_DIR}")
        return []

    for entry in entries:
        agent_dir = os.path.join(_AGENTS_DIR, entry)
        config_path = os.path.join(agent_dir, "config.json")
        if not os.path.isdir(agent_dir) or not os.path.exists(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[Roster]: failed to load {config_path} — {e}")
            continue

        slug = entry.lower()
        # Derive color_glow from color_primary if not set
        color_primary = cfg.get("color_primary", "#888888")
        color_glow    = cfg.get("color_glow") or _derive_glow(color_primary)

        agent = {
            "slug":          slug,
            "name":          cfg.get("name", slug.capitalize()),
            "display_name":  cfg.get("display_name") or cfg.get("name", slug.capitalize()),
            "title":         cfg.get("title", ""),
            "domain":        cfg.get("domain", ""),
            "role":          cfg.get("role", "agent"),
            "color_primary": color_primary,
            "color_secondary": cfg.get("color_secondary", color_primary),
            "color_text":    cfg.get("color_text", "#ffffff"),
            "color_glow":    color_glow,
            "capabilities":  cfg.get("capabilities", []),
            "tools":         cfg.get("tools", []),
            "model":         cfg.get("model", ""),
            "model_path":    cfg.get("model_path", cfg.get("model", "")),
        }
        agents.append(agent)

    _roster = agents
    _roster_by_slug = {a["slug"]: a for a in agents}
    print(f"[Roster]: loaded {len(agents)} agents — {[a['slug'] for a in agents]}")
    return agents


def get_roster() -> list:
    """Return the cached roster. Loads if empty."""
    if not _roster:
        reload_roster()
    return list(_roster)


def get_agent(slug: str) -> Optional[dict]:
    """Return one agent by slug, or None."""
    if not _roster:
        reload_roster()
    return _roster_by_slug.get(slug.lower())


def agent_has_cap(slug: str, capability: str) -> bool:
    """True if the agent has the given capability string."""
    agent = get_agent(slug)
    if not agent:
        return False
    return capability in agent.get("capabilities", [])


def default_agent_slug() -> str:
    """Return slug of agent with 'default_boot' capability, or first agent."""
    if not _roster:
        reload_roster()
    for a in _roster:
        if "default_boot" in a.get("capabilities", []):
            return a["slug"]
    return _roster[0]["slug"] if _roster else "selene"


def agents_with_cap(capability: str) -> list:
    """Return all agents that have a given capability."""
    if not _roster:
        reload_roster()
    return [a for a in _roster if capability in a.get("capabilities", [])]


def build_ping_map() -> dict:
    """
    Build the @-mention slug → display_name map from the roster.
    e.g. {"selene": "Selene", "sage": "Sage", ...}
    Used by chat.py for @ routing.
    """
    if not _roster:
        reload_roster()
    return {a["slug"]: a["display_name"] for a in _roster}


def _derive_glow(hex_color: str) -> str:
    """Derive a low-opacity glow color from a hex color string."""
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},0.08)"
    except Exception:
        return "rgba(128,128,128,0.08)"
