"""
tools/manifest.py — Development Manifest Tool
───────────────────────────────────────────────
ManifestTool — manages the strategic project task manifest.

Owns:
  - Task CRUD (add, toggle, update, delete, reorder)
  - Manifest compilation to markdown (development_manifest.md, philosophy_manifest.md)
  - Obsidian vault sync
  - LLM-driven manifest reorganization
  - Prioritization guidelines
"""

import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from server.roster import agent_has_cap, agents_with_cap

from .schema import BaseTool

if TYPE_CHECKING:
    from pantheon_brain.agent_protocol import AgentState


class ManifestTool(BaseTool):
    """A tool for managing the Obsidian Journal & dynamic Task Manifest."""
    name = "manifest_manager"
    description = (
        "Manages the strategic project development manifest — tasks, ideas, priorities, subtasks, dependencies, and guidelines. "
        "Inputs must be JSON containing a 'command' key. Supported schemas:\n"
        "- View full manifest & tasks:      {\"command\": \"get_manifest\"}\n"
        "- Save idea from conversation:     {\"command\": \"save_idea\", \"text\": \"Casual description of the idea\"}\n"
        "- View all saved ideas:            {\"command\": \"get_ideas\"}\n"
        "- Promote idea to structured task: {\"command\": \"promote_idea\", \"idea_id\": \"I1\"}\n"
        "- Add task from conversational text: {\"command\": \"add_task_from_text\", \"text\": \"Describe task, priority, subtasks, etc.\"}\n"
        "- Structural add task:             {\"command\": \"add_task\", \"description\": \"Task summary\", \"priority\": \"B2\", \"dependencies\": [], \"subtasks\": []}\n"
        "- Toggle task completion:          {\"command\": \"toggle_task\", \"id\": \"T1\"}\n"
        "- Delete task:                     {\"command\": \"delete_task\", \"id\": \"T1\"}\n"
        "- Reorganize manifest via LLM:     {\"command\": \"reorganize\", \"prompt\": \"Optional prioritization focus\"}\n"
        "- Add priority focus guideline:    {\"command\": \"add_guideline_from_text\", \"text\": \"Guideline text\"}\n"
        "- Read another agent's manifest:   {\"command\": \"read_agent_manifest\", \"agent\": \"sage\"}"
    )
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        normalized = user_input.lower()

        _view_phrases = (
            "list tasks", "list the tasks", "show tasks", "show the tasks",
            "view the manifest", "show the manifest", "what's on the manifest",
            "whats on the manifest", "current priorities", "what are the priorities",
            "what's on the backlog", "whats on the backlog", "show backlog",
            "what tasks do we have", "what are my tasks",
        )
        if any(p in normalized for p in _view_phrases):
            return {"command": "get_manifest"}

        if "make it a priority to" in normalized or "make it a priority that" in normalized or "add prioritization guideline" in normalized:
            return {"command": "add_guideline_from_text", "text": user_input}

        if "add a task to" in normalized or "add task to" in normalized or "new task:" in normalized:
            return {"command": "add_task_from_text", "text": user_input}

        if "organize our roadmap" in normalized or "reorganize the manifest" in normalized or "reorder our tasks" in normalized or "prioritize our manifest" in normalized:
            return {"command": "reorganize", "prompt": user_input}

        _idea_phrases = (
            "jot that down", "save that idea", "note that down", "make a note of that",
            "add that to ideas", "save this idea", "log that idea", "capture that idea",
            "put that in the sketchpad", "sketch that out", "save to ideas",
            "add this to the sketchpad", "note that idea",
        )
        if any(p in normalized for p in _idea_phrases):
            return {"command": "save_idea", "text": user_input}

        return None

    # ── State I/O ─────────────────────────────────────────────────────────────

    def load_state_json(self) -> dict:
        # MEMORY_DIR is the agent folder — manifest_state.json lives directly inside it
        state_path = os.path.join(self.agent_state.MEMORY_DIR, "manifest_state.json")
        if not os.path.exists(state_path):
            return {"tasks": [], "philosophies": [], "ideas": []}
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {"tasks": [], "philosophies": [], "ideas": []}

    def save_state_json(self, state: dict) -> None:
        state_path = os.path.join(self.agent_state.MEMORY_DIR, "manifest_state.json")
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            self.compile_manifests()
        except Exception as e:
            print(f"[Manifest Manager Error]: Failed to save json state — {e}")

    def read_guidelines(self) -> str:
        active_agent = getattr(self.agent_state, "active_agent_name", "Selene").lower()
        guidelines_path = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_prioritization_guidelines.md")
        if not os.path.exists(guidelines_path) and agent_has_cap(active_agent, "dev_manifest"):
            legacy_path = os.path.join(self.agent_state.MEMORY_DIR, "prioritization_guidelines.md")
            if os.path.exists(legacy_path):
                guidelines_path = legacy_path
        return self.agent_state._read_file_safe(guidelines_path, "*(No guidelines specified)*")

    def read_build_context(self) -> str:
        active_agent = getattr(self.agent_state, "active_agent_name", "Selene").lower()
        context_path = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_build_context.md")
        if not os.path.exists(context_path) and agent_has_cap(active_agent, "dev_manifest"):
            legacy_path = os.path.join(self.agent_state.MEMORY_DIR, "build_context.md")
            if os.path.exists(legacy_path):
                context_path = legacy_path
        return self.agent_state._read_file_safe(context_path, "*(No build context on file)*")

    def update_guidelines(self, content: str) -> None:
        active_agent = getattr(self.agent_state, "active_agent_name", "Selene").lower()
        guidelines_path = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_prioritization_guidelines.md")
        try:
            with open(guidelines_path, 'w', encoding='utf-8') as f:
                f.write(content.strip() + "\n")
            self.sync_to_obsidian()
        except Exception as e:
            print(f"[Manifest Manager Error]: Failed to write guidelines — {e}")

    # ── Task helpers ──────────────────────────────────────────────────────────

    def generate_next_task_id(self, state: dict) -> str:
        max_num = 0
        for t in state.get("tasks", []):
            tid = t.get("id", "")
            match = re.match(r"^T(\d+)$", tid, re.IGNORECASE)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        return f"T{max_num + 1}"

    def add_task(self, title: str, description: str = "", category: str = "Feature",
                 priority: str = "B", dependencies: list | None = None,
                 subtasks: list | None = None) -> dict:
        state = self.load_state_json()
        new_id = self.generate_next_task_id(state)

        sub_list = []
        if subtasks:
            for idx, sub in enumerate(subtasks):
                if isinstance(sub, dict):
                    s_title  = sub.get("title") or sub.get("description") or ""
                    s_desc   = sub.get("description", "") if sub.get("title") else ""
                    s_status = sub.get("status", "pending")
                else:
                    s_title  = str(sub)
                    s_desc   = ""
                    s_status = "pending"
                sub_list.append({
                    "id":          f"{new_id}.{idx + 1}",
                    "title":       s_title.strip(),
                    "description": s_desc.strip(),
                    "status":      s_status,
                })

        new_task = {
            "id":           new_id,
            "title":        title.strip(),
            "description":  description.strip() if description else "",
            "category":     category.strip() if category else "Feature",
            "priority":     priority.strip().upper() if priority else "B",
            "status":       "pending",
            "dependencies": dependencies if dependencies else [],
            "subtasks":     sub_list,
        }
        state["tasks"].append(new_task)
        self.save_state_json(state)
        return new_task

    def toggle_task(self, task_id: str, status: Optional[str] = None) -> bool:
        state   = self.load_state_json()
        task_id = task_id.strip().upper()
        found   = False

        if "." in task_id:
            parent_id, _ = task_id.split(".", 1)
            for t in state.get("tasks", []):
                if t.get("id", "").upper() == parent_id:
                    for sub in t.get("subtasks", []):
                        if sub.get("id", "").upper() == task_id:
                            new_status = status if status else ("completed" if sub.get("status") != "completed" else "pending")
                            sub["status"] = new_status
                            found = True
                            if all(s.get("status") == "completed" for s in t.get("subtasks", [])):
                                t["status"] = "completed"
                            else:
                                t["status"] = "pending"
                            break
        else:
            for t in state.get("tasks", []):
                if t.get("id", "").upper() == task_id:
                    new_status = status if status else ("completed" if t.get("status") != "completed" else "pending")
                    t["status"] = new_status
                    for sub in t.get("subtasks", []):
                        sub["status"] = new_status
                    found = True
                    break

        if found:
            self.save_state_json(state)
        return found

    def update_task(self, task_id: str, description: str) -> bool:
        state   = self.load_state_json()
        task_id = task_id.strip().upper()
        for t in state.get("tasks", []):
            if t.get("id", "").upper() == task_id:
                t["description"] = description.strip()
                self.save_state_json(state)
                return True
            for sub in t.get("subtasks", []):
                if sub.get("id", "").upper() == task_id:
                    sub["description"] = description.strip()
                    self.save_state_json(state)
                    return True
        return False

    def update_task_full(self, task_id: str, title: str = "", description: str = "",
                         category: str = "Feature", priority: str = "B",
                         dependencies: list | None = None,
                         subtasks: list | None = None) -> bool:
        state   = self.load_state_json()
        task_id = task_id.strip().upper()
        for t in state.get("tasks", []):
            if t.get("id", "").upper() == task_id:
                if title:
                    t["title"]       = title.strip()
                t["description"]     = description.strip() if description else ""
                t["category"]        = category.strip() if category else "Feature"
                t["priority"]        = priority.strip().upper() if priority else "B"
                if dependencies is not None:
                    t["dependencies"] = dependencies

                new_subs       = []
                old_subs       = t.get("subtasks", [])
                sub_list_input = subtasks if subtasks is not None else []

                for idx, sub in enumerate(sub_list_input):
                    if isinstance(sub, dict):
                        s_id     = sub.get("id") or f"{task_id}.{idx + 1}"
                        s_title  = sub.get("title") or sub.get("description") or ""
                        s_desc   = sub.get("description", "") if sub.get("title") else ""
                        s_status = sub.get("status") or "pending"
                    else:
                        s_id     = f"{task_id}.{idx + 1}"
                        s_title  = str(sub)
                        s_desc   = ""
                        s_status = "pending"

                    matching_old = None
                    if isinstance(sub, dict) and sub.get("id"):
                        matching_old = next((o for o in old_subs if o.get("id") == sub.get("id")), None)
                    else:
                        matching_old = next(
                            (o for o in old_subs if (o.get("title") or o.get("description") or "").strip() == s_title.strip()),
                            None
                        )
                    if matching_old:
                        s_status = matching_old.get("status", "pending")

                    new_subs.append({
                        "id":          s_id,
                        "title":       s_title.strip(),
                        "description": s_desc.strip(),
                        "status":      s_status,
                    })

                t["subtasks"] = new_subs
                if new_subs and all(s.get("status") == "completed" for s in new_subs):
                    t["status"] = "completed"
                elif new_subs:
                    t["status"] = "pending"

                self.save_state_json(state)
                return True
        return False

    def delete_task(self, task_id: str) -> bool:
        state         = self.load_state_json()
        task_id       = task_id.strip().upper()
        initial_count = len(state["tasks"])
        state["tasks"] = [t for t in state["tasks"] if t.get("id", "").upper() != task_id]
        for t in state["tasks"]:
            t["dependencies"] = [dep for dep in t.get("dependencies", []) if dep.upper() != task_id]
        if len(state["tasks"]) < initial_count:
            self.save_state_json(state)
            return True
        return False

    def reorder_tasks(self, task_order: list) -> bool:
        state    = self.load_state_json()
        tasks    = state.get("tasks", [])
        task_map = {t.get("id", "").upper(): t for t in tasks}
        reordered = []
        for tid in task_order:
            t = task_map.get(tid.strip().upper())
            if t:
                reordered.append(t)
                del task_map[tid.strip().upper()]
        for t in task_map.values():
            reordered.append(t)
        state["tasks"] = reordered
        self.save_state_json(state)
        return True


    # ── Idea sketchpad helpers ────────────────────────────────────────────────

    def generate_next_idea_id(self, state: dict) -> str:
        max_num = 0
        for idea in state.get("ideas", []):
            iid = idea.get("id", "")
            match = re.match(r"^I(\d+)$", iid, re.IGNORECASE)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        return f"I{max_num + 1}"

    def route_idea_suggestion(self, distilled: dict) -> dict | None:
        """
        Sage-only: given a distilled idea dict {title, summary, tags}, ask the
        LLM whether this idea better fits another agent's domain.

        Reads agent domains from agents/*/config.json at the project root.
        Returns:
          {"agent": slug, "name": name, "domain": domain, "reason": str, "confidence": "high"|"low"}
          or None if the idea fits Sage best (no routing suggestion).
        """
        import glob as _glob

        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Load all agents that don't have idea_routing capability (they route, not receive)
        _idea_routers = {a["slug"] for a in agents_with_cap("idea_routing")}
        agents_info = []
        for cfg_path in _glob.glob(os.path.join(_root, "agents", "*", "config.json")):
            slug = os.path.basename(os.path.dirname(cfg_path))
            if slug in _idea_routers:
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as _f:
                    cfg = json.load(_f)
                agents_info.append({
                    "slug":   slug,
                    "name":   cfg.get("name", slug.capitalize()),
                    "domain": cfg.get("domain", ""),
                })
            except Exception:
                continue

        if not agents_info:
            return None

        roster = "\n".join(
            f"  {a['slug']} ({a['name']}): {a['domain']}"
            for a in agents_info
        )

        idea_text = f"{distilled.get('title', '')}\n{distilled.get('summary', '')}"
        tags      = ", ".join(distilled.get("tags", [])) or "none"

        prompt = (
            "You are Sage, the Selene OS orchestrator. Ghost gave you an idea to capture.\n\n"
            f"IDEA:\n{idea_text}\n\nTAGS: {tags}\n\n"
            "AGENT ROSTER (slug: domain):\n"
            f"{roster}\n\n"
            "Decide: does this idea clearly belong in another agent\'s domain?\n"
            "Rules:\n"
            "- Only suggest routing if you\'re confident the idea is a better fit elsewhere.\n"
            "- If it\'s borderline, ambiguous, or could fit multiple agents, return null.\n"
            "- Never suggest routing to yourself (sage).\n"
            "- Confidence: \'high\' = obvious domain match. \'low\' = plausible but not clear.\n\n"
            "Return ONLY raw JSON, no markdown:\n"
            "{\"agent\": \"slug\", \"reason\": \"one sentence why\", \"confidence\": \"high\"}"
            "\nor\n"
            "null"
        )

        try:
            raw = self.agent_state.llm_caller.call_llm(
                prompt, max_tokens=120, temperature=0.1
            ).strip()
            raw = __import__("re").sub(
                r'<think>[\s\S]*?</think>', '', raw,
                flags=__import__("re").IGNORECASE
            ).strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                raw = "\n".join(lines).strip()

            if raw.lower() in ("null", "none", ""):
                return None

            parsed = json.loads(raw)
            target_slug = parsed.get("agent", "").lower()

            # Find the agent entry
            match = next((a for a in agents_info if a["slug"] == target_slug), None)
            if not match:
                return None

            return {
                "agent":      target_slug,
                "name":       match["name"],
                "domain":     match["domain"],
                "reason":     parsed.get("reason", ""),
                "confidence": parsed.get("confidence", "low"),
            }
        except Exception as e:
            print(f"[ManifestTool] route_idea_suggestion failed: {e}")
            return None

    def save_idea(self, text: str) -> dict:
        """
        LLM-distills a casual idea from conversation text and saves it to ideas[].
        When Sage is the active agent, runs a routing pass first — if the idea
        clearly belongs to another agent's domain, returns a routing-pending dict
        instead of saving immediately.

        Returns either:
          idea dict (saved)       — normal save
          {"routing_pending": True, "distilled": {...}, "suggestion": {...}}
                                  — Sage routing suggestion, not yet saved
        """
        agent_name = getattr(self.agent_state, "active_agent_name", "Agent")
        agent_slug = getattr(self.agent_state, "active_agent_slug", "unknown")
        prompt = (
            f"You are {agent_name}. Ghost just shared something he wants captured as an idea.\n"
            "Extract and distill it into a clear, concise idea entry. Do NOT plan or structure it as a task yet — "
            "just capture the essence of what was said.\n\n"
            f"INPUT:\n{text}\n\n"
            "Return a strict JSON object (no markdown) with these fields:\n"
            "{\n"
            '  "title": "Short idea headline (5-10 words)",\n'
            '  "summary": "One to three sentences distilling the core idea",\n'
            '  "tags": ["optional", "context", "tags"]\n'
            "}"
        )
        try:
            raw = self.agent_state.llm_caller.call_llm(prompt, max_tokens=512, temperature=0.3).strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                raw = "\n".join(lines).strip()
            data = json.loads(raw)
        except Exception as e:
            print(f"[ManifestTool] idea LLM parse failed: {e} — saving raw text")
            data = {"title": text[:80].strip(), "summary": text.strip(), "tags": []}

        # Routing intercept — only for agents with idea_routing capability
        if agent_has_cap(agent_slug, "idea_routing"):
            suggestion = self.route_idea_suggestion(data)
            if suggestion and suggestion.get("confidence") == "high":
                return {
                    "routing_pending": True,
                    "distilled":       data,
                    "suggestion":      suggestion,
                    "original_text":   text,
                }

        state  = self.load_state_json()
        new_id = self.generate_next_idea_id(state)
        idea   = {
            "id":        new_id,
            "title":     data.get("title", "").strip(),
            "summary":   data.get("summary", "").strip(),
            "tags":      data.get("tags", []),
            "agent":     agent_slug,
            "status":    "sketch",          # sketch | promoted
            "task_id":   None,              # set when promoted to a task
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        state.setdefault("ideas", []).append(idea)
        self.save_state_json(state)
        return idea

    def save_idea_to_agent(self, target_slug: str, distilled: dict, original_text: str) -> dict:
        """
        Write a distilled idea directly to another agent's manifest_state.json.
        Used by Sage after routing confirmation. Does NOT require Sage gate —
        the gate was already passed at the confirmation step.
        """
        _root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target_path = os.path.join(_root, "agents", target_slug, "manifest_state.json")

        existing: dict = {"tasks": [], "ideas": []}
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as _f:
                    existing = json.load(_f)
            except Exception:
                pass

        existing.setdefault("ideas", [])

        # Generate ID in target's namespace
        max_n = 0
        for idea in existing["ideas"]:
            _id = idea.get("id", "")
            if _id.startswith("I") and _id[1:].isdigit():
                max_n = max(max_n, int(_id[1:]))
        new_id = f"I{max_n + 1}"

        new_idea = {
            "id":         new_id,
            "title":      distilled.get("title", "").strip(),
            "summary":    distilled.get("summary", "").strip(),
            "tags":       distilled.get("tags", []),
            "agent":      target_slug,
            "routed_by":  agent_slug,
            "status":     "sketch",
            "task_id":    None,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        existing["ideas"].append(new_idea)

        try:
            with open(target_path, "w", encoding="utf-8") as _f:
                json.dump(existing, _f, indent=2, ensure_ascii=False)
            return new_idea
        except Exception as e:
            return {"error": str(e)}

    def promote_idea(self, idea_id: str) -> dict:
        """Converts an idea into a structured task and marks it as promoted."""
        state   = self.load_state_json()
        idea_id = idea_id.strip().upper()
        idea    = next((i for i in state.get("ideas", []) if i.get("id", "").upper() == idea_id), None)
        if not idea:
            return {"ok": False, "error": f"Idea {idea_id} not found."}
        if idea.get("status") == "promoted":
            return {"ok": False, "error": f"Idea {idea_id} already promoted to task {idea.get('task_id')}."}

        # Build a task from the idea using add_task_from_text path
        text   = f"{idea['title']}\n{idea['summary']}"
        prompt = (
            "Convert this rough idea into a structured manifest task.\n\n"
            f"IDEA:\n{text}\n\n"
            "Return strict JSON (no markdown):\n"
            "{\n"
            '  "title": "Concise task title",\n'
            '  "description": "Actionable task description",\n'
            '  "category": "Feature/Bug/Idea",\n'
            '  "priority": "B",\n'
            '  "subtasks": []\n'
            "}"
        )
        try:
            raw = self.agent_state.llm_caller.call_llm(prompt, max_tokens=512, temperature=0.2).strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                raw = "\n".join(lines).strip()
            data = json.loads(raw)
        except Exception:
            data = {"title": idea["title"], "description": idea["summary"], "category": "Idea", "priority": "C", "subtasks": []}

        task = self.add_task(
            title=data.get("title", idea["title"]),
            description=data.get("description", idea["summary"]),
            category=data.get("category", "Idea"),
            priority=data.get("priority", "C"),
            subtasks=data.get("subtasks", []),
        )
        # Mark idea as promoted (load fresh state since add_task saved it)
        state = self.load_state_json()
        for i in state.get("ideas", []):
            if i.get("id", "").upper() == idea_id:
                i["status"]  = "promoted"
                i["task_id"] = task["id"]
                break
        self.save_state_json(state)
        return {"ok": True, "idea_id": idea_id, "task": task}

    # ── Compilation & sync ────────────────────────────────────────────────────

    def compile_manifests(self) -> None:
        state_path         = os.path.join(self.agent_state.MEMORY_DIR, "manifest_state.json")
        dev_manifest_path  = os.path.join(self.agent_state.MEMORY_DIR, "development_manifest.md")
        phil_manifest_path = os.path.join(self.agent_state.MEMORY_DIR, "philosophy_manifest.md")

        if not os.path.exists(state_path):
            return

        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception as e:
            print(f"[Manifest Compiler Error]: Failed to read json state — {e}")
            return

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        lines   = [
            "# Selene OS — Development Task Manifest",
            f"*Auto-updated on: {now_str}*",
            "",
            "## Active Development Tasks (Ordered by Priority)",
        ]

        tasks           = state.get("tasks", [])
        pending_tasks   = [t for t in tasks if t.get("status") != "completed"]
        completed_tasks = [t for t in tasks if t.get("status") == "completed"]

        def get_priority_key(t):
            p = t.get("priority", "Z").strip().upper()
            return p if p else "Z"

        pending_tasks.sort(key=get_priority_key)

        if not pending_tasks:
            lines.append("*(No active tasks at the moment)*")
        else:
            for t in pending_tasks:
                tid   = t.get("id", "T?")
                title = t.get("title") or t.get("description", "")
                desc  = t.get("description", "") if t.get("title") else ""
                prio  = t.get("priority", "B").strip().upper()
                cat   = t.get("category", "Feature")
                deps  = t.get("dependencies", [])
                dep_str = f" (Blocked by: {', '.join(deps)})" if deps else ""
                lines.append(f"- `[ ]` **[{prio}]** [{cat}] {title} `({tid})`{dep_str}")
                if desc:
                    lines.append("  > " + desc.replace('\n', '\n  > '))
                for sub in t.get("subtasks", []):
                    sub_tid   = sub.get("id", "")
                    sub_title = sub.get("title") or sub.get("description", "")
                    sub_desc  = sub.get("description", "") if sub.get("title") else ""
                    sub_check = "x" if sub.get("status") == "completed" else " "
                    lines.append(f"  - `[{sub_check}]` {sub_title} `({sub_tid})`")
                    if sub_desc:
                        lines.append("    > " + sub_desc.replace('\n', '\n    > '))

        lines.extend(["", "## Completed Tasks"])
        if not completed_tasks:
            lines.append("*(No completed tasks yet)*")
        else:
            for t in completed_tasks:
                tid   = t.get("id", "T?")
                title = t.get("title") or t.get("description", "")
                lines.append(f"- `[x]` {title} `({tid})`")
                for sub in t.get("subtasks", []):
                    sub_tid   = sub.get("id", "")
                    sub_title = sub.get("title") or sub.get("description", "")
                    lines.append(f"  - `[x]` {sub_title} `({sub_tid})`")

        # Ideas sketchpad section
        ideas = state.get("ideas", [])
        sketch_ideas = [i for i in ideas if i.get("status") != "promoted"]
        lines.extend(["", "## Ideas Sketchpad"])
        if not sketch_ideas:
            lines.append("*(No ideas captured yet)*")
        else:
            for idea in sketch_ideas:
                iid     = idea.get("id", "I?")
                ititle  = idea.get("title", "")
                isumm   = idea.get("summary", "")
                iagent  = idea.get("agent", "")
                itags   = ", ".join(idea.get("tags", []))
                idate   = idea.get("created_at", "")
                lines.append(f"- **[{iid}]** {ititle}  *(from {iagent}, {idate})*")
                if isumm:
                    lines.append(f"  > {isumm}")
                if itags:
                    lines.append(f"  > Tags: {itags}")

        try:
            with open(dev_manifest_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            print(f"[Manifest Compiler Error]: Failed to write development manifest — {e}")

        p_lines = [
            "# Selene OS — Philosophical & Architectural Insights",
            f"*Auto-updated on: {now_str}*",
            "",
            "## Architectural Principles & Insights",
        ]
        philosophies = state.get("philosophies", [])
        if not philosophies:
            p_lines.append("*(No philosophical insights recorded yet)*")
        else:
            for p in philosophies:
                p_lines.append(f"* {p}")

        try:
            with open(phil_manifest_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(p_lines) + "\n")
        except Exception as e:
            print(f"[Manifest Compiler Error]: Failed to write philosophy manifest — {e}")

        self.sync_to_obsidian()

    def sync_to_obsidian(self) -> None:
        obsidian_vault = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
        if not obsidian_vault:
            return
        active_agent = getattr(self.agent_state, "active_agent_name", "Selene").lower()
        try:
            os.makedirs(obsidian_vault, exist_ok=True)
            for fname in ["development_manifest.md", "philosophy_manifest.md", "prioritization_guidelines.md"]:
                src = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_{fname}")
                dst = os.path.join(obsidian_vault, f"{active_agent}_{fname}")
                if os.path.exists(src):
                    shutil.copy2(src, dst)
            print(f"[Manifest Obsidian Sync]: Synced manifests to vault -> {obsidian_vault}")
        except Exception as e:
            print(f"[Manifest Obsidian Sync Error]: Failed to copy to vault — {e}")

    def reorganize_manifest_via_llm(self, user_instruction: str = "") -> str:
        state    = self.load_state_json()
        guidelines   = self.read_guidelines()
        tasks    = state.get("tasks", [])

        if not tasks:
            return "There are no active tasks to prioritize, Ghost."

        structural_view = []
        for i, t in enumerate(tasks, 1):
            entry = {
                "n":            i,
                "id":           t.get("id"),
                "title":        t.get("title") or t.get("description", ""),
                "description":  t.get("description", "") if t.get("title") else "",
                "category":     t.get("category", "Feature"),
                "priority":     t.get("priority", "B").strip()[:1].upper(),
                "status":       t.get("status"),
                "dependencies": t.get("dependencies", []),
                "subtasks": [
                    {
                        "id":          s.get("id"),
                        "title":       s.get("title") or s.get("description", ""),
                        "description": s.get("description", "") if s.get("title") else "",
                        "status":      s.get("status", "pending"),
                    }
                    for s in t.get("subtasks", [])
                ],
            }
            structural_view.append(entry)

        total_count  = len(tasks)
        original_ids = {t.get("id") for t in tasks}
        tasks_json   = json.dumps(structural_view, indent=2)
        build_context = self.read_build_context()

        prompt = (
            "You are reorganizing Ghost's Selene OS development manifest.\n\n"
            f"PROJECT CONTEXT (read this first — it explains what Selene OS is, the architecture, and why certain tasks matter more than others):\n{build_context}\n\n"
            f"PRIORITIZATION GUIDELINES:\n{guidelines}\n\n"
            "DIRECTIVE:\n"
            + (user_instruction if user_instruction else
               "Perform a full alignment scan. Reorder, reprioritize, and update dependencies so the list is logically coherent.")
            + f"\n\nCURRENT MANIFEST ({total_count} top-level tasks):\n{tasks_json}\n\n"
            "RULES:\n"
            f"1. Return ALL {total_count} top-level tasks — no omissions, no additions. Same IDs.\n"
            "2. You may reorder them freely. The returned array order becomes the new order.\n"
            "3. Assign priority codes for every task:\n"
            "   A = Bug / Critical (critical blockers or immediate work)\n"
            "   B = Feature / Medium (implementation work currently being developed)\n"
            "   C = Idea / Low (future thoughts, polish, non-blocking research)\n"
            "4. Update dependencies: a task must not outrank its own blockers.\n"
            "5. You may restructure subtasks — move them between parents, add new ones, reorder them.\n"
            "   All subtask IDs must remain accounted for (no orphaned IDs).\n"
            "6. Preserve 'title', 'description', 'category', and 'status' of every task and subtask unless restructuring subtasks.\n"
            "7. Return ONLY a raw JSON object. No markdown, no codeblocks, no extra text.\n\n"
            "OUTPUT FORMAT:\n"
            "{\n"
            '  "tasks": [\n'
            '    { "id": "T1", "priority": "A", "dependencies": [], "subtasks": [ {"id": "T1.1", "title": "...", "description": "...", "status": "pending"} ] },\n'
            '    ...\n'
            "  ],\n"
            '  "explanation": "Warm conversational briefing — what changed and why."\n'
            "}"
        )

        try:
            raw_response = self.agent_state.llm_caller.call_llm(
                input_data=prompt,
                system_prompt=(
                    "You are a JSON-only manifest reorganizer. "
                    "Return only the raw JSON object with keys 'tasks' and 'explanation'. "
                    "No markdown, no codeblocks, no preamble."
                ),
                history=[],
                temperature=0.2,
                max_tokens=4096,
            )

            clean = raw_response.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                clean = "\n".join(lines).strip()

            result      = json.loads(clean)
            new_tasks   = result.get("tasks", [])
            explanation = result.get("explanation", "Manifest reorganized.")

            returned_ids = {t.get("id") for t in new_tasks}
            missing = original_ids - returned_ids
            extra   = returned_ids - original_ids
            if missing:
                return (
                    f"I generated a reorganization but it was missing {len(missing)} task(s): "
                    f"{', '.join(sorted(missing))}. Nothing was saved — please try again."
                )
            if extra:
                new_tasks = [t for t in new_tasks if t.get("id") in original_ids]

            original_by_id = {t.get("id"): t for t in tasks}
            merged_tasks   = []
            for nt in new_tasks:
                tid  = nt.get("id")
                orig = original_by_id[tid].copy()
                orig["priority"]     = str(nt.get("priority", orig.get("priority", "B"))).strip()[:1].upper()
                if orig["priority"] not in ["A", "B", "C"]:
                    orig["priority"] = "B"
                orig["dependencies"] = nt.get("dependencies", orig.get("dependencies", []))
                if "subtasks" in nt and isinstance(nt["subtasks"], list):
                    all_orig_subs = {s.get("id"): s for s in orig.get("subtasks", [])}
                    rebuilt_subs  = []
                    seen_sub_ids  = set()
                    for ns in nt["subtasks"]:
                        sid = ns.get("id", "")
                        if sid in all_orig_subs:
                            rebuilt_subs.append(all_orig_subs[sid])
                            seen_sub_ids.add(sid)
                        else:
                            rebuilt_subs.append({
                                "id":          sid,
                                "title":       ns.get("title") or ns.get("description") or "",
                                "description": ns.get("description", "") if ns.get("title") else "",
                                "status":      ns.get("status", "pending"),
                            })
                            seen_sub_ids.add(sid)
                    for sid, sub in all_orig_subs.items():
                        if sid not in seen_sub_ids:
                            rebuilt_subs.append(sub)
                    orig["subtasks"] = rebuilt_subs
                merged_tasks.append(orig)

            state["tasks"] = merged_tasks
            self.save_state_json(state)
            return explanation

        except Exception as e:
            print(f"[Manifest LLM Reorganizer Error]: {e}")
            return (
                f"I tried to reorganize the manifest, Ghost, but hit an error parsing "
                f"the model response: `{type(e).__name__}: {e}`"
            )


    # ── Cross-agent manifest write (Sage-only) ──────────────────────────────────

    def write_agent_manifest(self, target_slug: str, tasks: list, ideas: list | None = None) -> dict:
        """
        Overwrite another agent's manifest_state.json with the supplied task list.
        Sage-gated: raises PermissionError if the active agent is not sage.
        Preserves existing ideas unless explicitly passed.
        """
        caller = getattr(self.agent_state, "active_agent_slug", "unknown").lower()
        if not agent_has_cap(caller, "write_agent_manifest"):
            raise PermissionError(
                f"write_agent_manifest requires 'write_agent_manifest' capability. Current agent: {caller}"
            )
        _root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target_path = os.path.join(_root, "agents", target_slug, "manifest_state.json")

        # Load existing state so we can preserve ideas / guidelines if not supplied
        existing: dict = {"tasks": [], "ideas": []}
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as _f:
                    existing = json.load(_f)
            except Exception:
                pass

        new_state = {
            "tasks": tasks,
            "ideas": ideas if ideas is not None else existing.get("ideas", []),
        }
        try:
            with open(target_path, "w", encoding="utf-8") as _f:
                json.dump(new_state, _f, indent=2, ensure_ascii=False)
            return {"ok": True, "agent": target_slug, "task_count": len(tasks)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reorganize_agent_manifest(self, target_slug: str, user_instruction: str = "") -> str:
        """
        Load another agent's manifest, run the LLM reorganizer, write back.
        Sage-gated.
        """
        caller = getattr(self.agent_state, "active_agent_slug", "unknown").lower()
        if not agent_has_cap(caller, "write_agent_manifest"):
            return f"reorganize_agent_manifest requires 'write_agent_manifest' capability. Current agent: {caller}"

        _root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target_path = os.path.join(_root, "agents", target_slug, "manifest_state.json")
        if not os.path.exists(target_path):
            return f"No manifest found for agent '{target_slug}'."

        try:
            with open(target_path, "r", encoding="utf-8") as _f:
                state = json.load(_f)
        except Exception as e:
            return f"Failed to load manifest for '{target_slug}': {e}"

        guidelines = self.read_guidelines()
        tasks      = state.get("tasks", [])
        if not tasks:
            return f"{target_slug.capitalize()}'s manifest has no tasks to reorganize."

        structural_view = []
        for i, t in enumerate(tasks, 1):
            entry = {
                "n":            i,
                "id":           t.get("id"),
                "title":        t.get("title") or t.get("description", ""),
                "description":  t.get("description", "") if t.get("title") else "",
                "category":     t.get("category", "Feature"),
                "priority":     t.get("priority", "B").strip()[:1].upper(),
                "status":       t.get("status"),
                "dependencies": t.get("dependencies", []),
                "subtasks": [
                    {
                        "id":          s.get("id"),
                        "title":       s.get("title") or s.get("description", ""),
                        "description": s.get("description", "") if s.get("title") else "",
                        "status":      s.get("status", "pending"),
                    }
                    for s in t.get("subtasks", [])
                ],
            }
            structural_view.append(entry)

        total_count  = len(tasks)
        original_ids = {t.get("id") for t in tasks}
        tasks_json   = json.dumps(structural_view, indent=2)
        build_context = self.read_build_context()

        prompt = (
            f"You are Sage, reorganizing {target_slug.capitalize()}'s manifest on behalf of Ghost's Selene OS project.\n\n"
            f"PROJECT CONTEXT:\n{build_context}\n\n"
            f"PRIORITIZATION GUIDELINES:\n{guidelines}\n\n"
            "DIRECTIVE:\n"
            + (user_instruction if user_instruction else
               f"Perform a full alignment scan of {target_slug.capitalize()}'s task list. Reorder, reprioritize, and update dependencies so the list is logically coherent.")
            + f"\n\nCURRENT MANIFEST ({total_count} top-level tasks):\n{tasks_json}\n\n"
            "RULES:\n"
            f"1. Return ALL {total_count} top-level tasks — no omissions, no additions. Same IDs.\n"
            "2. You may reorder them freely. The returned array order becomes the new order.\n"
            "3. Assign priority codes: A = Bug/Critical, B = Feature/Medium, C = Idea/Low.\n"
            "4. Update dependencies: a task must not outrank its own blockers.\n"
            "5. You may restructure subtasks. All subtask IDs must remain accounted for.\n"
            "6. Preserve 'title', 'description', 'category', 'status' unless restructuring.\n"
            "7. Return ONLY a raw JSON object. No markdown, no codeblocks, no extra text.\n\n"
            "OUTPUT FORMAT:\n"
            "{\n"
            "  \"tasks\": [\n"
            "    { \"id\": \"T1\", \"priority\": \"A\", \"dependencies\": [], \"subtasks\": [] },\n"
            "    ...\n"
            "  ],\n"
            "  \"explanation\": \"What changed and why.\"\n"
            "}"
        )

        try:
            raw_response = self.agent_state.llm_caller.call_llm(
                input_data=prompt,
                system_prompt=(
                    "You are a JSON-only manifest reorganizer. "
                    "Return only the raw JSON object with keys 'tasks' and 'explanation'. "
                    "No markdown, no codeblocks, no preamble."
                ),
                history=[],
                temperature=0.2,
                max_tokens=4096,
            )

            clean = raw_response.strip()
            if clean.startswith("```"):
                lines = clean.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                clean = "\n".join(lines).strip()

            result      = json.loads(clean)
            new_tasks   = result.get("tasks", [])
            explanation = result.get("explanation", "Manifest reorganized.")

            returned_ids = {t.get("id") for t in new_tasks}
            missing = original_ids - returned_ids
            extra   = returned_ids - original_ids
            if missing:
                return (
                    f"Reorganization of {target_slug}'s manifest was missing "
                    f"{len(missing)} task(s): {', '.join(sorted(missing))}. Nothing was saved."
                )
            if extra:
                new_tasks = [t for t in new_tasks if t.get("id") in original_ids]

            original_by_id = {t.get("id"): t for t in tasks}
            merged_tasks   = []
            for nt in new_tasks:
                tid  = nt.get("id")
                orig = original_by_id[tid].copy()
                orig["priority"]     = str(nt.get("priority", orig.get("priority", "B"))).strip()[:1].upper()
                if orig["priority"] not in ["A", "B", "C"]:
                    orig["priority"] = "B"
                orig["dependencies"] = nt.get("dependencies", orig.get("dependencies", []))
                if "subtasks" in nt and isinstance(nt["subtasks"], list):
                    all_orig_subs = {s.get("id"): s for s in orig.get("subtasks", [])}
                    rebuilt_subs  = []
                    seen_sub_ids  = set()
                    for ns in nt["subtasks"]:
                        sid = ns.get("id", "")
                        if sid in all_orig_subs:
                            rebuilt_subs.append(all_orig_subs[sid])
                            seen_sub_ids.add(sid)
                        else:
                            rebuilt_subs.append({
                                "id":          sid,
                                "title":       ns.get("title") or ns.get("description") or "",
                                "description": ns.get("description", "") if ns.get("title") else "",
                                "status":      ns.get("status", "pending"),
                            })
                            seen_sub_ids.add(sid)
                    for sid, sub in all_orig_subs.items():
                        if sid not in seen_sub_ids:
                            rebuilt_subs.append(sub)
                    orig["subtasks"] = rebuilt_subs
                merged_tasks.append(orig)

            # Write back to target agent's manifest
            write_result = self.write_agent_manifest(
                target_slug, merged_tasks, ideas=state.get("ideas", [])
            )
            if not write_result.get("ok"):
                return f"Reorganization completed but failed to write: {write_result.get('error')}"

            return f"[{target_slug.capitalize()}] {explanation}"

        except Exception as e:
            print(f"[Manifest Cross-Agent Reorganizer Error]: {e}")
            return (
                f"I tried to reorganize {target_slug}'s manifest but hit an error: "
                f"`{type(e).__name__}: {e}`"
            )

    # ── Execute (command router) ──────────────────────────────────────────────

    def execute(self, input_data: Dict[str, Any]) -> Any:
        # Robust input normalisation — handles Hermes-style schema deviations
        if not input_data:
            input_data = {}
        elif isinstance(input_data, str):
            input_data = {"command": "add_task_from_text", "text": input_data}

        if "add" in input_data:
            add_val = input_data["add"]
            if isinstance(add_val, dict):
                desc = add_val.get("task") or add_val.get("description")
                if desc:
                    prio   = add_val.get("priority", "B2")
                    p_str  = str(prio).strip().upper()
                    if p_str not in ["A1", "A2", "B1", "B2", "C1", "C2"]:
                        if "HIGH" in p_str or "CRITICAL" in p_str:
                            prio = "A1"
                        elif "LOW" in p_str or "MINOR" in p_str:
                            prio = "C1"
                        else:
                            prio = "B2"
                    input_data = {
                        "command":      "add_task",
                        "description":  desc,
                        "priority":     prio,
                        "dependencies": add_val.get("dependencies", []),
                        "subtasks":     add_val.get("subtasks", []),
                    }
            elif isinstance(add_val, str):
                input_data = {"command": "add_task_from_text", "text": add_val}

        elif input_data.get("action") == "task" or "name" in input_data:
            desc = input_data.get("name") or input_data.get("description")
            if desc:
                prio  = input_data.get("priority", "B2")
                p_str = str(prio).strip().upper()
                if p_str not in ["A1", "A2", "B1", "B2", "C1", "C2"]:
                    if "HIGH" in p_str or "CRITICAL" in p_str:
                        prio = "A1"
                    elif "LOW" in p_str or "MINOR" in p_str:
                        prio = "C1"
                    else:
                        prio = "B2"
                input_data = {
                    "command":      "add_task",
                    "description":  desc,
                    "priority":     prio,
                    "dependencies": input_data.get("dependencies", []),
                    "subtasks":     input_data.get("subtasks", []),
                }

        command = input_data.get("command")

        if command == "add_task":
            title = input_data.get("title", "")
            desc  = input_data.get("description", "")
            cat   = input_data.get("category", "Feature")
            prio  = input_data.get("priority", "B")
            deps  = input_data.get("dependencies", [])
            subs  = input_data.get("subtasks", [])
            if not title and desc:
                title = desc
                desc  = ""
            if not title:
                return "Failed to add task: 'title' or 'description' is required."
            task = self.add_task(title=title, description=desc, category=cat,
                                 priority=prio, dependencies=deps, subtasks=subs)
            return f"Added task '{title}' successfully under ID {task['id']}."

        elif command == "toggle_task":
            tid    = input_data.get("id", "")
            status = input_data.get("status")
            if not tid:
                return "Failed to toggle task: 'id' is required."
            ok = self.toggle_task(tid, status)
            return f"Task {tid} toggled successfully." if ok else f"Task {tid} not found."

        elif command == "update_task":
            tid  = input_data.get("id", "")
            desc = input_data.get("description", "")
            if not tid or not desc:
                return "update_task requires 'id' and 'description'."
            ok = self.update_task(tid, desc)
            return f"Task {tid} updated." if ok else f"Task {tid} not found."

        elif command == "update_task_full":
            tid  = input_data.get("id", "")
            if not tid:
                return "update_task_full requires 'id'."
            ok = self.update_task_full(
                task_id=tid,
                title=input_data.get("title", ""),
                description=input_data.get("description", ""),
                category=input_data.get("category", "Feature"),
                priority=input_data.get("priority", "B"),
                dependencies=input_data.get("dependencies", []),
                subtasks=input_data.get("subtasks", []),
            )
            return f"Task {tid} updated." if ok else f"Task {tid} not found."

        elif command == "delete_task":
            tid = input_data.get("id", "")
            if not tid:
                return "Failed to delete task: 'id' is required."
            ok = self.delete_task(tid)
            return f"Task {tid} deleted successfully." if ok else f"Task {tid} not found."

        elif command == "reorder_tasks":
            order = input_data.get("task_order", [])
            if not order:
                return "Failed to reorder tasks: 'task_order' list is required."
            self.reorder_tasks(order)
            return "Tasks manually reordered successfully."

        elif command == "add_guideline_from_text":
            text   = input_data.get("text", "")
            prompt = (
                "The user wants to add a new prioritization guideline or rule. "
                "Extract the core prioritization guideline as a clear, concise bullet point directive.\n\n"
                f"USER INPUT:\n{text}\n\n"
                "Return only the raw bullet point directive text (e.g., '- Always focus on UI bugs before writing new APIs.'). No commentary."
            )
            rule    = self.agent_state.llm_caller.call_llm(prompt).strip()
            current = self.read_guidelines()
            if not current.endswith("\n"):
                current += "\n"
            self.update_guidelines(current + rule + "\n")
            summary = self.reorganize_manifest_via_llm(f"Re-organizing because of new rule: {rule}")
            return f"Added new guideline: '{rule}'.\n\n{summary}"

        elif command == "add_task_from_text":
            text   = input_data.get("text", "")
            prompt = (
                "The user wants to add a new task. Extract the core task title, description (if long/detailed), "
                "priority estimate (A/B/C, where A=Bug/Critical, B=Feature/Medium, C=Idea/Low), "
                "and any subtasks or dependencies mentioned.\n\n"
                f"USER INPUT:\n{text}\n\n"
                "Return a strict JSON block (no markdown codeblocks) matching this format:\n"
                "{\n"
                "  \"title\": \"Brief task title...\",\n"
                "  \"description\": \"Detailed instructions or context...\",\n"
                "  \"category\": \"Bug/Feature/Idea\",\n"
                "  \"priority\": \"B\",\n"
                "  \"dependencies\": [],\n"
                "  \"subtasks\": []\n"
                "}"
            )
            raw = self.agent_state.llm_caller.call_llm(prompt).strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw = "\n".join(lines).strip()

            data        = json.loads(raw)
            title       = data.get("title", "")
            desc        = data.get("description", "")
            cat         = data.get("category", "Feature")
            prio        = data.get("priority", "B")
            if not title and data.get("description"):
                title = data.get("description")
                desc  = ""
            prio_letter = prio.strip()[:1].upper() if prio else "B"
            if prio_letter not in ["A", "B", "C"]:
                prio_letter = "B"
            task = self.add_task(
                title=title, description=desc, category=cat,
                priority=prio_letter,
                dependencies=data.get("dependencies", []),
                subtasks=data.get("subtasks", []),
            )
            return f"Successfully added task: **{title}** ({task['id']})."

        elif command == "reorganize":
            return self.reorganize_manifest_via_llm(input_data.get("prompt", ""))

        elif command == "save_idea":
            text = input_data.get("text", "").strip()
            if not text:
                return "save_idea requires 'text'."
            result = self.save_idea(text)
            if result.get("routing_pending"):
                # Return sentinel so the handler can set pending_idea_routing
                return result
            return f"Idea captured as {result['id']}: **{result['title']}**"

        elif command == "save_idea_to_agent":
            target    = input_data.get("agent", "").strip().lower()
            distilled = input_data.get("distilled", {})
            orig_text = input_data.get("original_text", "")
            if not target or not distilled:
                return "save_idea_to_agent requires 'agent' and 'distilled'."
            idea = self.save_idea_to_agent(target, distilled, orig_text)
            if idea.get("error"):
                return f"Failed to route idea to {target}: {idea['error']}"
            return f"Idea routed to {target.capitalize()} as {idea['id']}: **{idea['title']}**"

        elif command == "get_ideas":
            state = self.load_state_json()
            ideas = state.get("ideas", [])
            if not ideas:
                return "No ideas in the sketchpad yet."
            lines = []
            for idea in ideas:
                status_str = " [PROMOTED]" if idea.get("status") == "promoted" else ""
                lines.append(f"[{idea['id']}]{status_str} {idea['title']} — {idea['summary'][:120]}")
            return "\n".join(lines)

        elif command == "promote_idea":
            idea_id = input_data.get("idea_id", "").strip()
            if not idea_id:
                return "promote_idea requires 'idea_id'."
            result = self.promote_idea(idea_id)
            if result.get("ok"):
                task = result["task"]
                return f"Idea {idea_id} promoted to task {task['id']}: **{task['title']}**"
            return f"promote_idea failed: {result.get('error')}"

        elif command == "read_agent_manifest":
            target = input_data.get("agent", "").strip().lower()
            if not target:
                return "read_agent_manifest requires 'agent' slug."
            _root       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            target_path = os.path.join(_root, "agents", target, "manifest_state.json")
            if not os.path.exists(target_path):
                return f"No manifest found for agent '{target}'."
            try:
                with open(target_path, "r", encoding="utf-8") as _f:
                    data = json.load(_f)
                tasks = data.get("tasks", [])
                ideas = data.get("ideas", [])
                lines = [f"## {target.capitalize()} Manifest"]
                lines.append(f"Tasks: {len(tasks)} | Ideas: {len(ideas)}")
                lines.append("")
                if tasks:
                    lines.append("### Tasks")
                    for t in tasks:
                        lines.append(f"- [{t.get('id')}] [{t.get('priority','B')}] {t.get('title') or t.get('description','')}")
                if ideas:
                    sketch = [i for i in ideas if i.get("status") != "promoted"]
                    if sketch:
                        lines.append("")
                        lines.append("### Ideas Sketchpad")
                        for i in sketch:
                            lines.append(f"- [{i['id']}] {i['title']} — {i['summary'][:100]}")
                return "\n".join(lines)
            except Exception as e:
                return f"Failed to read manifest for '{target}': {e}"

        elif command == "write_agent_manifest":
            target = input_data.get("agent", "").strip().lower()
            if not target:
                return "write_agent_manifest requires 'agent' slug."
            tasks  = input_data.get("tasks", [])
            ideas  = input_data.get("ideas", None)
            try:
                result = self.write_agent_manifest(target, tasks, ideas)
                if result.get("ok"):
                    return f"Wrote {result['task_count']} tasks to {target}'s manifest."
                return f"write_agent_manifest failed: {result.get('error')}"
            except PermissionError as _pe:
                return str(_pe)

        elif command == "reorganize_agent_manifest":
            target = input_data.get("agent", "").strip().lower()
            if not target:
                return "reorganize_agent_manifest requires 'agent' slug."
            prompt = input_data.get("prompt", "")
            return self.reorganize_agent_manifest(target, prompt)

        elif command == "get_manifest":
            active_agent = getattr(self.agent_state, "active_agent_name", "Selene").lower()
            state        = self.load_state_json()
            guidelines   = self.read_guidelines()

            dev_manifest_path = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_development_manifest.md")
            if not os.path.exists(dev_manifest_path) and agent_has_cap(active_agent, "dev_manifest"):
                legacy = os.path.join(self.agent_state.MEMORY_DIR, "development_manifest.md")
                if os.path.exists(legacy):
                    dev_manifest_path = legacy
            dev_manifest = self.agent_state._read_file_safe(dev_manifest_path)

            phil_manifest_path = os.path.join(self.agent_state.MEMORY_DIR, f"{active_agent}_philosophy_manifest.md")
            if not os.path.exists(phil_manifest_path) and agent_has_cap(active_agent, "dev_manifest"):
                legacy = os.path.join(self.agent_state.MEMORY_DIR, "philosophy_manifest.md")
                if os.path.exists(legacy):
                    phil_manifest_path = legacy
            phil_manifest = self.agent_state._read_file_safe(phil_manifest_path)

            return {
                "state":                state,
                "guidelines":           guidelines,
                "development_manifest": dev_manifest,
                "philosophy_manifest":  phil_manifest,
            }

        return f"Unknown command for manifest_manager: {command}"
