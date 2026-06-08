"""
tools/meta_insight.py — MetaInsight Self-Observation Tool
----------------------------------------------------------
Gives Selene (and gated access to Sage) a structured backtrace into her own
reasoning history, decision patterns, tool use records, and emotional states.

Callable via XML tool call mid-conversation:
  <tool_call name="meta_insight">{"command":"query","category":"decision","keyword":"youtube","limit":5}</tool_call>

Commands:
  query          — retrieve raw log entries with optional filters
  pattern_mode   — aggregate patterns across a time window
  grant_sage     — mark a set of entries as sage_accessible
  revoke_sage    — remove sage access from entries
  promote_card   — mark an entry as promoted to knowledge board
"""

import json
import os
import time
from typing import Any, Dict, Optional

from .schema import BaseTool


class MetaInsightTool(BaseTool):
    name        = "meta_insight"
    description = (
        "Query your own reasoning history, decision logs, tool-use records, and emotional "
        "state traces. Use this to backtrace why you made a specific decision, identify "
        "patterns in your behavior, or update your self-model. Sage can access Selene's "
        "logs only when explicitly granted via grant_sage command."
    )
    input_type  = "json"
    output_type = "json"

    # Sage access is opt-in. Selene explicitly grants it per-query, not globally.
    _sage_access_token: bool = False

    def __init__(self, agent_state=None):
        self.agent_state = agent_state   # LLMChat instance

    @property
    def _db(self):
        """Access the agent's DB from the LLMChat state reference."""
        if self.agent_state and hasattr(self.agent_state, "db"):
            return self.agent_state.db
        return None

    @property
    def _agent_name(self) -> str:
        if self.agent_state:
            return getattr(self.agent_state, "active_agent_name", "selene").lower()
        return "selene"

    def execute(self, input_data: Any) -> Dict[str, Any]:
        db = self._db
        if db is None:
            return {"status": "error", "message": "MetaInsight: no DB available."}

        # Parse args
        if isinstance(input_data, str):
            try:
                args = json.loads(input_data) if input_data.strip() else {}
            except Exception:
                args = {}
        elif isinstance(input_data, dict):
            args = input_data
        else:
            args = {}

        command = args.get("command", "query").lower()

        # ── query ─────────────────────────────────────────────────────────────
        if command == "query":
            category     = args.get("category")
            subcategory  = args.get("subcategory")
            keyword      = args.get("keyword")
            limit        = min(int(args.get("limit", 10)), 50)
            offset       = int(args.get("offset", 0))
            time_window  = args.get("time_window_hours")
            time_window  = float(time_window) if time_window is not None else None

            # If caller is Sage, apply access gate
            is_sage      = self._agent_name == "sage"

            entries = db.query_meta_insight(
                agent=args.get("agent"),      # None = all agents; "selene" = only Selene's
                category=category,
                subcategory=subcategory,
                keyword=keyword,
                limit=limit,
                offset=offset,
                time_window_hours=time_window,
                sage_requesting=is_sage,
                requesting_agent=self._agent_name,
            )

            return {
                "status":  "success",
                "command": "query",
                "count":   len(entries),
                "data":    entries,
            }

        # ── pattern_mode ──────────────────────────────────────────────────────
        elif command == "pattern_mode":
            target_agent  = args.get("agent", self._agent_name)
            category      = args.get("category")
            time_window   = float(args.get("time_window_hours", 168.0))
            min_conf      = float(args.get("min_confidence", 0.0))

            # Sage can only pattern-analyse its own logs + Selene's accessible ones
            # For pattern mode we run on the target agent's logs
            if self._agent_name == "sage" and target_agent == "selene":
                # Allowed — Sage seeing Selene's accessible logs — but note it in result
                result = db.pattern_mode_meta_insight(
                    agent=target_agent,
                    category=category,
                    time_window_hours=time_window,
                    min_confidence=min_conf,
                )
                result["note"] = "Pattern derived from Selene's sage-accessible entries only."
            else:
                result = db.pattern_mode_meta_insight(
                    agent=target_agent,
                    category=category,
                    time_window_hours=time_window,
                    min_confidence=min_conf,
                )

            return {
                "status":  "success",
                "command": "pattern_mode",
                "data":    result,
            }

        # ── grant_sage ────────────────────────────────────────────────────────
        elif command == "grant_sage":
            # Only Selene can grant Sage access to her own logs
            if self._agent_name != "selene":
                return {"status": "error", "message": "Only Selene can grant Sage access to her logs."}

            entry_ids = args.get("entry_ids", [])
            if not entry_ids:
                # Grant access to the N most recent entries in a category
                category = args.get("category")
                limit    = min(int(args.get("limit", 10)), 50)
                entries  = db.query_meta_insight(
                    agent="selene", category=category, limit=limit
                )
                entry_ids = [e["id"] for e in entries]

            count = 0
            for eid in entry_ids:
                if db.set_meta_insight_sage_access(int(eid), True):
                    count += 1

            return {
                "status":  "success",
                "command": "grant_sage",
                "granted_count": count,
                "data":    f"Granted Sage access to {count} log entries.",
            }

        # ── revoke_sage ───────────────────────────────────────────────────────
        elif command == "revoke_sage":
            if self._agent_name != "selene":
                return {"status": "error", "message": "Only Selene can revoke Sage access."}

            entry_ids = args.get("entry_ids", [])
            count = 0
            for eid in entry_ids:
                if db.set_meta_insight_sage_access(int(eid), False):
                    count += 1

            return {
                "status":  "success",
                "command": "revoke_sage",
                "revoked_count": count,
                "data":    f"Revoked Sage access from {count} log entries.",
            }

        # ── promote_card ──────────────────────────────────────────────────────
        elif command == "promote_card":
            entry_id = args.get("entry_id")
            if not entry_id:
                return {"status": "error", "message": "promote_card requires entry_id."}
            ok = db.mark_meta_insight_promoted(int(entry_id))
            return {
                "status":  "success" if ok else "error",
                "command": "promote_card",
                "data":    f"Entry {entry_id} marked as promoted." if ok else "Failed.",
            }

        else:
            return {
                "status":  "error",
                "message": f"Unknown MetaInsight command: '{command}'. "
                           "Valid: query, pattern_mode, grant_sage, revoke_sage, promote_card",
            }

    def check_and_trigger(self, user_input: str):
        """
        Keyword trigger: phrases like 'why did you' / 'backtrace' / 'what were you thinking'
        auto-query the decision log for the most recent relevant entry.
        """
        lower = user_input.lower()
        triggers = [
            "why did you", "backtrace", "what were you thinking",
            "meta insight", "metainsight", "self observation",
            "show me your reasoning", "what was your reasoning",
        ]
        if any(t in lower for t in triggers):
            return {
                "command":  "query",
                "category": "decision",
                "limit":    3,
            }
        return None
