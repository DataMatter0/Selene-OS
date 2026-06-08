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

from . import PromptBuilderMixin, ConversationManagerMixin, MemoryExtractorMixin

load_dotenv()

# Get the directory of the current script's parent (the project root) to make file paths absolute
_BRAIN_DIR         = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_DIR        = os.path.dirname(_BRAIN_DIR) # project root
_STATE_FILE        = os.path.join(_SCRIPT_DIR, "selene_state.json")
_CONVERSATIONS_DIR = os.path.join(_SCRIPT_DIR, "conversations")
_SOUL_FILE          = os.path.join(_SCRIPT_DIR, "configs", "soul.md")
_TOOLS_CONTEXT_FILE = os.path.join(_SCRIPT_DIR, "configs", "tools_context.md")
_MEMORY_DIR         = os.path.join(_SCRIPT_DIR, "memories")

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
    TOOLS_CONTEXT_FILE  = _TOOLS_CONTEXT_FILE
    MEMORY_DIR          = _MEMORY_DIR

    def __init__(self, base_url: str, model_name: str, system_prompt: Optional[str] = None, memory_window: int = 5):
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

        # Pass base_url without suffix — LLMCaller appends /v1/chat/completions itself.
        self.llm_caller = LLMCaller(base_url=base_url, model_name=model_name, system_prompt="")
        self.tool_router = ToolRouter(llm_caller=self.llm_caller)

        register_all_tools(self, self.tool_router)

        # Setup Agent via hot-swapping Selene by default
        self._prompt_dirty = True
        self.swap_agent("selene")

        self.load_state()

    def swap_agent(self, agent_name: str) -> None:
        """
        Hot-swaps the active agent's configuration, memory database, system prompt, and allowed tools
        without reloading LM Studio.
        """
        with self.lock:
            agent_name = agent_name.lower().strip()
            if agent_name not in ("selene", "sage"):
                print(f"[LLMChat]: Unknown agent name '{agent_name}'. Defaulting to 'selene'.")
                agent_name = "selene"

            config_file = os.path.join(_SCRIPT_DIR, "configs", f"{agent_name}_config.json")
            if not os.path.exists(config_file):
                raise FileNotFoundError(f"Configuration file not found: {config_file}")

            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            self.active_agent_name = config["name"]
            self.active_agent_title = config["title"]
            self.active_agent_domain = config["domain"]
            self.allowed_tools = config["tools"]
            self.prompt_path = os.path.join(_SCRIPT_DIR, config["prompt_path"])
            self.notion_page_id = config["notion_page_id"]

            # Initialize isolated SQLite database
            db_path = os.path.join(_SCRIPT_DIR, config["memory_path"])
            
            # Close old database if open
            if hasattr(self, "db") and self.db:
                self.db.close()

            # Initialize AgentMemoryStore
            from .agent_memory import AgentMemoryStore
            self.db = AgentMemoryStore(db_path, is_readonly=False)

            # Set per-agent profile file paths.
            # character_profile_path is optional in config — defaults to agent-namespaced file.
            _mem = os.path.join(_SCRIPT_DIR, "memories")

            # prompt_path → the single identity file for this agent (selene_prompt.txt etc.)
            # soul.md / sage_soul.md are legacy orphans — not read here.
            self.USER_PROFILE_FILE = os.path.join(
                _mem, config.get("user_profile_path", f"{agent_name}_user_profile.md")
            )
            self.CHARACTER_PROFILE_FILE = os.path.join(
                _mem, config.get("character_profile_path", f"{agent_name}_character_profile.md")
            )

            # If Sage: open Selene's DB read-only so MetaInsight cross-agent queries work
            if agent_name == "sage":
                selene_db_path = os.path.join(_SCRIPT_DIR, "memories/selene_memory.db")
                self.selene_db = AgentMemoryStore(selene_db_path, is_readonly=True)
            else:
                self.selene_db = None

            # Setup model-agnostic emotion classifier
            from .mood_observer import EmotionClassifier
            self.emotion_classifier = EmotionClassifier(self.active_agent_name, self.llm_caller)

            # Load active uncertainty warnings
            self.active_uncertainty_warning = None

            # Refresh system prompt
            self._prompt_dirty = True
            self._refresh_system_prompt()
            print(f"[LLMChat]: Swapped agent to '{self.active_agent_name}' ({self.active_agent_title}) successfully.")

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

            choice_prompt = (
                f"You are the presence awareness for {self.active_agent_name} — The Voice, "
                f"an AI companion to Ghost. You are deciding, on {self.active_agent_name}'s behalf, "
                f"whether this moment calls for her voice.\n\n"
                f"{mood_line}\n\n"
                "Return exactly one self-closing XML tag — nothing else:\n"
                "  <presence_decision mode=\"respond\" />\n"
                "  <presence_decision mode=\"observe\" />\n"
                "  <presence_decision mode=\"ignore\" />\n\n"
                "respond  — Ghost is directly addressing Selene, asking something, or clearly wants engagement.\n"
                "observe  — Use this when:\n"
                "             • Ghost tells her to observe, go quiet, or stand by\n"
                "             • The message is a natural conversation ender (thanks, got it, ok)\n"
                "             • Ghost is narrating ambient activity (watching something, working, brb)\n"
                "             • More context is clearly incoming — observe acts as a continue gate\n"
                "             • Ghost is sharing media or watching YouTube and hasn't asked anything\n"
                "             • The message doesn't dignify a reply and would feel intrusive\n"
                "ignore   — The message is empty, spam, accidental, or pure noise.\n\n"
                "Examples:\n"
                "  'what do you think about this?' → respond\n"
                "  'observe' / 'go quiet'           → observe\n"
                "  'watching this video'             → observe\n"
                "  'brb' / 'ok thanks' / 'got it'   → observe\n"
                "  'this is interesting...'          → observe  (more context incoming)\n"
                "  'can you help me with X?'         → respond\n"
                "  'hey' / 'hello'                   → respond\n"
                "  '...' / (blank)                   → ignore\n\n"
                "RECENT CONVERSATION:\n"
                f"{history_ctx}\n\n"
                "When in doubt mid-conversation, lean toward respond. "
                "Only observe when the message clearly doesn't need her voice right now."
            )

            # Call LLM with low temperature — presence layer uses its own prompt only.
            # /no_think suppresses Qwen3's reasoning mode for this classification call —
            # it only needs to output a single XML tag, not a reasoning chain.
            try:
                raw_choice = self.llm_caller.call_llm(
                    input_data=f"/no_think\nNew message from Ghost: '{user_input}'",
                    system_prompt=choice_prompt,
                    temperature=0.0,
                    max_tokens=30,
                )
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
                if mode_match:
                    mode = mode_match.group(1).upper()
                    result = {
                        "gating": "RESPOND" if mode == "RESPOND" else mode,
                        "type": "CONVERSATIONAL",
                        "action": "CHAT" if mode == "RESPOND" else mode,
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
        Gathers all dialog history from the active agent's database for the current calendar day,
        filters out noise using a low-temperature review pass, and builds a daily manifest document.
        """
        with self.lock:
            # Get today's start timestamp
            import datetime
            today_str = datetime.date.today().isoformat()
            
            # Fetch all conversation rows from dialog_history
            cursor = self.db.conn.cursor()
            rows = cursor.execute("""
                SELECT timestamp, role, content, thoughts, status 
                FROM dialog_history 
                ORDER BY id ASC
            """).fetchall()
            
            dialogs = []
            for r in rows:
                dt = datetime.date.fromtimestamp(r["timestamp"])
                if dt.isoformat() == today_str:
                     dialogs.append(dict(r))
                     
            if not dialogs:
                return {"date": today_str, "status": "no_data", "summary": "No interactions recorded today."}
                 
            full_chat_log = ""
            for d in dialogs:
                full_chat_log += f"[{d['role'].upper()}]: {d['content']}\n"
                 
            # LLM noise filtering prompt
            review_prompt = (
                f"You are the Daily Manifest Compiler for {self.active_agent_name}. "
                "Your task is to review today's conversation log and separate meaningful learnings, decisions, and outcomes "
                "from noise (e.g. simple greetings, idle chatter, tests). "
                "Provide a structured, compressed manifest summarizing the day's active threads, emotional highlights, "
                "decisions made, and open action items.\n\n"
                "Respond in a clean Markdown format containing:\n"
                "## Daily Manifest Summary\n"
                "- [Summary of interactions]\n"
                "## Key Decisions & Insights\n"
                "- [Decisions]\n"
                "## Completed Actions\n"
                "- [Actions]\n"
                "## Pending Backlog / Tasks\n"
                "- [Backlog items]"
            )
            
            summary = self.llm_caller.call_llm(
                input_data=f"Today's Chat Log:\n{full_chat_log}",
                system_prompt=review_prompt,
                temperature=0.2,
                max_tokens=1024
            )
            summary = self._strip_tool_tags(summary)
            if "<think>" in summary:
                summary = re.sub(r'<think>[\s\S]*?</think>', '', summary).strip()
                 
            # Compile metadata
            metadata = {
                "agent": self.active_agent_name,
                "total_messages": len(dialogs),
                "compiled_at": time.time()
            }
             
            # Save to daily manifests table
            self.db.save_daily_manifest(
                date_str=today_str,
                metadata=metadata,
                emotions={},
                choices={},
                tasks={},
                threads={},
                summary=summary
            )
             
            return {
                "date": today_str,
                "status": "success",
                "summary": summary,
                "metadata": metadata
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
            
        # 3. Asynchronous Semantic Emotion Classification
        def run_emotion_pass():
            try:
                # Classify user thought/intent
                user_emo, user_int = self.emotion_classifier.classify_text(user_input, is_thought=True)
                # Classify assistant response
                asst_emo, asst_int = self.emotion_classifier.classify_text(final_reply, is_thought=False)
                
                # Log to emotional history
                self.db.log_emotion(
                    thought_emotion=user_emo,
                    thought_intensity=user_int,
                    response_emotion=asst_emo,
                    response_intensity=asst_int,
                    action_details=f"Turn processed for {self.active_agent_name}"
                )
            except Exception as e:
                print(f"[Emotion Classifier]: Async background thread failed: {e}")
                
        threading.Thread(target=run_emotion_pass, daemon=True).start()

        # 4. MetaInsight: log output entry (reasoning trace vs final text delta)
        try:
            # Ensure _conf is always defined even if confidence_score wasn't set above
            if raw_entropy is not None and not is_greeting:
                _conf = locals().get('confidence_score', max(0.0, min(1.0, 1.0 - (raw_entropy / 1.5))))
            else:
                _conf = 0.0
            _emotion_snap = {"energy": self.creative_energy, "status": "idle"}
            # Extract think block from thought_log for reasoning field
            _reasoning = thought_log[:3000] if thought_log else ""
            _final_clean = final_reply[:1500] if final_reply else ""
            self.db.log_meta_insight(
                agent=getattr(self, "active_agent_name", "selene").lower(),
                category="output",
                subcategory="chat_response",
                input_context=user_input[:500],
                reasoning=_reasoning,
                result=_final_clean,
                emotional_state_before=_emotion_snap,
                emotional_state_after=_emotion_snap,
                confidence_score=_conf,
                trigger_mode="llm",
                session_id=session_id,
            )
        except Exception:
            pass

    # ── State persistence (non-conversation) ──────────────────────────────────

    def load_state(self):
        """Loads persistent state (energy, focus, layout, active agent) but does NOT auto-resume any
        conversation.  Conversations are loaded only when explicitly selected.
        This keeps working_memory clean on boot so Selene isn't fed stale context
        from a previous session she didn't choose to re-enter."""
        self.dashboard_layout = {
            "left": "fused_manifest",
            "center": "main_chat",
            "right": "status_panel"
        }
        if os.path.exists(self.STATE_FILE):
            print(f"[System]: Found previous state at '{self.STATE_FILE}'. Loading...")
            try:
                with open(self.STATE_FILE, 'r') as f:
                    state = json.load(f)
                self.creative_energy = state.get("creative_energy", 100)
                
                # Load persistent dashboard layout
                self.dashboard_layout = state.get("dashboard_layout", {
                    "left": "fused_manifest",
                    "center": "main_chat",
                    "right": "status_panel"
                })
                
                # Auto-swap agent on boot if saved state differs
                saved_agent = state.get("active_agent", "selene").lower()
                if saved_agent in ("selene", "sage") and saved_agent != self.active_agent_name.lower():
                    self._prompt_dirty = True
                    self.swap_agent(saved_agent)

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
            "dashboard_layout":     self.dashboard_layout
        }
        try:
            with open(self.STATE_FILE, 'w') as f:
                json.dump(state, f, indent=4)
            print("[System]: State saved.")
        except IOError as e:
            print(f"[System Error]: Could not save state file. Error: {e}")

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
             disable_tools: bool = False, _suggestion_warning: str = ""):
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

            # Prepend dynamic context to the current user message only.
            # History messages stay clean — context is NOT injected into past turns.
            # _suggestion_warning is injected when ToolSuggestionLayer has low confidence.
            extra = _suggestion_warning if _suggestion_warning else ""
            current_input = f"{turn_context}{extra}{input_data}" if (turn_context or extra) else input_data
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
                self.maybe_extract_memory(user_input, final_response_str)

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
