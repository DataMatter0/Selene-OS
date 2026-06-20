# selene_brain/tool_suggestion.py
"""
ToolSuggestionLayer
───────────────────
Sits between the user message and tool execution. Replaces the blunt
keyword-trigger system with a two-tier gate:

  Tier 1 — Slash commands: /youtube, /maps, /notion etc.
            Deterministic. Bypasses all gates. Executes immediately.

  Tier 2 — Phrase matching: phrases stored per-tool in SQLite.
            When matched, asks the model "should I use this tool?" (binary).
            Confidence score gates the outcome:

              High confidence YES  → execute tool
              Low confidence       → inject warning into Selene's prompt,
                                     she asks Ghost to clarify naturally.
                                     Next message caught as YES/NO reply.
              High confidence NO   → pass to normal chat

  Pending confirmation state: stored on the LLMChat instance as
  `pending_tool_confirmation`. The WS handler checks this before the
  presence layer so Ghost's YES/NO is caught before it goes anywhere else.
"""

import re
import threading
from typing import Any, Dict, Optional, Tuple


# ── Default slash commands per tool ──────────────────────────────────────────
# Format: "/command" → (tool_name, args_dict)
SLASH_COMMANDS: Dict[str, Tuple[str, Dict]] = {
    "/youtube":   ("youtube",          {"command": "summarise"}),
    "/yt":        ("youtube",          {"command": "summarise"}),
    "/maps":      ("maps",             {"command": "search"}),
    "/map":       ("maps",             {"command": "search"}),
    "/notion":    ("notion",           {"command": "search"}),
    "/memory":    ("memory_tool",      {"command": "get_summary"}),
    "/mem":       ("memory_tool",      {"command": "get_summary"}),
    "/status":    ("status_checker",   {"command": "full_status"}),
    "/manifest":  ("manifest_manager", {"command": "get_manifest"}),
    "/rune":      ("runereader",       {"command": "read"}),
    "/knowledge": ("knowledge_manager",{"command": "list"}),
    "/kb":        ("knowledge_manager",{"command": "list"}),
    "/meta":      ("meta_insight",     {"command": "query", "category": "decision", "limit": 5}),
}

# ── Default phrase seeds per tool ─────────────────────────────────────────────
# These are loaded into SQLite on first boot if the table is empty for a tool.
DEFAULT_PHRASES: Dict[str, list] = {
    "youtube": [
        "watching this video", "check out this video", "this clip",
        "youtube link", "youtu.be", "youtube.com",
    ],
    "maps": [
        "how do i get to", "directions to", "where is", "find nearby",
        "navigate to", "distance to",
    ],
    "notion": [
        "add to notion", "save to notion", "log this in notion",
        "update notion", "notion page",
    ],
    "knowledge_manager": [
        "add to board", "save to board", "put this on the board",
        "knowledge board", "tabletop",
    ],
    "runereader": [
        "read this file", "analyze this document", "parse this",
        "read the file", "look at this doc",
    ],
    "meta_insight": [
        "why did you", "what were you thinking", "backtrace",
        "show me your reasoning", "what was your reasoning",
    ],
    "schedule_manager": [
        "set a timer", "remind me in", "set an alarm", "schedule this",
    ],
}

# Confidence threshold — raw entropy below this = high confidence
HIGH_CONFIDENCE_ENTROPY = 0.35


class ToolSuggestionLayer:
    """
    Stateless helper — attach one instance to the LLMChat/server.
    All state (pending confirmation) lives on the selene instance itself.
    """

    def __init__(self, selene_ref: Any):
        self.selene = selene_ref
        self._seed_default_phrases()

    # ── Phrase seeding ────────────────────────────────────────────────────────

    def _seed_default_phrases(self):
        """Load default phrases into SQLite for any tool that has none yet."""
        try:
            db = self.selene.db
            for tool_name, phrases in DEFAULT_PHRASES.items():
                existing = db.get_tool_phrases(tool_name)
                if not existing:
                    for phrase in phrases:
                        db.add_tool_phrase(tool_name, phrase)
        except Exception as e:
            print(f"[ToolSuggestion]: Phrase seeding failed — {e}")

    # ── Slash command detection ───────────────────────────────────────────────

    def check_slash_command(self, user_input: str) -> Optional[Tuple[str, Dict, str]]:
        """
        Returns (tool_name, args, rest_of_message) if a slash command is found,
        else None. rest_of_message is the text after the command (may be empty).
        """
        stripped = user_input.strip()
        lower    = stripped.lower()

        for cmd, (tool_name, base_args) in SLASH_COMMANDS.items():
            if lower == cmd or lower.startswith(cmd + " "):
                rest = stripped[len(cmd):].strip()
                args = dict(base_args)
                # Inject remaining text as query/url if present
                if rest:
                    if "url" in args or tool_name in ("youtube", "maps", "runereader"):
                        args["url"] = rest
                        args["query"] = rest
                    else:
                        args["query"] = rest
                return (tool_name, args, rest)

        return None

    # ── Phrase matching ───────────────────────────────────────────────────────

    def find_phrase_match(self, user_input: str) -> Optional[Tuple[str, str]]:
        """
        Scan user_input against all stored phrases.
        Returns (tool_name, matched_phrase) or None.
        Only matches tools that are in selene's allowed_tools list.
        """
        allowed = getattr(self.selene, "allowed_tools", None)
        lower   = user_input.lower()
        try:
            phrases = self.selene.db.get_tool_phrases()
            for row in phrases:
                tool = row["tool_name"]
                if allowed is not None and tool not in allowed:
                    continue
                if row["phrase"] in lower:
                    return (tool, row["phrase"])
        except Exception:
            pass
        return None

    # ── Structured tool intent classifier ───────────────────────────────────────

    def ask_model_binary(self, tool_name: str, user_input: str) -> Tuple[bool, float]:
        """
        Legacy shim — calls ask_model_tool_decision scoped to one tool.
        Returns (should_use, entropy) for backward compatibility.
        """
        result = self.ask_model_tool_decision(user_input, tool_hint=tool_name)
        matched = result.get("tool") == tool_name
        intent  = result.get("intent", "indirect")
        # Map: direct match → high-confidence yes; indirect → low confidence; no match → no
        if matched and intent == "direct":
            return (True, 0.1)
        elif matched and intent == "indirect":
            return (True, 0.5)
        return (False, 1.0)

    def ask_model_tool_decision(
        self,
        user_input: str,
        tool_hint: str = "",
    ) -> Dict:
        """
        Structured tool intent classification.

        Surfaces the full active tool roster to the model alongside Ghost's
        message and asks it to decide:
          - which tool (if any) is warranted
          - whether the request is DIRECT (explicit ask → auto-execute)
            or INDIRECT (agent thinks it's useful → ask first)

        Returns a dict:
          {"tool": "manifest_manager", "intent": "direct",   "reason": "..."}
          {"tool": "manifest_manager", "intent": "indirect", "reason": "..."}
          {"tool": None}
        """
        import json as _json

        # Build compact tool roster — only allowed, non-dormant tools
        allowed  = getattr(self.selene, "allowed_tools", None)
        tool_lines: list = []
        try:
            skip_set = {"memory_tool", "chronicle_manager", "status_checker"}  # presence/keyword-only
            for name, t in self.selene.tool_router.tools.items():
                if getattr(t, "dormant", False):
                    continue
                if name in skip_set:
                    continue
                if allowed is not None and name not in allowed:
                    continue
                # Full description, not just first line
                desc = getattr(t, "description", "").strip()
                tool_lines.append(f"  {name}: {desc}")
        except Exception:
            pass

        # If a hint was given (from phrase match), surface it first
        if tool_hint and not any(tool_hint in l for l in tool_lines):
            try:
                t = self.selene.tool_router.tools.get(tool_hint)
                if t:
                    tool_lines.insert(0, f"  {tool_hint}: {t.description.strip()}")
            except Exception:
                pass

        tools_block = "\n".join(tool_lines) if tool_lines else "  (no tools available)"

        hint_note = (
            f"\n\nPhrase match hint: the message matched a trigger phrase for tool '{tool_hint}'. "
            f"Consider this tool first, but any tool from the roster is valid."
        ) if tool_hint else ""

        prompt = (
            "/no_think\n"
            f"Ghost's message: \"{user_input[:300]}\"\n\n"
            f"Available tools:\n{tools_block}"
            f"{hint_note}\n\n"
            "Decide:\n"
            "1. Is a tool call warranted for this message? If not, return: {\"tool\": null}\n"
            "2. Which tool? Use the exact tool name from the list above.\n"
            "3. Is this DIRECT (Ghost explicitly asked for a tool action or its output) "
            "or INDIRECT (no explicit ask but a tool would genuinely help)?\n\n"
            "Return ONLY a raw JSON object, no markdown, no explanation outside JSON:\n"
            "{\"tool\": \"tool_name_or_null\", \"intent\": \"direct_or_indirect\", \"reason\": \"one sentence\"}\n"
            "or\n"
            "{\"tool\": null}"
        )

        try:
            raw = self.selene.llm_caller.call_llm(
                input_data=prompt,
                system_prompt=(
                    "You are a tool routing classifier. Return ONLY a raw JSON object. "
                    "No markdown, no codeblocks, no extra text."
                ),
                history=[],
                temperature=0.0,
                max_tokens=80,
            )
            raw = re.sub(r'<think>[\s\S]*?</think>', '', str(raw), flags=re.DOTALL | re.IGNORECASE).strip()
            # Strip markdown fences if model ignores instruction
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = lines[1:] if lines[0].startswith("```") else lines
                lines = lines[:-1] if lines and lines[-1].startswith("```") else lines
                raw = "\n".join(lines).strip()

            parsed = _json.loads(raw)
            tool   = parsed.get("tool") or None
            intent = parsed.get("intent", "indirect").lower()
            reason = parsed.get("reason", "")

            # Validate tool is in the roster
            if tool and tool not in (self.selene.tool_router.tools or {}):
                tool = None

            return {"tool": tool, "intent": intent, "reason": reason}

        except Exception as e:
            print(f"[ToolSuggestion]: Tool decision failed — {e}")
            return {"tool": None}

    # ── Confidence warning injection ──────────────────────────────────────────

    def build_suggestion_warning(self, tool_name: str) -> str:
        """
        Returns a context block to prepend to Selene's prompt when confidence
        is low. She will include uncertainty and ask Ghost to clarify.
        """
        return (
            f"<tool_suggestion>\n"
            f"I noticed this message might be asking me to use the {tool_name} tool, "
            f"but I'm not certain that's what's needed right now. "
            f"Acknowledge your uncertainty naturally in your reply and ask Ghost whether "
            f"he'd like you to use it — don't execute the tool yet.\n"
            f"</tool_suggestion>\n\n"
        )

    # ── Pending confirmation check ────────────────────────────────────────────

    def check_pending_confirmation(self, user_input: str) -> Optional[Dict]:
        """
        If a pending_tool_confirmation exists on the selene instance, check
        whether Ghost's message is a YES or NO reply.

        Returns:
          {"action": "execute", "tool_name": ..., "args": ..., "context": ...}
          {"action": "cancel"}
          None — no pending confirmation
        """
        pending = getattr(self.selene, "pending_tool_confirmation", None)
        if not pending:
            return None

        lower = user_input.lower().strip()

        # Affirmative patterns
        _YES = {"yes", "yeah", "yep", "sure", "go ahead", "do it",
                "affirmative", "correct", "yup", "ok", "okay", "y"}

        # Negative patterns
        _NO  = {"no", "nope", "nah", "don't", "dont", "skip",
                "never mind", "nevermind", "cancel", "stop", "n"}

        is_only_yes = lower in _YES
        starts_affirmative = any(lower.startswith(y) for y in _YES) or lower in _YES
        starts_negative    = any(lower.startswith(n) for n in _NO)  or lower in _NO

        # Clear pending state regardless of outcome
        tool_name    = pending["tool_name"]
        base_args    = pending["args"]
        orig_context = pending["original_input"]

        if starts_affirmative:
            # Record hit for the matched phrase
            if pending.get("matched_phrase"):
                try:
                    self.selene.db.record_phrase_outcome(tool_name, pending["matched_phrase"], True)
                except Exception:
                    pass

            # Build enriched args — if Ghost said more than just "yes", use full message as context
            args = dict(base_args)
            if not is_only_yes:
                # Ghost added context/instructions — use full reply as query
                args["query"]   = user_input
                args["context"] = user_input
            else:
                args["query"]   = orig_context
                args["context"] = orig_context

            self._clear_pending()
            return {"action": "execute", "tool_name": tool_name, "args": args, "context": user_input}

        elif starts_negative:
            if pending.get("matched_phrase"):
                try:
                    self.selene.db.record_phrase_outcome(tool_name, pending["matched_phrase"], False)
                except Exception:
                    pass
            self._clear_pending()
            return {"action": "cancel"}

        # Ambiguous — keep pending, treat as normal message
        return None

    def set_pending(self, tool_name: str, args: Dict,
                    original_input: str, matched_phrase: str = ""):
        """Store a pending tool confirmation on the selene instance."""
        self.selene.pending_tool_confirmation = {
            "tool_name":      tool_name,
            "args":           args,
            "original_input": original_input,
            "matched_phrase": matched_phrase,
        }

    def _clear_pending(self):
        self.selene.pending_tool_confirmation = None

    # ── Main entry point ──────────────────────────────────────────────────────

    def process(self, user_input: str) -> Dict:
        """
        Main routing method. Returns a decision dict:

          {"decision": "execute",   "tool_name": ..., "args": ..., "trigger": "slash"|"confident"}
          {"decision": "suggest",   "tool_name": ..., "warning": ..., "matched_phrase": ...}
          {"decision": "pass"}  — no tool action, proceed to normal chat
        """
        # 1. Slash command — deterministic, no gate
        slash = self.check_slash_command(user_input)
        if slash:
            tool_name, args, _ = slash
            # Inject any remaining text as url/query
            return {"decision": "execute", "tool_name": tool_name,
                    "args": args, "trigger": "slash"}

        # 2. Structured tool decision — surfaces full tool context to the model.
        #    Phrase match is used as a hint (fast pre-filter), not the gate itself.
        phrase_match = self.find_phrase_match(user_input)
        hint = phrase_match[0] if phrase_match else ""
        matched_phrase = phrase_match[1] if phrase_match else ""

        decision = self.ask_model_tool_decision(user_input, tool_hint=hint)
        tool_name = decision.get("tool")
        intent    = decision.get("intent", "indirect")

        if not tool_name:
            return {"decision": "pass"}

        # Build base args for this tool
        args = {"query": user_input, "url": user_input}
        for cmd, (tname, base) in SLASH_COMMANDS.items():
            if tname == tool_name:
                args = {**base, **args}
                break

        if intent == "direct":
            # Ghost explicitly asked → execute immediately
            if matched_phrase:
                try:
                    self.selene.db.record_phrase_outcome(tool_name, matched_phrase, True)
                except Exception:
                    pass
            return {"decision": "execute", "tool_name": tool_name,
                    "args": args, "trigger": "intent_direct",
                    "matched_phrase": matched_phrase}
        else:
            # Agent thinks tool is useful but not explicitly asked → ask first
            if matched_phrase:
                try:
                    self.selene.db.record_phrase_outcome(tool_name, matched_phrase, False)
                except Exception:
                    pass
            self.set_pending(tool_name, args, user_input, matched_phrase)
            warning = self.build_suggestion_warning(tool_name)
            return {"decision": "suggest", "tool_name": tool_name,
                    "warning": warning, "matched_phrase": matched_phrase}
