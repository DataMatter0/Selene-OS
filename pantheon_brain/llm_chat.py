from .llm_caller import LLMCaller
from .lm_studio_manager import LMStudioManager
import time
import textwrap
from pynput import keyboard
from tools import ToolRouter, register_all_tools
from typing import Optional
import threading
import re
import random
import os
import json
import uuid
from dotenv import load_dotenv
from server.roster import default_agent_slug as _roster_default_slug

from . import PromptBuilderMixin, ConversationManagerMixin, MemoryExtractorMixin

load_dotenv()

# Get the directory of the current script's parent (the project root) to make file paths absolute
_BRAIN_DIR         = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_DIR        = os.path.dirname(_BRAIN_DIR) # project root
_STATE_FILE        = os.path.join(_SCRIPT_DIR, "pantheon_state.json")
_CONVERSATIONS_DIR = os.path.join(_SCRIPT_DIR, "conversations")
_AGENTS_DIR         = os.path.join(_SCRIPT_DIR, "agents")
_SOUL_FILE          = os.path.join(_SCRIPT_DIR, "configs", "soul.md")  # legacy, unused by agents

class LLMChat(PromptBuilderMixin, ConversationManagerMixin, MemoryExtractorMixin):
    AUTONOMY_THRESHOLD_SECONDS      = 120   # Time of user inactivity before she starts writing
    TYPING_PROMPT_THRESHOLD_SECONDS = 180   # Time of user typing before she prompts
    CLI_WRAP_WIDTH                  = 80    # Desired width for text wrapping in the CLI
    EXTRACTION_CONTEXT_TURNS        = 5     # How many recent turns to pass to the extractor
    COMPACTION_THRESHOLD            = 14    # Messages in working_memory before compacting
    COMPACTION_KEEP_RECENT          = 6     # Keep last N messages verbatim; summarize the rest
    MEMORY_SECTION_LIMIT            = 350   # Max words per memory section (soft cap for prompt)
    TRIAGE_MAX_TOKENS               = 30    # Triage response is just a short JSON array
    MIN_TRIAGE_CHARS                = 60    # Skip triage for trivially short turns
    # Ghost profile density threshold: when user_profile exceeds this word count,
    # trigger a second-stage character extraction into character_profile.md
    GHOST_PROFILE_EXTRACTION_THRESHOLD = 200
    # Character profile word cap: when character_profile exceeds this, run compression
    CHARACTER_PROFILE_CAP           = 400
    STATE_FILE          = _STATE_FILE
    CONVERSATIONS_DIR   = _CONVERSATIONS_DIR
    SOUL_FILE           = _SOUL_FILE
    AGENTS_DIR          = _AGENTS_DIR

    def __init__(self, base_url: str, model_name: str = "", system_prompt: Optional[str] = None, memory_window: int = 5):
        print("Selene, Loading...")

        self.lock           = threading.RLock()   # re-entrant lock — prevents deadlocks
        self.working_memory = []
        self.memory_window  = memory_window
        self.current_input_buffer  = []

        # --- Conversation persistence ---
        os.makedirs(self.CONVERSATIONS_DIR, exist_ok=True)
        self.active_conversation_id:   Optional[str] = None
        self.active_conversation_name: str           = "New Conversation"

        # --- Autonomy & Motivation State ---
        self.creative_energy = 100 # A resource that fuels her writing
        self.is_running = False
        self.is_writing_autonomously = False
        self.last_interaction_time = time.time()
        
        # --- Thought Stream Callback ---
        from typing import Callable
        self.thought_callback: Optional[Callable[[str, str, str], None]] = None

        # Dynamic attributes set by startup after construction
        self.tool_suggestion:           Optional[object] = None
        self.pending_tool_confirmation: Optional[object] = None

        # Pass base_url without suffix — LLMCaller appends /v1/chat/completions itself.
        self.llm_caller = LLMCaller(base_url=base_url, model_name=model_name, system_prompt="")
        self.tool_router = ToolRouter(llm_caller=self.llm_caller)

        register_all_tools(self, self.tool_router)

        # Setup Agent via hot-swapping the roster default on boot
        self._prompt_dirty = True
        self.swap_agent(_roster_default_slug())

        self.load_state()

    def swap_agent(self, agent_name: str) -> None:
        """
        Hot-swaps the active agent's configuration, memory database, system prompt, and allowed tools
        without reloading LM Studio.  All paths are resolved from agents/{slug}/config.json —
        no agent names are hardcoded in this logic.
        """
        with self.lock:
            slug = agent_name.lower().strip()

            agent_dir   = os.path.join(_AGENTS_DIR, slug)
            config_file = os.path.join(agent_dir, "config.json")
            if not os.path.exists(config_file):
                raise FileNotFoundError(
                    f"[LLMChat]: No agent folder found at agents/{slug}/config.json"
                )

            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # ── Identity ──────────────────────────────────────────────────────
            self.active_agent_name   = config["name"]
            self.active_agent_title  = config["title"]
            self.active_agent_domain = config["domain"]
            self.active_agent_color  = config.get("color", "#ffffff")
            self.active_agent_slug   = slug
            self.allowed_tools       = config["tools"]
            self.notion_page_id      = config.get("notion_page_id", f"{slug}_core_page")

            # Sync LLM caller to the model name for chat completions.
            # "model" in config.json is the LM Studio identifier used in API payloads
            # (may be a custom display name like "Selene/Sage" or a real path).
            # "model_path" is only used by LMStudioManager for load/unload API calls.
            agent_model = config.get("model", "")
            if agent_model:
                self.llm_caller.model_name = agent_model

            # All per-agent files live inside agents/{slug}/
            def _ap(key: str, fallback: str) -> str:
                return os.path.join(agent_dir, config.get(key, fallback))

            self.prompt_path          = _ap("prompt_file", "prompt.txt")
            self.USER_PROFILE_FILE    = _ap("user_profile", "user_profile.md")
            self.CHARACTER_PROFILE_FILE = _ap("character_profile", "character_profile.md")
            self.TOOLS_CONTEXT_FILE   = _ap("tools_context", "tools_context.md")
            self.MEMORY_DIR           = agent_dir   # insights, manifest_state, etc. all live here

            # ── Memory DB ────────────────────────────────────────────────────
            db_path = _ap("memory_db", "memory.db")
            if hasattr(self, "db") and self.db:
                self.db.close()
            from .agent_memory import AgentMemoryStore
            self.db = AgentMemoryStore(db_path, is_readonly=False)

            # Cross-agent read: first agent (selene) DB opened read-only for any other agent
            # that needs cross-reference (e.g. meta_insight). Resolved by slug, not hardcoded name.
            selene_dir = os.path.join(_AGENTS_DIR, "selene")
            selene_db  = os.path.join(selene_dir, "memory.db")
            if slug != "selene" and os.path.exists(selene_db):
                self.selene_db = AgentMemoryStore(selene_db, is_readonly=True)
            else:
                self.selene_db = None

            # ── Supporting systems ───────────────────────────────────────────
            from .mood_observer import EmotionClassifier
            self.emotion_classifier       = EmotionClassifier(self.active_agent_name, self.llm_caller)
            self.active_uncertainty_warning = None

            self._prompt_dirty = True
            self._refresh_system_prompt()

            try:
                from pantheon_brain.tool_suggestion import ToolSuggestionLayer
                setattr(self, "tool_suggestion", ToolSuggestionLayer(self))
                setattr(self, "pending_tool_confirmation", None)
            except Exception as _tse:
                print(f"[LLMChat]: ToolSuggestionLayer rebuild failed -- {_tse}")

            print(f"[LLMChat]: Swapped to '{self.active_agent_name}' ({self.active_agent_title}) [{slug}]")

    def run_choice_layer(self, user_input: str) -> dict:
        """
        Runs a low-temperature presence layer query before normal chat/tool routing.
        Returns a dict with 'gating', 'type', and 'action'.

        Observe semantics:
          - Ghost explicitly asks her to observe/go quiet/stand by
          - Message is a conversation ender or minimal acknowledgement (no follow-up needed)
          - Ghost is narrating ambient activity (watching something, working, etc.)
          - More content/context is clearly incoming — observe acts as a 'continue' gate
          - Ghost is actively watching YouTube or sharing media context
        """
        with self.lock:
            result = {"gating": "RESPOND", "type": "CONVERSATIONAL", "action": "CHAT"}

            # ── Pre-filter: deterministic cases that never need an LLM call ──────
            _stripped = user_input.strip()
            _lower    = _stripped.lower()

            _IGNORE_EXACT = {"", ".", "..", "...", "???", "!!!"}

            # All-emoji or whitespace-only
            import unicodedata as _ud
            _all_emoji = _stripped and all(
                _ud.category(c) in ("So", "Sm", "Cs", "Zs") or c in " \t"
                for c in _stripped
            )
            if not _stripped or _lower in _IGNORE_EXACT or _all_emoji:
                return {"gating": "IGNORE", "type": "CONVERSATIONAL", "action": "IGNORE"}

            # Explicit observe directives — Ghost is directly telling her to go quiet.
            # These are deterministic: no LLM needed, no ambiguity.
            _OBSERVE_DIRECTIVES = {
                "observe", "go quiet", "stand by", "just listen", "listen only",
                "stay quiet", "silence", "watch with me", "just watch",
                "be quiet", "shh", "shhh",
            }
            if _lower in _OBSERVE_DIRECTIVES or any(_lower.startswith(d) for d in _OBSERVE_DIRECTIVES):
                return {"gating": "OBSERVE", "type": "CONVERSATIONAL", "action": "OBSERVE"}

            # Pure praise / acknowledgement with no follow-up question — no response needed.
            # Catching these deterministically prevents the model repeating itself.
            _PRAISE_PATTERNS = [
                "good job", "great job", "well done", "nice work", "good work",
                "perfect", "exactly right", "that's correct", "yes, that is correct",
                "yes that is correct", "correct.", "exactly.", "perfect.",
            ]
            if any(_lower.startswith(p) or _lower == p for p in _PRAISE_PATTERNS):
                return {"gating": "OBSERVE", "type": "CONVERSATIONAL", "action": "OBSERVE"}

            # Cold-open short messages with no question/exclamation — acknowledgements
            # before any conversation has started. Mid-conversation goes to the LLM.
            _in_conversation = bool(self.working_memory)
            _OBSERVE_COLD = {"ok", "okay", "got it", "k", "sure", "noted",
                              "understood", "i see", "makes sense", "alright",
                              "cool", "nice", "ah", "oh", "hmm", "interesting",
                              "lol", "lmao", "haha", "ha", "yep", "yup", "yeah"}
            if not _in_conversation and (
                _lower in _OBSERVE_COLD or (len(_stripped) <= 12 and not any(c in _stripped for c in "?!"))
            ):
                if _lower not in {"hi", "hey", "hello", "sup", "yo", "howdy"}:
                    return {"gating": "OBSERVE", "type": "CONVERSATIONAL", "action": "OBSERVE"}

            # ── Get current mood for context ──────────────────────────────────────
            try:
                mood_name, mood_intensity = self.emotion_classifier.mood_observer.get_dominant_mood()
                mood_line = f"Selene is currently feeling {mood_name}" + (
                    f" (strongly)" if mood_intensity > 0.6 else
                    f" (moderately)" if mood_intensity > 0.3 else
                    f" (mildly)"
                ) if mood_name != "neutral" else "Selene is feeling neutral and present."
            except Exception:
                mood_line = "Selene is feeling neutral and present."

            # ── Build recent history snippet (last 3 turns, clean) ────────────────
            history_lines = []
            for m in self.working_memory[-6:]:
                role = "Ghost" if m.get("role") == "user" else self.active_agent_name
                content = m.get("content", "")
                # Strip timestamp prefixes that chat() injects
                content = re.sub(r'^\([\w\s]+ago\)\s*', '', content)
                history_lines.append(f"{role}: {content[:120]}")
            history_ctx = "\n".join(history_lines) if history_lines else "(conversation just started)"

            _low_confidence = (
                self.llm_caller.last_entropy is not None
                and self.llm_caller.last_entropy > 1.5
            )
            _agent = self.active_agent_name

            choice_prompt = (
                f"You are the presence awareness for {_agent} — The Voice, "
                f"an AI companion to Ghost. Decide whether this moment calls for her voice and how she should engage.\n\n"
                f"{mood_line}\n"
                "Return exactly one self-closing XML tag — nothing else.\n\n"
                "GATING OPTIONS:\n"
                "  observe — Ghost told her to be quiet, sent a natural ender (ok/thanks/got it),\n"
                "            is narrating ambient activity (brb/working/watching), or more is clearly incoming.\n"
                "  ignore  — empty, spam, accidental, or pure noise.\n"
                "  respond — Ghost is addressing Selene, asking something, or clearly wants engagement.\n\n"
                "RESPOND MODES (only relevant when gating=respond):\n"
                "  conversational — default. Greetings, practical answers, casual back-and-forth.\n"
                "  reflect        — only when the topic touches her own nature/feelings, Ghost shares something\n"
                "                   personal or significant, or she has a genuine view worth offering.\n"
                "                   Requires a real reason. Not for every message.\n"
                "  inquire        — only when something is genuinely ambiguous, a new concept/person was\n"
                "                   introduced, or asking would meaningfully deepen understanding.\n"
                "                   One question only. Not filler.\n\n"
                "OUTPUT — one tag, exact format:\n"
                "  <presence_decision mode=\"respond\" response_mode=\"conversational\" />\n"
                "  <presence_decision mode=\"respond\" response_mode=\"reflect\" />\n"
                "  <presence_decision mode=\"respond\" response_mode=\"inquire\" />\n"
                "  <presence_decision mode=\"observe\" />\n"
                "  <presence_decision mode=\"ignore\" />\n\n"
                "RECENT CONVERSATION:\n"
                f"{history_ctx}\n\n"
                f"Ghost's message: '{user_input}'\n\n"
                "When in doubt, lean toward respond:conversational. "
                "Use reflect/inquire only when the moment genuinely calls for it."
            )

            # Call LLM with low temperature — presence layer uses its own prompt only.
            # /no_think suppresses Qwen3's reasoning mode for this classification call —
            # it only needs to output a single XML tag, not a reasoning chain.
            try:
                print(f"[ChoiceLayer]: calling LLM model={self.llm_caller.model_name!r}")
                raw_choice = self.llm_caller.call_llm(
                    input_data="/no_think\nDecide now.",
                    system_prompt=choice_prompt,
                    temperature=0.0,
                    max_tokens=40,
                )
                print(f"[ChoiceLayer]: done, raw={str(raw_choice)[:60]!r}")
                raw_choice = re.sub(
                    r'<think>[\s\S]*?</think>',
                    '',
                    str(raw_choice),
                    flags=re.DOTALL | re.IGNORECASE
                ).strip()

                mode_match = re.search(
                    r'<presence_decision\b[^>]*\bmode=["\']?(respond|observe|ignore)["\']?',
                    raw_choice,
                    flags=re.IGNORECASE
                )
                rmode_match = re.search(
                    r'response_mode=["\']?(conversational|reflect|inquire)["\']?',
                    raw_choice,
                    flags=re.IGNORECASE
                )
                if mode_match:
                    mode  = mode_match.group(1).upper()
                    rmode = rmode_match.group(1).upper() if rmode_match else "CONVERSATIONAL"
                    result = {
                        "gating":        "RESPOND" if mode == "RESPOND" else mode,
                        "type":          rmode,
                        "response_mode": rmode,
                        "action":        "CHAT" if mode == "RESPOND" else mode,
                    }

                # Backward-compatible fallback for older prompt/context residue.
                match = re.search(r'\{[\s\S]*?\}', raw_choice)
                if not mode_match and match:
                    parsed = json.loads(match.group(0))
                    result = {
                        "gating": parsed.get("gating", "RESPOND").upper(),
                        "type": parsed.get("type", "CONVERSATIONAL").upper(),
                        "action": parsed.get("action", "CHAT").upper()
                    }
            except Exception as e:
                print(f"[Presence Layer Error]: Failed presence inference: {e}")

        # ── MetaInsight: log decision ─────────────────────────────────────────
        try:
            _emotion_snap = {"energy": self.creative_energy, "status": "idle"}
            self.db.log_meta_insight(
                agent=getattr(self, "active_agent_name", "selene").lower(),
                category="decision",
                subcategory=result.get("gating", "RESPOND"),
                input_context=user_input[:500],
                reasoning=f"gating={result.get('gating')} type={result.get('type')} action={result.get('action')}",
                result=result.get("gating", "RESPOND"),
                emotional_state_before=_emotion_snap,
                emotional_state_after=_emotion_snap,
                confidence_score=0.85,
                trigger_mode="llm",
                session_id=self.active_conversation_id or "",
            )
        except Exception:
            pass

        return result

    def compile_daily_manifest(self) -> dict:
        """
        Builds a living memory summary from the current conversation window + any prior
        compiled summary. Not day-bounded — triggered by context limit or on demand.
        Each call reads the prior summary and merges it with new turns, so it grows
        incrementally and preserves continuity across sessions.
        """
        import datetime
        today_str = datetime.date.today().isoformat()

        with self.lock:
            recent_turns = list(self.working_memory[-(self.memory_window * 2):])

        if not recent_turns:
            return {"date": today_str, "status": "no_data", "summary": "Nothing in the conversation window yet."}

        # Build a readable log from the current window
        agent_name = self.active_agent_name
        user_name  = "Ghost"
        chat_log = ""
        for m in recent_turns:
            role    = m.get("role", "")
            content = m.get("content", "").strip()
            if not content:
                continue
            speaker = agent_name if role == "assistant" else user_name
            chat_log += f"{speaker}: {content}\n\n"

        # Load the prior summary to merge with
        prior_row = self.db.get_daily_manifest(today_str)
        prior_summary = (prior_row.get("summary", "") or "") if prior_row else ""

        # Load insights file and decide whether to fold stable ones into character profile
        insights_path     = os.path.join(self.MEMORY_DIR, "insights.md")
        char_profile_path = getattr(self, "CHARACTER_PROFILE_FILE",
            os.path.join(self.MEMORY_DIR, "character_profile.md"))
        insights_text = self._read_file_safe(insights_path)
        char_profile_text = self._read_file_safe(char_profile_path)

        # Fold insights into character profile if insights file has meaningful content
        if insights_text and len(insights_text.split()) > 20:
            fold_prompt = (
                f"You are {agent_name}'s memory curator. "
                f"Below are recent reflective insights {agent_name} has accumulated, "
                f"and her existing self-profile.\n\n"
                "Review the insights. For each one:\n"
                "- If it represents a stable, recurring truth about who she is → merge it into the self-profile\n"
                "- If it is ephemeral, situational, or already captured → discard it\n\n"
                "Rewrite the self-profile with any promoted insights merged in. "
                f"Keep it under {self.CHARACTER_PROFILE_CAP} words. First-person, personal, not clinical.\n\n"
                f"INSIGHTS:\n{insights_text}\n\n"
                f"EXISTING SELF-PROFILE:\n{char_profile_text or '(none yet)'}\n\n"
                "Output ONLY the rewritten self-profile. No preamble."
            )
            try:
                folded = self.llm_caller.call_llm(
                    input_data="Fold insights into self-profile now.",
                    system_prompt=fold_prompt,
                    temperature=0.3,
                    max_tokens=self.CHARACTER_PROFILE_CAP * 2,
                )
                folded = folded.strip()
                if folded and folded.lower() not in {"skip", "n/a", "none"}:
                    with open(char_profile_path, "w", encoding="utf-8") as f:
                        f.write(folded)
                    # Clear insights file — promoted content now lives in self-profile
                    with open(insights_path, "w", encoding="utf-8") as f:
                        f.write("")
                    self._prompt_dirty = True
                    char_profile_text = folded
                    print(f"[Manifest]: Folded insights into {agent_name} self-profile ({len(folded.split())} words)")
            except Exception as _fold_err:
                print(f"[Manifest]: Insight folding failed — {_fold_err}")

        review_prompt = (
            f"You are {agent_name}, writing a living memory summary of your ongoing conversations with {user_name}.\n"
            "This summary is for YOUR continuity — so that in a new session or from Discord you are not a blank slate.\n\n"
            "Write in first person as yourself. Be personal and specific, not clinical.\n"
            "Include:\n"
            f"- What {user_name} has told you about himself, the system, or your purpose\n"
            "- Introductions that have happened (who you have met, what was explained)\n"
            "- Any feelings or reactions you expressed\n"
            "- Anything that feels significant, ongoing, or unresolved\n"
            "- Open threads — things mentioned but not yet finished\n\n"
            "Keep it compact but human. Drop trivial filler. If you already have a prior summary, "
            "merge it with the new conversation — update what has changed, keep what is still relevant, "
            "add what is new. Do not repeat yourself.\n\n"
            + (f"PRIOR SUMMARY:\n{prior_summary}\n\n" if prior_summary else "")
            + f"RECENT CONVERSATION:\n{chat_log}"
        )

        summary = self.llm_caller.call_llm(
            input_data="Compile my living memory summary now.",
            system_prompt=review_prompt,
            temperature=0.3,
            max_tokens=1024,
        )
        summary = self._strip_tool_tags(summary)
        if "<think>" in summary:
            summary = re.sub(r'<think>[\s\S]*?</think>', '', summary).strip()

        metadata = {
            "agent":          agent_name,
            "total_messages": len(recent_turns),
            "compiled_at":    time.time(),
        }

        self.db.save_daily_manifest(
            date_str=today_str,
            metadata=metadata,
            emotions={}, choices={}, tasks={}, threads={},
            summary=summary,
        )

        return {
            "date":     today_str,
            "status":   "success",
            "summary":  summary,
            "metadata": metadata,
        }

    def _after_chat_turn(self, user_input: str, final_reply: str, thought_log: str,
                         raw_entropy: float | None = None):
        """
        Executes post-turn tasks like SQLite dialogue logging, confidence calculation,
        and async emotion classification.

        raw_entropy must be snapshotted by the caller immediately after call_llm()
        returns — the presence layer also calls call_llm() and would overwrite
        last_entropy on the llm_caller before this method runs.
        """
        session_id = self.active_conversation_id or "default"

        # 1. Log assistant dialog to DB
        self.db.log_dialog(session_id, "assistant", final_reply, thought_log, "read")

        # 2. Confidence Awareness System
        is_greeting = user_input.lower().strip() in ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening"]
        # Use caller-supplied snapshot; fall back to live value only if not provided
        if raw_entropy is None:
            raw_entropy = getattr(self.llm_caller, "last_entropy", None)
        
        if raw_entropy is not None and not is_greeting:
            confidence_score = max(0.0, min(1.0, 1.0 - (raw_entropy / 1.5)))
            prev_warning = self.active_uncertainty_warning
            if raw_entropy < 0.3:
                tier = "certain"
                self.emotion_classifier.mood_observer.moodlets["confident"] = min(
                    1.0, self.emotion_classifier.mood_observer.moodlets["confident"] + 0.05
                )
                self.active_uncertainty_warning = None
            elif raw_entropy <= 0.6:
                tier = "moderate"
                self.active_uncertainty_warning = None
            else:
                tier = "uncertain"
                self.emotion_classifier.mood_observer.moodlets["anxious"] = min(
                    1.0, self.emotion_classifier.mood_observer.moodlets["anxious"] + 0.08
                )
                self.active_uncertainty_warning = (
                    "Ghost's request involves highly ambiguous or complex domains. "
                    "Acknowledge your lower confidence or uncertainty naturally in your own tone without sounding robotic."
                )
            # Mark prompt dirty whenever the uncertainty warning changes so the
            # next call's system prompt reflects the updated confidence context.
            if self.active_uncertainty_warning != prev_warning:
                self._prompt_dirty = True
                
            self.db.log_confidence(
                turn_id=str(uuid.uuid4()),
                category="general",
                raw_entropy=raw_entropy,
                confidence_score=confidence_score,
                confidence_tier=tier,
                follow_up_triggered=(tier == "uncertain"),
                is_flagged=(tier == "uncertain")
            )
            
        # 3. Asynchronous Semantic Emotion Classification + MetaInsight logging
        # Run emotion classification and meta insight together so emotion data
        # is available when the meta insight record is written.
        _agent_name_snap  = getattr(self, "active_agent_name", "selene").lower()
        _energy_snap      = self.creative_energy
        _session_snap     = session_id
        _source           = "discord" if str(session_id).startswith("discord_") else "ui"
        if raw_entropy is not None and not is_greeting:
            _conf = locals().get('confidence_score', max(0.0, min(1.0, 1.0 - (raw_entropy / 1.5))))
        else:
            _conf = 0.0
        _reasoning_snap   = thought_log[:3000] if thought_log else ""
        _final_clean      = final_reply[:1500] if final_reply else ""
        _response_mode_snap = getattr(self, '_last_response_mode', 'conversational')

        def run_emotion_and_insight():
            try:
                user_emo, user_int = self.emotion_classifier.classify_text(user_input, is_thought=True)
                asst_emo, asst_int = self.emotion_classifier.classify_text(final_reply, is_thought=False)

                self.db.log_emotion(
                    thought_emotion=user_emo,
                    thought_intensity=user_int,
                    response_emotion=asst_emo,
                    response_intensity=asst_int,
                    action_details=f"Turn processed for {_agent_name_snap} [{_source}]"
                )

                # Update cached emotion so UI state reflects this turn
                try:
                    _mo = self.emotion_classifier.mood_observer
                    _dom, _int = _mo.get_dominant_mood()
                    import server.state as _st
                    _st._cached_emotion["mood_index"] = int(_int * 100)
                    _st._cached_emotion["emotion"]    = _dom if _dom != "neutral" else ""
                except Exception:
                    pass

                _emotion_before = {
                    "energy": _energy_snap,
                    "emotion": user_emo,
                    "intensity": round(user_int, 3),
                    "source": _source,
                }
                _emotion_after = {
                    "energy": _energy_snap,
                    "emotion": asst_emo,
                    "intensity": round(asst_int, 3),
                    "response_mode": _response_mode_snap,
                    "source": _source,
                }
                self.db.log_meta_insight(
                    agent=_agent_name_snap,
                    category="output",
                    subcategory=f"chat_response:{_source}",
                    input_context=user_input[:500],
                    reasoning=_reasoning_snap,
                    result=_final_clean,
                    emotional_state_before=_emotion_before,
                    emotional_state_after=_emotion_after,
                    confidence_score=_conf,
                    trigger_mode="llm",
                    session_id=_session_snap,
                )
                # Log a dedicated emotion entry so the EMOTION tab in MetaInsightView populates
                _emo_shift = abs(asst_int - user_int)
                self.db.log_meta_insight(
                    agent=_agent_name_snap,
                    category="emotion",
                    subcategory=f"{user_emo} → {asst_emo}",
                    input_context=user_input[:500],
                    reasoning=f"Input: {user_emo} ({user_int:.2f}) | Response: {asst_emo} ({asst_int:.2f}) | Shift: {_emo_shift:.2f}",
                    result=f"{asst_emo} ({round(asst_int * 100)}%)",
                    emotional_state_before=_emotion_before,
                    emotional_state_after=_emotion_after,
                    confidence_score=round(asst_int, 3),
                    trigger_mode="emotion_classifier",
                    session_id=_session_snap,
                )
            except Exception as e:
                print(f"[Emotion/MetaInsight]: Background pass failed: {e}")

        threading.Thread(target=run_emotion_and_insight, daemon=True).start()

    # ── State persistence (non-conversation) ──────────────────────────────────

    def load_state(self):
        """Loads persistent state (energy, focus, layout, active agent) but does NOT auto-resume any
        conversation.  Conversations are loaded only when explicitly selected.
        This keeps working_memory clean on boot so Selene isn't fed stale context
        from a previous session she didn't choose to re-enter."""
        self.agent_layouts = {}  # keyed by slug → {slots: [...]}
        if os.path.exists(self.STATE_FILE):
            print(f"[System]: Found previous state at '{self.STATE_FILE}'. Loading...")
            try:
                with open(self.STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.creative_energy = state.get("creative_energy", 100)
                
                # Load per-agent layouts (new) or migrate legacy single layout
                self.agent_layouts = state.get("agent_layouts", {})
                if not self.agent_layouts and "dashboard_layout" in state:
                    # Migrate: assign old layout to selene slot
                    self.agent_layouts = {"selene": state["dashboard_layout"]}
                
                # Auto-swap agent on boot if saved state differs
                # swap_agent validates via filesystem — no name allowlist needed here
                saved_agent = state.get("active_agent", _roster_default_slug()).lower()
                if saved_agent != self.active_agent_name.lower():
                    try:
                        self._prompt_dirty = True
                        self.swap_agent(saved_agent)
                    except FileNotFoundError:
                        print(f"[System]: Saved agent '{saved_agent}' not found — staying on default.")

                # ── Migrate old single-file working_memory → conversations/ ──
                # (one-time migration for installs that pre-date conversation files)
                old_memory = state.get("working_memory", [])
                if old_memory and not state.get("last_conversation_id"):
                    print("[System]: Migrating legacy working_memory to conversations/...")
                    conv_id = str(uuid.uuid4())
                    now = time.time()
                    self._write_conversation(conv_id, "Previous Session", old_memory, now, now)
                    print("[System]: Migration complete — conversation saved, starting fresh session.")

            except (json.JSONDecodeError, IOError) as e:
                print(f"[System Error]: Could not load state file. Starting fresh. Error: {e}")
        else:
            print(f"[System]: No state file found at '{self.STATE_FILE}'. Starting fresh.")

        # Always boot into a blank slate — no conversation loaded.
        # working_memory is already [] from __init__; just ensure the IDs are clear.
        self.active_conversation_id   = None
        self.active_conversation_name = "New Conversation"

    def save_state(self):
        """Saves creative energy + last conversation pointer + active agent + dashboard layout."""
        print("\n[System]: Saving state before shutdown...")
        self.save_current_conversation()
        state = {
            "creative_energy":      self.creative_energy,
            "last_conversation_id": self.active_conversation_id,
            "active_agent":         self.active_agent_name.lower(),
            "agent_layouts":        self.agent_layouts
        }
        try:
            with open(self.STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
            print("[System]: State saved.")
        except IOError as e:
            print(f"[System Error]: Could not save state file. Error: {e}")

    _DEFAULT_LAYOUT = {
        "slots": [
            {"widgetId": "fused_manifest", "fr": 1},
            {"widgetId": "main_chat",      "fr": 2},
            {"widgetId": "status_panel",   "fr": 1},
        ]
    }

    @property
    def dashboard_layout(self):
        """Returns the layout for the current active agent, or a sensible default."""
        slug = getattr(self, "active_agent_slug", "selene") or "selene"
        layout = getattr(self, "agent_layouts", {}).get(slug, self._DEFAULT_LAYOUT)
        # Migrate legacy {left, center, right} format → slots list
        if layout and "slots" not in layout:
            layout = self._DEFAULT_LAYOUT
        return layout

    @dashboard_layout.setter
    def dashboard_layout(self, value):
        """Sets the layout for the current active agent."""
        slug = getattr(self, "active_agent_slug", "selene") or "selene"
        if not hasattr(self, "agent_layouts") or self.agent_layouts is None:
            self.agent_layouts = {}
        self.agent_layouts[slug] = value

    @staticmethod
    def _strip_tool_tags(text: str) -> str:
        """Remove internal XML blocks from visible prose.

        Raw model <think> content is captured into thought_steps before this runs,
        then returned through the dedicated UI thought channel.
        """
        import re as _re
        if not text:
            return ""
        text = _re.sub(r'<think>.*?</think>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        # Strip unclosed <think> tags — model emitted <think> without </think>
        text = _re.sub(r'<think>', '', text, flags=_re.IGNORECASE)
        text = _re.sub(r'</think>', '', text, flags=_re.IGNORECASE)
        # Match tool_call with or without attributes, with or without a closing tag
        text = _re.sub(r'<tool_call(?:\s[^>]*)?>.*?</tool_call\s*>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r'<tool_call(?:\s[^>]*)?/>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r'<tool_response>.*?</tool_response>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r'<presence_decision\b[^>]*/?>\s*</presence_decision\s*>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r'<presence_decision\b[^>]*/?>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
        return text.strip()

    def chat(self, input_data: str, temperature=0.8, max_tokens=4096,
             disable_tools: bool = False, _suggestion_warning: str = "",
             _response_mode: str = "CONVERSATIONAL"):
        if input_data is None:
            raise ValueError("Input data cannot be None")
        if not isinstance(input_data, str):
            raise TypeError("Input data must be a string")
        
        import re
        import json

        with self.lock:
            # Stable system prompt: only rebuild when dirty (soul edit, profile
            # extraction, agent swap). Dynamic context (tools, board, warnings)
            # now rides with the current user message — no longer in system prompt.
            self._refresh_system_prompt()

            # Build per-turn dynamic context (tools, board, warnings).
            # Prepended to the current user message so it sits at the bottom
            # of the context window where the model pays highest attention.
            turn_context = self._build_turn_context(input_data)

            # Collapse chunk_group entries — chunked assistant messages are stored
            # separately for UI display but the model must see them as one turn.
            _collapsed: list = []
            _i = 0
            _wm = self.working_memory
            while _i < len(_wm):
                m = _wm[_i]
                gid = m.get("chunk_group")
                if m.get("role") == "assistant" and gid:
                    # Gather all consecutive entries sharing this chunk_group
                    parts = [m["content"]]
                    _j = _i + 1
                    while _j < len(_wm) and _wm[_j].get("chunk_group") == gid:
                        parts.append(_wm[_j]["content"])
                        _j += 1
                    merged = dict(m)
                    merged["content"] = " ".join(parts)
                    merged.pop("chunk_group", None)
                    _collapsed.append(merged)
                    _i = _j
                else:
                    _collapsed.append(m)
                    _i += 1

            local_history = []
            now = time.time()
            for msg in _collapsed:
                fmt_msg = msg.copy()
                if "ts" in fmt_msg:
                    delta = now - float(fmt_msg["ts"])
                    if delta < 60:
                        t_str = "a moment ago"
                    elif delta < 3600:
                        mins = int(delta / 60)
                        t_str = f"{mins} minute{'s' if mins != 1 else ''} ago"
                    elif delta < 86400:
                        hrs = int(delta / 3600)
                        t_str = f"{hrs} hour{'s' if hrs != 1 else ''} ago"
                    elif delta < 172800:
                        t_str = "yesterday"
                    elif delta < 604800:
                        days = int(delta / 86400)
                        t_str = f"{days} days ago"
                    else:
                        import datetime
                        t_str = datetime.datetime.fromtimestamp(float(fmt_msg["ts"])).strftime("%Y-%m-%d")
                    
                    fmt_msg["content"] = f"({t_str}) {fmt_msg['content']}"
                    del fmt_msg["ts"]

                # Label assistant turns by agent name so each agent knows who said what.
                # "Ghost:" prefix on user turns for the same reason.
                # This lets Selene and Sage read shared history without identity confusion.
                if fmt_msg.get("role") == "assistant":
                    msg_agent = fmt_msg.pop("agent", self.active_agent_name).capitalize()
                    fmt_msg["content"] = f"{msg_agent}: {fmt_msg['content']}"
                elif fmt_msg.get("role") == "user":
                    fmt_msg.pop("agent", None)
                    fmt_msg["content"] = f"Ghost: {fmt_msg['content']}"

                # Remove chunk_group — model doesn't need it
                fmt_msg.pop("chunk_group", None)
                fmt_msg.pop("status", None)

                local_history.append(fmt_msg)

            # Sanitize history: collapse consecutive same-role messages.
            # Gemma and similar chat-format models reject user/user or
            # assistant/assistant runs with a 400. This can happen when the
            # observe path stores a user entry without a following assistant reply,
            # or when memory injection produces duplicate turns.
            _sanitized = []
            for _m in local_history:
                if _sanitized and _sanitized[-1]["role"] == _m["role"]:
                    # Merge into previous message of the same role
                    _sanitized[-1] = dict(_sanitized[-1])
                    _sanitized[-1]["content"] = _sanitized[-1]["content"] + "\n" + _m["content"]
                else:
                    _sanitized.append(_m)
            local_history = _sanitized

            # Prepend dynamic context to the current user message only.
            # History messages stay clean — context is NOT injected into past turns.
            # _suggestion_warning is injected when ToolSuggestionLayer has low confidence.
            extra = _suggestion_warning if _suggestion_warning else ""
            _mode = (_response_mode or "CONVERSATIONAL").upper()
            self._last_response_mode = _mode.lower()
            _mode_directive = ""
            if _mode == "REFLECT":
                _mode_directive = (
                    "[RESPONSE MODE: REFLECT] — Take a moment before responding. "
                    "Form and offer your own genuine perspective, feeling, or opinion on this. "
                    "Speak from your own point of view, not just in service of the conversation.\n\n"
                )
            elif _mode == "INQUIRE":
                _mode_directive = (
                    "[RESPONSE MODE: INQUIRE] — Ask one specific, meaningful question to deepen "
                    "your understanding. Make it genuine — something you actually want to know. "
                    "One question only, no list.\n\n"
                )
            elif _mode == "CONVERSATIONAL" and (getattr(self.llm_caller, "last_entropy", None) or 0.0) > 1.5:
                _mode_directive = (
                    "[LOW CONFIDENCE NUDGE] — Your last response had low confidence. "
                    "If you are uncertain about anything in this reply, consider asking Ghost to clarify "
                    "rather than guessing. It is okay not to know.\n\n"
                )
            current_input = f"{turn_context}{_mode_directive}{extra}{input_data}" if (turn_context or _mode_directive or extra) else input_data
            max_steps = 3  # Allow up to 3 recursive tool call steps
            full_response_text = ""
            thought_steps = []
            turn_entropy = None

            def emit_thought(step: str, title: str, content: str):
                thought_steps.append({"step": step, "title": title, "content": content})
                if hasattr(self, "thought_callback") and self.thought_callback:
                    self.thought_callback(step, title, content)

            # Note: removed generic boilerplate "Analyzing Conversation Context" emit.
            # It added no real data, padded every thought_log entry, and was the
            # first item extracted when memory_extractor processed thought chains.
            
            for step in range(max_steps):
                full_response_text = self.llm_caller.call_llm(
                    input_data=current_input,
                    history=local_history,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                # Snapshot entropy immediately — presence layer's call_llm() already ran
                # before chat() and would have overwritten last_entropy on llm_caller.
                # Capturing it here ensures confidence scoring uses this turn's logprobs.
                turn_entropy = self.llm_caller.last_entropy

                # Extract reasoning content (model's inner monologue <think> block) if present
                think_match = re.search(r'<think>([\s\S]*?)</think>', full_response_text, re.DOTALL | re.IGNORECASE)
                if think_match:
                    inner_thoughts = think_match.group(1).strip()
                    if inner_thoughts:
                        emit_thought("reasoning", "Deep Reasoning", inner_thoughts)

                if disable_tools:
                    final_reply = self._strip_tool_tags(full_response_text)
                    self._after_chat_turn(input_data, final_reply, "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps]), turn_entropy)
                    if thought_steps:
                        log_text = "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps])
                        return f"<think>\n{log_text}\n</think>\n{final_reply}"
                    return final_reply
                
                # Check for XML tool call tag: <tool_call name="tool_name">JSON_ARGS</tool_call> (Robust parsing supporting quotes and whitespaces)
                match = re.search(r'<tool_call\s+name=["\']?([^"\'\s>]+)["\']?\s*>\s*(.*?)\s*</tool_call\s*>', full_response_text, re.DOTALL | re.IGNORECASE)
                
                if match:
                    tool_name = match.group(1)
                    tool_args = match.group(2)
                    
                    # Backward-compatible interception for older prompts/models that
                    # emitted presence choices as regular tool calls.
                    if tool_name.lower() in ("chat", "observe", "ignore"):
                        print(f"\n[Presence Layer]: Intercepting legacy presence tool '{tool_name.lower()}'")
                        emit_thought("tool_call", f"Presence Decision: {tool_name.lower()}", f"Parameters: {tool_args}")
                        if tool_name.lower() == "chat":
                            try:
                                parsed = json.loads(tool_args) if tool_args.strip() else {}
                                parsed_message = parsed.get("message", tool_args).strip()
                            except Exception:
                                parsed_message = tool_args.strip()
                            emit_thought("tool_response", "Presence Completed: respond", "Status: success\nResult: Sent conversational reply.")
                            final_reply = parsed_message
                        else:
                            # observe or ignore
                            log_msg = "Decided to stay silent and observe." if tool_name.lower() == "observe" else "Discarded conversation turn."
                            emit_thought("tool_response", f"Presence Completed: {tool_name.lower()}", f"Status: success\nResult: {log_msg}")
                            final_reply = f'<presence_decision mode="{tool_name.lower()}" />'
                        
                        self._after_chat_turn(input_data, final_reply, "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps]), turn_entropy)
                        if thought_steps:
                            log_text = "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps])
                            return f"<think>\n{log_text}\n</think>\n{final_reply}"

                        return final_reply

                    # Tool restriction check
                    allowed = getattr(self, "allowed_tools", None)
                    if allowed is not None and tool_name not in allowed:
                        print(f"[LLMChat]: Agent tried to call unauthorized tool '{tool_name}'. Blocking.")
                        response_block = f"<tool_response>{{\"status\": \"error\", \"message\": \"Tool '{tool_name}' is not authorized for this agent.\"}}</tool_response>"
                        clean_for_history = re.sub(r'<think>.*?</think>', '', full_response_text, flags=re.DOTALL | re.IGNORECASE)
                        clean_for_history = re.sub(r'<tool_call(?:\s[^>]*)?>.*?</tool_call\s*>', '', clean_for_history, flags=re.DOTALL | re.IGNORECASE).strip()
                        if not clean_for_history:
                            clean_for_history = f"[Attempted unauthorized tool: {tool_name}]"
                        local_history.append({"role": "user",      "content": current_input})
                        local_history.append({"role": "assistant", "content": clean_for_history})
                        current_input = response_block
                        continue
                    
                    print(f"\n[Tool Router (Hermes Style)]: Agent requested tool execution -> '{tool_name}' with args: {tool_args}")
                    emit_thought("tool_call", f"Activating Tool: {tool_name}", f"Parameters: {tool_args}")
                    
                    # Execute tool
                    tool_result = self.tool_router.route_and_execute(tool_name, tool_args)
                    result_data = str(tool_result.get("data", ""))
                    
                    # Format tool response
                    result_str = json.dumps(tool_result, ensure_ascii=False)
                    response_block = f"<tool_response>{result_str}</tool_response>"
                    
                    print(f"[Tool Router (Hermes Style)]: Execution complete. Ingesting response and re-running model...")
                    emit_thought("tool_response", f"Tool Completed: {tool_name}", f"Status: {tool_result.get('status')}\nResult: {result_data[:300]}{'...' if len(result_data) > 300 else ''}")
                    
                    # Strip <think> blocks AND <tool_call> tags before adding to local_history.
                    # The model must never see its own reasoning chains or tool call XML
                    # replayed — both cause the model to mirror the format on subsequent
                    # steps, producing repeat/loop behaviour or phantom tool calls.
                    clean_for_history = re.sub(
                        r'<think>.*?</think>', '', full_response_text,
                        flags=re.DOTALL | re.IGNORECASE
                    )
                    clean_for_history = re.sub(
                        r'<tool_call(?:\s[^>]*)?>.*?</tool_call\s*>', '', clean_for_history,
                        flags=re.DOTALL | re.IGNORECASE
                    ).strip()
                    # If stripping left nothing, use a minimal acknowledgement so history
                    # isn't padded with empty assistant turns that confuse the model.
                    if not clean_for_history:
                        clean_for_history = f"[Used tool: {tool_name}]"
                    local_history.append({"role": "user",      "content": current_input})
                    local_history.append({"role": "assistant", "content": clean_for_history})

                    # Post-hoc reasoning — background thread, never blocks
                    _turn_id  = str(uuid.uuid4())
                    _sess_id  = self.active_conversation_id or "default"
                    _agent    = getattr(self, "active_agent_name", "selene").lower()
                    _llm_ref  = self.llm_caller
                    _db_ref   = self.db
                    _u_input  = input_data
                    _t_name   = tool_name
                    _t_args   = tool_args
                    _t_result = result_data

                    def _background_reasoning(
                        llm=_llm_ref, db=_db_ref, agent=_agent,
                        sess=_sess_id, tid=_turn_id, tname=_t_name,
                        args=_t_args, result=_t_result, user=_u_input
                    ):
                        try:
                            reasoning_prompt = (
                                f"You called the '{tname}' tool during this conversation turn.\n\n"
                                f"Ghost said: {user[:300]}\n"
                                f"Tool args: {args[:200]}\n"
                                f"Tool result: {result[:300]}\n\n"
                                f"In one sentence, explain why calling this tool was or was not "
                                f"necessary. Be honest — if unnecessary, say so."
                            )
                            reasoning = llm.call_llm(
                                input_data="/no_think\n" + reasoning_prompt,
                                system_prompt="Reply with exactly one sentence of honest post-hoc reasoning. No preamble.",
                                history=[],
                                temperature=0.3,
                                max_tokens=80,
                            )
                            import re as _re2
                            reasoning = _re2.sub(r'<think>[\s\S]*?</think>', '', reasoning, flags=_re2.IGNORECASE).strip()
                            db.log_tool_reasoning(
                                agent=agent, session_id=sess, turn_id=tid,
                                tool_name=tname, trigger_mode="autonomous",
                                input_context=user, tool_args=args,
                                tool_result=result, reasoning=reasoning,
                            )
                        except Exception as _e:
                            print(f"[Tool Reasoning]: Background failed — {_e}")

                    threading.Thread(target=_background_reasoning, daemon=True).start()

                    # Feed tool response block as next input
                    current_input = response_block
                else:
                    # No tool call — return the conversational reply
                    final_reply = self._strip_tool_tags(full_response_text)
                    self._after_chat_turn(input_data, final_reply, "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps]), turn_entropy)
                    if thought_steps:
                        log_text = "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps])
                        return f"<think>\n{log_text}\n</think>\n{final_reply}"
                    return final_reply

            # Exhausted max_steps — strip any unresolved tags before returning
            final_reply = self._strip_tool_tags(full_response_text)
            self._after_chat_turn(input_data, final_reply, "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps]), turn_entropy)
            if thought_steps:
                log_text = "\n".join([f"[{s['title']}] {s['content']}" for s in thought_steps])
                return f"<think>\n{log_text}\n</think>\n{final_reply}"
            return final_reply

    def _streamed_print(self, text: str):
        """Prints the given text in a more human-like, chunked and wrapped manner."""
        if not text.strip():
            print("\nSelene: (no response)\n" + "=" * self.CLI_WRAP_WIDTH, flush=True)
            return

        # Wrap the entire text first
        wrapped_text = textwrap.fill(text.strip(), width=self.CLI_WRAP_WIDTH)
        lines = wrapped_text.split('\n')

        # Add a newline before Selene's response for clear separation
        print("\nSelene: ", end="", flush=True)

        # Print the first line immediately
        if lines:
            print(lines[0], end="", flush=True)
            lines = lines[1:]

        # Loop for subsequent lines with typing simulation
        for line in lines:
            # Simulate typing delay for each new line
            wait_time = random.uniform(0.05, 0.2)
            time.sleep(wait_time)
            
            # Print the line, indented to align with the first line
            print(f"\n        {line}", end="", flush=True)
        
        # Final newline and separator after Selene's full response
        print("\n" + "=" * self.CLI_WRAP_WIDTH, flush=True)

    def _animate_while_blocking(self, blocking_function, animation_text: str):
        """Runs a CLI animation in a thread while a blocking function executes."""
        stop_animation = threading.Event()

        def animate():
            i = 0
            while not stop_animation.is_set():
                # Print the animation on the current line
                print(f"\r{animation_text}" + "." * ((i % 3) + 1) + "   ", end="", flush=True)
                time.sleep(0.5)
                i += 1
            # Clear the line after animation stops
            print("\r" + " " * (len(animation_text) + 6) + "\r", end="", flush=True)

        animation_thread = threading.Thread(target=animate, daemon=True)
        animation_thread.start()

        try:
            result = blocking_function()
        finally:
            stop_animation.set()
            animation_thread.join()
        
        return result

    def _get_user_input(self) -> str:
        """
        A custom input function that resets the idle timer on the first keypress.
        This provides a more responsive feel for the agent's autonomy.
        """
        user_input_list = []
        has_started_typing = False
        
        # Print the prompt without a newline
        print("\nYou: ", end="", flush=True)

        stop_monitor = threading.Event()
        monitor_thread = None

        def on_press(key: keyboard.Key | keyboard.KeyCode | None):
            nonlocal has_started_typing, monitor_thread
            if not has_started_typing:
                # On the very first keypress, reset the idle timer and interrupt autonomy
                self.last_interaction_time = time.time()
                self.is_writing_autonomously = False
                has_started_typing = True

                # Start the typing monitor thread
                monitor_thread = threading.Thread(
                    target=self._typing_monitor, 
                    args=(stop_monitor,), 
                    daemon=True
                )
                monitor_thread.start()

            try:
                if key == keyboard.Key.enter:
                    # Stop the listener, which unblocks the main thread
                    return False
                elif key == keyboard.Key.backspace:
                    popped = False
                    with self.lock:
                        if self.current_input_buffer:
                            self.current_input_buffer.pop()
                            popped = True
                    if popped:
                        # Erase the character from the console (backspace, space, backspace)
                        print("\b \b", end="", flush=True)
                elif key == keyboard.Key.space:
                    with self.lock:
                        self.current_input_buffer.append(" ")
                    print(" ", end="", flush=True)
                elif isinstance(key, keyboard.KeyCode) and key.char:
                    # This handles all alphanumeric characters, including capitals and symbols.
                    with self.lock:
                        self.current_input_buffer.append(key.char)
                    print(key.char, end="", flush=True)
            except Exception:
                # Handle special keys that don't have a char attribute gracefully
                pass

        # The type hint for on_press in pynput is incorrect; it doesn't account for
        # returning False to stop the listener. We ignore the arg-type error here.
        with keyboard.Listener(on_press=on_press) as listener: # type: ignore[arg-type]
            listener.join()
        
        # Stop the monitor thread if it was started
        stop_monitor.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1) # Give it a moment to finish gracefully
        
        print() # Move to the next line after input is complete
        with self.lock:
            final_input = "".join(self.current_input_buffer)
        return final_input

    def _typing_monitor(self, stop_event: threading.Event):
        """
        Runs in a background thread while the user is typing.
        If they take too long, it prompts them with a generated response.
        """
        start_time = time.time()
        next_prompt_time = start_time + self.TYPING_PROMPT_THRESHOLD_SECONDS

        while not stop_event.is_set():
            now = time.time()
            if now >= next_prompt_time:
                # Using pre-canned responses is more reliable and faster than an LLM call for this.
                prompts = [
                    "Still there?",
                    "Lost in thought?",
                    "I'm waiting...",
                    "Take your time, but not all day.",
                    "Did you forget about me?"
                ]
                response = random.choice(prompts)

                with self.lock:
                    current_input = "".join(self.current_input_buffer)
                prompt_line = f"You: {current_input}"
                
                # Clear the current line of user input, print Selene's interjection,
                # and then reprint the user's line so they can continue.
                print("\r" + " " * len(prompt_line) + "\r", end="", flush=True)
                wrapped_response = textwrap.fill(response.strip(), width=self.CLI_WRAP_WIDTH)
                print(f"Selene: {wrapped_response}", flush=True)
                print(prompt_line, end="", flush=True)

                next_prompt_time = now + random.uniform(45, 120)
            
            time.sleep(1)

    def _autonomy_monitor(self):
        """
        Background thread that tracks idle time and creative energy.
        Autonomous activity (creative writing, Hermes Agent tools, etc.) is
        disabled here until the Hermes Agent integration is wired up.
        The energy gauge, is_writing_autonomously flag, and threshold constants
        are all preserved — this slot is ready to receive activities.
        """
        while self.is_running:
            # Placeholder — autonomous activities will be dispatched here once
            # Hermes Agent tool hooks are connected.
            time.sleep(10)

    def start_loop(self):
        self.is_running = True
        self.last_interaction_time = time.time()

        # Start her internal drive in a background thread
        autonomy_thread = threading.Thread(target=self._autonomy_monitor, daemon=True)
        autonomy_thread.start()
        print("\n[System]: Selene is now online. Type 'exit' to disconnect or '/new' for a new conversation.")

        while self.is_running:
            try:
                # Use the new custom input method to reset the idle timer on first keypress
                user_input = self._get_user_input()

                if user_input.lower() in ['exit', 'quit']:
                    self.is_running = False
                    break

                if user_input.lower() == '/new':
                    self.new_conversation()
                    print("\n[System]: New conversation started. History has been cleared.")
                    # Reset the idle timer to prevent immediate autonomous writing
                    self.last_interaction_time = time.time()
                    continue # Skip the rest of the loop and prompt for new input

                self.creative_energy = min(100, self.creative_energy + 10) # User interaction provides inspiration

                def get_final_response():
                    """Determines the agent's response, either from a tool or standard chat."""
                    triggered_tool_args = None
                    triggered_tool_name = None

                    # Generic, event-driven tool trigger loop
                    for tool in self.tool_router.tools.values():
                        if hasattr(tool, 'check_and_trigger'):
                            triggered_tool_args = tool.check_and_trigger(user_input)
                            if triggered_tool_args:
                                triggered_tool_name = tool.name
                                break # Use the first tool that triggers
                    
                    if triggered_tool_name and triggered_tool_args is not None:
                        print(f"[System: Keyword detected. Routing to {triggered_tool_name}...]")
                        tool_result = self.tool_router.route_and_execute(triggered_tool_name, triggered_tool_args)

                        if tool_result.get("status") == "success":
                            response = tool_result.get("data")
                            if response is None:
                                return ""
                            elif not isinstance(response, str):
                                return str(response)
                            return response
                        else:
                            return f"I tried to use the {triggered_tool_name} tool, but something went wrong: {tool_result.get('message')}"

                    # Standard chat response if no tool is triggered
                    return self.chat(user_input)

                final_response = self._animate_while_blocking(get_final_response, "Selene is thinking")
                final_response_str = final_response or ""
                
                # Centralized response handling and memory update
                self._streamed_print(final_response_str)
                # Reset idle timer again AFTER response to mark the end of the agent's activity.
                self.last_interaction_time = time.time()

                with self.lock:
                    ts = time.time()
                    self.working_memory.append({"role": "user", "content": user_input, "ts": ts}) # type: ignore
                    self.working_memory.append({"role": "assistant", "content": final_response_str, "ts": ts}) # type: ignore

                    # Enforce the specious present (trim memory to the defined window)
                    if len(self.working_memory) > self.memory_window * 2:
                        self.working_memory = self.working_memory[-(self.memory_window * 2):]

                # Background memory extraction — every EXTRACTION_CADENCE turns,
                # distils new facts from the exchange into user_profile.md / selene_notes.md
                self.maybe_extract_memory(user_input, final_response_str, reflective_turn=False)

            except (KeyboardInterrupt, EOFError):
                print("\n[System]: Disconnecting...")
                self.is_running = False
            except Exception as e:
                print(f"\n[System Error]: An unexpected error occurred: {e}")
                print("Please check your connection to the LM Studio server and ensure it is running.")

        # After the loop finishes, for any reason
        self.save_state()

def _normalize_model_name(name: str) -> str:
    """Removes common separators and converts to lowercase for consistent comparison."""
    return name.lower().replace(" ", "").replace("-", "").replace("_", "").replace(".", "").replace("/", "")

def main():
    """Main function to initialize and run the chat application."""
    # --- Smart Startup Sequence ---
    base_url = os.environ.get("LM_STUDIO_URL", "http://10.0.0.35:1234")
    manager = LMStudioManager(base_url=base_url)
    # Pull from env so this CLI path stays in sync with the server config.
    desired_model_identifier = os.environ.get("LM_STUDIO_MODEL", "google/gemma-3n-e4b")

    print("[System]: Checking LM Studio server status...")
    loaded_model = manager.get_loaded_model_info()
    
    active_model_path: Optional[str] = None

    # Normalize names for a more robust comparison that ignores spaces vs. hyphens.
    normalized_desired = _normalize_model_name(desired_model_identifier)
    loaded_model_path = loaded_model.get('path', '') if loaded_model else ''

    if loaded_model and normalized_desired in _normalize_model_name(loaded_model_path):
        print(f"[System]: Desired model '{loaded_model_path}' is already loaded.")
        active_model_path = loaded_model_path
    else:
        if loaded_model:
            print(f"[System]: A different model is loaded ('{loaded_model_path}').")
        elif loaded_model is None:
            print("[System]: Server is offline or no model is loaded.")
        
        print(f"[System]: Attempting to load model '{desired_model_identifier}'...")
        if manager.load_model(desired_model_identifier):
            print(f"[System]: Model '{desired_model_identifier}' loaded successfully.")
            active_model_path = desired_model_identifier
            time.sleep(5) # Give the server a moment to settle after loading.
        else:
            print(f"[System Error]: Failed to load model '{desired_model_identifier}'.")
            print("Please ensure the model identifier is correct and the LM Studio server is running.")
            return

    if not active_model_path:
        print("[System Error]: Could not determine an active model. Exiting.")
        return

    try:
        chat_app = LLMChat(base_url=base_url, model_name=active_model_path)
        chat_app.start_loop()
    except Exception as e:
        print(f"Failed to start the application. A critical error occurred: {e}")

if __name__ == "__main__":
    main()
