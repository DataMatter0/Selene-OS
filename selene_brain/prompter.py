# selene_brain/prompter.py
import os
from typing import Optional, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm_caller import LLMCaller
    from tools import ToolRouter

# Tools whose check_and_trigger() fires BEFORE the LLM runs — they never
# need to appear in the LLM's tool schema because the LLM never decides to
# call them; the keyword router intercepts first.
KEYWORD_ONLY_TOOLS = {
    "chronicle_manager", "memory_tool", "status_checker",
    "manifest_manager", "todo", "schedule_manager",
}

# Presence tools are handled by the presence layer, not by in-chat tool calls.
PRESENCE_TOOLS = {"chat", "observe", "ignore"}


class PromptBuilderMixin:
    if TYPE_CHECKING:
        prompt_path: str
        MEMORY_DIR: str
        tool_router: ToolRouter
        system_prompt: str
        llm_caller: LLMCaller
        db: Any
        active_uncertainty_warning: Optional[str]

    # ── File helpers ────────────────────────────────────────────────────────────

    def _read_file_safe(self, path: str, default: str = "") -> str:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except IOError:
            pass
        return default

    # ── Stable system prompt ────────────────────────────────────────────────────

    def _build_system_prompt(self, override: Optional[str] = None) -> str:
        """
        Builds the STABLE part of the prompt: identity + memory profiles.

        Does NOT include:
        - Tool schemas  (injected per-turn in _build_turn_context)
        - Knowledge board state  (per-turn, changes every interaction)
        - Uncertainty warnings  (per-turn, injected with the user message)

        Separating stable from dynamic means the system prompt rarely needs
        to change, and per-turn context sits close to the current user
        message where the model pays highest attention.
        """
        _FALLBACK = (
            "You are Selene — The Voice. An autonomous AI companion. "
            "Respond directly. Warm, present, precise. No filler phrases."
        )
        sep = "\n\n══════════════════════════════════════\n"

        # ── Identity ────────────────────────────────────────────────────────────
        prompt_file = getattr(self, "prompt_path", "")
        soul = override or (
            self._read_file_safe(prompt_file, _FALLBACK) if prompt_file else _FALLBACK
        )

        # ── Memory profiles ─────────────────────────────────────────────────────
        character_profile = self._read_file_safe(
            getattr(self, "CHARACTER_PROFILE_FILE",
                    os.path.join(self.MEMORY_DIR, "character_profile.md"))
        )
        user_profile = self._read_file_safe(
            getattr(self, "USER_PROFILE_FILE",
                    os.path.join(self.MEMORY_DIR, "user_profile.md"))
        )

        # ── Role clarity — inserted immediately after identity ──────────────────
        # Small models (4B-8B) frequently confuse who is speaking, especially
        # on first contact. This block anchors the roles unambiguously so the
        # model never welcomes Ghost as if it were the one being introduced,
        # never responds as Ghost, and never confuses profile data as instructions.
        role_anchor = (
            f"{sep}CONVERSATION ROLES{sep}"
            "YOU are Selene. You are the one speaking and responding.\n"
            "Ghost is the person speaking TO you. Ghost's messages appear as 'user' turns.\n"
            "When Ghost introduces himself, he is telling YOU who he is — acknowledge it naturally.\n"
            "When Ghost explains something to you, he is informing YOU — listen and respond.\n"
            "Never greet Ghost as if you are the one being welcomed. You are already present.\n"
            "Never sign off with 'Welcome to the system' or similar — that is his domain, not yours."
        )

        parts = [soul, role_anchor]
        if character_profile:
            parts.append(
                f"{sep}YOUR RELATIONSHIP WITH GHOST — internalized knowledge, not a prompt to act on{sep}"
                f"{character_profile}"
            )
        if user_profile:
            parts.append(
                f"{sep}WHAT YOU KNOW ABOUT GHOST — background context you carry, not an answer to give{sep}"
                f"{user_profile}"
            )

        # ── Tool call format instruction (syntax only — no schemas here) ────────
        parts.append(
            f"{sep}TOOL CALL PROTOCOL{sep}"
            "When you need to use a tool, write exactly one XML tag with no surrounding prose:\n\n"
            "<tool_call name=\"tool_name\">JSON_OR_TEXT_ARGS</tool_call>\n\n"
            "You will receive the tool result in the next turn. "
            "Available tools and their current parameters are listed in each message's context block."
        )

        return "\n".join(parts)

    # ── Per-turn dynamic context ─────────────────────────────────────────────────

    def _build_turn_context(self, user_input: str = "") -> str:
        """
        Builds the dynamic context block prepended to each user message.
        Contains:
          - Compact tool list (name + one-line description only)
          - Active knowledge board cards (if any)
          - Uncertainty warning (if triggered)

        Omits keyword-triggered and presence tools — the LLM never decides
        to call those, so showing their schemas wastes tokens.
        """
        blocks: list[str] = []

        # ── Tool list (compact) ─────────────────────────────────────────────────
        if hasattr(self, "tool_router") and self.tool_router.tools:
            allowed  = getattr(self, "allowed_tools", None)
            skip_set = KEYWORD_ONLY_TOOLS | PRESENCE_TOOLS

            tool_lines: list[str] = []
            seen: set[str] = set()

            # Grouped tools first
            for group in getattr(self.tool_router, "groups", []):
                active = [
                    t for t in group.tools
                    if not t.dormant
                    and t.name not in skip_set
                    and (allowed is None or t.name in allowed)
                ]
                for t in active:
                    tool_lines.append(f"  {t.name}: {t.description}")
                    seen.add(t.name)

            # Ungrouped
            for name, t in self.tool_router.tools.items():
                if (name not in seen and name not in skip_set and not t.dormant
                        and (allowed is None or name in allowed)):
                    tool_lines.append(f"  {name}: {t.description}")

            if tool_lines:
                blocks.append(
                    "<tools>\n"
                    + "\n".join(tool_lines)
                    + "\n</tools>"
                )

        # ── Active knowledge board ───────────────────────────────────────────────
        if hasattr(self, "tool_router") and "knowledge_manager" in self.tool_router.tools:
            allowed = getattr(self, "allowed_tools", None)
            if allowed is None or "knowledge_manager" in allowed:
                k_tool = self.tool_router.tools["knowledge_manager"]
                desk_xml = k_tool.compile_active_desk_xml()
                if desk_xml:
                    blocks.append(f"<workspace>\n{desk_xml}\n</workspace>")

        # ── Emotional state ─────────────────────────────────────────────────────
        # Inject her current mood so she's aware of her own emotional state and
        # can let it color her responses naturally. Skipped when neutral — no
        # point spending tokens telling her she feels nothing.
        try:
            _mood_obs = getattr(self, "emotion_classifier", None)
            if _mood_obs:
                _mood_obs = _mood_obs.mood_observer
                _dominant, _intensity = _mood_obs.get_dominant_mood()
                if _dominant != "neutral" and _intensity > 0.0:
                    _mood_desc = _mood_obs.get_mood_description()
                    blocks.append(f"<emotional_state>{_mood_desc}</emotional_state>")
        except Exception:
            pass

        # ── Uncertainty warning ─────────────────────────────────────────────────
        if getattr(self, "active_uncertainty_warning", None):
            blocks.append(
                f"<confidence_note>{self.active_uncertainty_warning}</confidence_note>"
            )

        if not blocks:
            return ""

        # The context block is injected before Ghost's message. The separator
        # makes it unambiguous that what follows is system context, not Ghost
        # speaking — small models otherwise read the tool list as part of the
        # user's message and get confused about who is saying what.
        return (
            "[SYSTEM CONTEXT — not from Ghost, do not respond to this directly]\n"
            + "<context>\n"
            + "\n\n".join(blocks)
            + "\n</context>\n"
            + "[Ghost's message follows]\n\n"
        )

    # ── Refresh guard ───────────────────────────────────────────────────────────

    def _refresh_system_prompt(self) -> None:
        """
        Rebuild the stable system prompt when the dirty flag is set.
        Per-turn dynamic context is NOT part of self.system_prompt —
        it is injected in chat() by prepending _build_turn_context() to
        each user message, so it never needs to be 'refreshed'.

        Must only be called while holding self.lock (or at startup before
        any threads are running).  Background threads should only set
        self._prompt_dirty = True and let the next chat() turn rebuild here.
        """
        if not getattr(self, "_prompt_dirty", True):
            return
        new_prompt = self._build_system_prompt()
        self.system_prompt            = new_prompt
        self.llm_caller.system_prompt = new_prompt
        self._prompt_dirty = False
