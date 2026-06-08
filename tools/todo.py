"""
tools/todo.py — Autonomous Step Tracker
────────────────────────────────────────
TodoTool — Selene's internal multi-step execution planner.

This is NOT a user-facing todo list. Selene owns it — she plans multi-step
tasks herself, advances through them as she works, and uses it as a
breadcrumb trail so she can resume cleanly after interruptions.

Persisted to memories/active_steps.json so it survives restarts.
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .schema import BaseTool, atomic_write

if TYPE_CHECKING:
    from selene_brain import LLMChat


class TodoTool(BaseTool):
    """
    Selene's autonomous execution step tracker.

    Workflow:
      1. Ghost asks for something complex (3+ steps).
      2. Selene calls plan() to lay out her approach before diving in.
      3. After completing each step she calls advance() to move to the next.
      4. Ghost can observe progress in the Tools panel; clear() resets when done.
    """
    name = "todo"
    description = (
        "Selene's autonomous task execution planner. Use this when tackling any request "
        "that requires 3+ distinct steps. Plan first, then advance through steps one by one.\n"
        "Commands:\n"
        "  plan    — {\"command\":\"plan\",\"task\":\"goal summary\",\"steps\":[\"step 1\",\"step 2\",...]}\n"
        "            Creates the plan. First step is automatically set in_progress.\n"
        "  advance — {\"command\":\"advance\"}\n"
        "            Marks the current in_progress step completed and starts the next.\n"
        "  status  — {\"command\":\"status\"} — compact progress summary for chat\n"
        "  update  — {\"command\":\"update\",\"id\":\"<id>\",\"status\":\"pending|in_progress|completed|skipped\"}\n"
        "            Manual override for a specific step.\n"
        "  clear   — {\"command\":\"clear\"} — wipe the plan when the task is done\n"
        "  list    — {\"command\":\"list\"} — full step list"
    )
    input_type  = "json"
    output_type = "any"

    VALID_STATUSES = {"pending", "in_progress", "completed", "skipped"}

    def __init__(self, agent_state: Any = None):
        self.agent_state = agent_state
        self._plan: Dict[str, Any] = {"task": "", "steps": []}
        self._steps_file = os.path.join(
            getattr(agent_state, "MEMORY_DIR", "memories") if agent_state else "memories",
            "active_steps.json"
        )
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self._steps_file, "r", encoding="utf-8") as f:
                self._plan = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._plan = {"task": "", "steps": []}

    def _save(self) -> None:
        atomic_write(self._steps_file, json.dumps(self._plan, indent=2, ensure_ascii=False))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_id(self) -> str:
        return f"S{uuid.uuid4().hex[:5].upper()}"

    def _current_step(self) -> Optional[Dict]:
        return next((s for s in self._plan.get("steps", []) if s["status"] == "in_progress"), None)

    def get_items(self) -> List[Dict]:
        """Direct access for UI / server handlers."""
        return self._plan.get("steps", [])

    def get_plan(self) -> Dict:
        return self._plan

    # ── Execute ───────────────────────────────────────────────────────────────

    def execute(self, input_data: Dict[str, Any]) -> Any:
        if not isinstance(input_data, dict):
            return {"error": "Expected JSON input."}

        command = input_data.get("command", "status")

        # ── plan ──────────────────────────────────────────────────────────────
        if command == "plan":
            task  = input_data.get("task", "").strip()
            steps = [s.strip() for s in input_data.get("steps", []) if str(s).strip()]
            if not steps:
                return {"error": "plan requires a non-empty 'steps' list."}
            built = []
            for i, desc in enumerate(steps):
                built.append({
                    "id":          self._make_id(),
                    "description": desc,
                    "status":      "in_progress" if i == 0 else "pending",
                })
            chain_id = str(uuid.uuid4())[:8]
            self._plan = {"task": task, "steps": built, "chain_id": chain_id}
            self._save()
            return {
                "planned":  task,
                "steps":    len(built),
                "current":  built[0]["description"],
                "chain_id": chain_id,
            }

        # ── advance ───────────────────────────────────────────────────────────
        elif command == "advance":
            steps = self._plan.get("steps", [])
            completed_desc = None
            next_desc      = None
            for i, s in enumerate(steps):
                if s["status"] == "in_progress":
                    s["status"]    = "completed"
                    completed_desc = s["description"]
                    for j in range(i + 1, len(steps)):
                        if steps[j]["status"] == "pending":
                            steps[j]["status"] = "in_progress"
                            next_desc = steps[j]["description"]
                            break
                    break
            self._plan["steps"] = steps
            self._save()
            all_done = all(s["status"] in ("completed", "skipped") for s in steps) if steps else False
            return {
                "completed": completed_desc,
                "next":      next_desc,
                "all_done":  all_done,
                "task":      self._plan.get("task", ""),
                "chain_id":  self._plan.get("chain_id", ""),
            }

        # ── status ────────────────────────────────────────────────────────────
        elif command == "status":
            steps = self._plan.get("steps", [])
            if not steps:
                return {"status": "No active plan."}
            done    = sum(1 for s in steps if s["status"] == "completed")
            total   = len(steps)
            current = self._current_step()
            return {
                "task":     self._plan.get("task", ""),
                "progress": f"{done}/{total}",
                "current":  current["description"] if current else "All steps complete.",
                "chain_id": self._plan.get("chain_id", ""),
            }

        # ── update ────────────────────────────────────────────────────────────
        elif command == "update":
            step_id    = input_data.get("id", "").upper()
            new_status = input_data.get("status", "").lower()
            if new_status not in self.VALID_STATUSES:
                return {"error": f"Invalid status. Valid: {self.VALID_STATUSES}"}
            for s in self._plan.get("steps", []):
                if s["id"] == step_id:
                    s["status"] = new_status
                    self._save()
                    return {"updated": step_id, "status": new_status}
            return {"error": f"Step '{step_id}' not found."}

        # ── clear ─────────────────────────────────────────────────────────────
        elif command == "clear":
            chain_id = self._plan.get("chain_id", "")
            task     = self._plan.get("task", "")
            steps    = self._plan.get("steps", [])
            self._plan = {"task": "", "steps": []}
            self._save()
            return {
                "cleared":         True,
                "task":            task,
                "chain_id":        chain_id,
                "steps_completed": sum(1 for s in steps if s["status"] == "completed"),
                "steps_total":     len(steps),
            }

        # ── list ──────────────────────────────────────────────────────────────
        elif command == "list":
            return {
                "task":     self._plan.get("task", ""),
                "steps":    self._plan.get("steps", []),
                "chain_id": self._plan.get("chain_id", ""),
            }

        return {"error": f"Unknown todo command: '{command}'."}
