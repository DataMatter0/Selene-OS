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

    # ── Binary YES/NO gate ────────────────────────────────────────────────────

    def ask_model_binary(self, tool_name: str, user_input: str) -> Tuple[bool, float]:
        """
        Ask the model: should I use this tool right now?
        Returns (should_use: bool, raw_entropy: float).
        """
        tool_desc = ""
        try:
            t = self.selene.tool_router.tools.get(tool_name)
            if t:
                tool_desc = t.description.split("\n")[0]  # first line only
        except Exception:
            pass

        prompt = (
            f"/no_think\n"
            f"Ghost said: \"{user_input[:200]}\"\n"
            f"Tool available: {tool_name} — {tool_desc}\n\n"
            f"Should I use the {tool_name} tool right now? Reply with only YES or NO."
        )
        try:
            raw = self.selene.llm_caller.call_llm(
                input_data=prompt,
                system_prompt="Reply with exactly one word: YES or NO.",
                history=[],
                temperature=0.0,
                max_tokens=5,
            )
            raw = re.sub(r'<think>[\s\S]*?</think>', '', str(raw), flags=re.IGNORECASE).strip()
            entropy = self.selene.llm_caller.last_entropy or 1.0
            answered_yes = bool(re.search(r'\byes\b', raw, re.IGNORECASE))
            return (answered_yes, entropy)
        except Exception as e:
            print(f"[ToolSuggestion]: Binary gate failed — {e}")
            return (False, 1.0)

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

        # 2. Phrase match — run through binary gate
        match = self.find_phrase_match(user_input)
        if match:
            tool_name, matched_phrase = match
            should_use, entropy = self.ask_model_binary(tool_name, user_input)

            is_confident = entropy < HIGH_CONFIDENCE_ENTROPY

            if is_confident and should_use:
                # High confidence YES — execute
                self.selene.db.record_phrase_outcome(tool_name, matched_phrase, True)
                args = {"query": user_input, "url": user_input}
                # Merge default args for this tool
                for cmd, (tname, base) in SLASH_COMMANDS.items():
                    if tname == tool_name:
                        args = {**base, **args}
                        break
                return {"decision": "execute", "tool_name": tool_name,
                        "args": args, "trigger": "confident",
                        "matched_phrase": matched_phrase}

            elif is_confident and not should_use:
                # High confidence NO — pass through
                self.selene.db.record_phrase_outcome(tool_name, matched_phrase, False)
                return {"decision": "pass"}

            else:
                # Low confidence — inject warning, set pending confirmation
                args = {"query": user_input, "url": user_input}
                for cmd, (tname, base) in SLASH_COMMANDS.items():
                    if tname == tool_name:
                        args = {**base, **args}
                        break
                self.set_pending(tool_name, args, user_input, matched_phrase)
                warning = self.build_suggestion_warning(tool_name)
                return {"decision": "suggest", "tool_name": tool_name,
                        "warning": warning, "matched_phrase": matched_phrase}

        return {"decision": "pass"}
