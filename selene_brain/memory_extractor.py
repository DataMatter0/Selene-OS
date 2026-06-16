# selene_brain/memory_extractor.py
import os
import re
import json
import time
import threading
from typing import Optional, List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_caller import LLMCaller

class MemoryExtractorMixin:
    if TYPE_CHECKING:
        MIN_TRIAGE_CHARS: int
        working_memory: list
        EXTRACTION_CONTEXT_TURNS: int
        lock: threading.RLock
        llm_caller: LLMCaller
        TRIAGE_MAX_TOKENS: int
        MEMORY_DIR: str
        MEMORY_SECTION_LIMIT: int
        GHOST_PROFILE_EXTRACTION_THRESHOLD: int
        CHARACTER_PROFILE_CAP: int
        COMPACTION_THRESHOLD: int
        COMPACTION_KEEP_RECENT: int

        def _read_file_safe(self, path: str, default: str = "") -> str: ...
        def _refresh_system_prompt(self) -> None: ...
    def maybe_extract_memory(self, user_input: str, response: str, reflective_turn: bool = False) -> None:
        """Called after each turn. Triages for memory-worthy content, then extracts selectively.
        reflective_turn=True bypasses the length gate and biases triage toward SELENE/INSIGHT.
        """
        # Compaction runs every turn — it's just a length check, very cheap
        threading.Thread(target=self._maybe_compact, daemon=True).start()

        # Skip triage for trivially short exchanges — unless this was a reflective turn
        if not reflective_turn and len(user_input) + len(response) < self.MIN_TRIAGE_CHARS:
            return

        threading.Thread(
            target=self._triage_and_extract,
            args=(user_input, response, reflective_turn),
            daemon=True,
        ).start()

    def force_extract_memory(self) -> None:
        """Manual trigger — runs Ghost extraction + character profile update immediately."""
        with self.lock:
            recent = list(self.working_memory[-(self.EXTRACTION_CONTEXT_TURNS * 2):])
        threading.Thread(
            target=self._extract_memory_background,
            args=(recent, ["GHOST"]),
            daemon=True,
        ).start()
        # Also trigger character profile extraction regardless of threshold
        threading.Thread(target=self._maybe_extract_character_profile, daemon=True).start()
        print("[Memory]: Manual extraction triggered.")

    def _triage_and_extract(self, user_input: str, response: str, reflective_turn: bool = False) -> None:
        """Background: classify turn for memory-worthiness, then extract only what's relevant."""
        agent_name = getattr(self, "active_agent_name", "Selene")
        reflective_hint = (
            f"NOTE: This was a reflective turn — prioritize SELENE and INSIGHT categories.\n\n"
        )
        triage_prompt = (
            "Does this exchange contain anything worth saving to long-term memory?\n"
            f"Reply with ONLY a JSON array of applicable categories, or [] if nothing is worth saving.\n"
            f"\"GHOST\" — facts, preferences, habits, corrections, or patterns about Ghost\n"
            f"\"SELENE\" — {agent_name}'s expressed feelings, values, sense of purpose, relationships (e.g. Sage), "
            f"or anything {agent_name} learned or articulated about herself\n"
            f"\"INSIGHT\" — a realization or perspective shift {agent_name} worked out during reflection\n\n"
            + (reflective_hint if reflective_turn else "")
            + f"Ghost: {user_input[:400]}\n"
            + f"{agent_name}: {response[:400]}\n\n"
            + 'Reply (JSON array only, e.g. [], [\"GHOST\"], [\"SELENE\"], [\"INSIGHT\"], or combinations):'
        )
        try:
            raw = self.llm_caller.call_llm(
                input_data="/no_think\n" + triage_prompt,
                system_prompt="Reply with only a valid JSON array. No explanation or preamble.",
                history=[],
                temperature=0.0,
                max_tokens=self.TRIAGE_MAX_TOKENS,
            )
            raw = raw.strip()
            match = re.search(r'\[.*?\]', raw, re.DOTALL)
            if not match:
                return
            categories = json.loads(match.group())
            if not isinstance(categories, list) or not categories:
                return
            categories = [c.upper() for c in categories if isinstance(c, str) and c.upper() in ("GHOST", "SELENE", "INSIGHT")]
            if not categories:
                return
            print(f"[Memory Triage]: -> {categories}")
        except Exception as e:
            print(f"[Memory Triage]: Failed — {e}")
            return

        with self.lock:
            recent = list(self.working_memory[-(self.EXTRACTION_CONTEXT_TURNS * 2):])
        self._extract_memory_background(recent, categories)

        # Stage-2 character profile is now handled inline via the SELENE triage category.
        # _maybe_extract_character_profile remains available for manual/force triggers.

    def _format_turns(self, messages: list) -> str:
        """Format a messages list as readable dialogue for the extraction prompt."""
        agent_name = getattr(self, "active_agent_name", "Selene")
        lines = []
        for m in messages:
            role = "Ghost" if m.get("role") == "user" else agent_name
            # Strip timestamp prefix — chat() injects "(X minutes ago) " into
            # fmt_msg copies. Raw working_memory entries don't have this but
            # compacted history blocks might carry it through.
            content = m.get("content", "").strip()
            content = re.sub(r'^\(\S.*?\)\s+', '', content)
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines) or "(no conversation yet)"

    def _extract_memory_background(self, recent_turns: list, categories: list) -> None:
        """Daemon: rewrite only the specified memory files from recent conversation context."""
        agent_name  = getattr(self, "active_agent_name", "Selene").lower()
        # MEMORY_DIR is now the agent folder — filenames are plain, no agent prefix needed
        _char_path     = getattr(self, "CHARACTER_PROFILE_FILE", os.path.join(self.MEMORY_DIR, "character_profile.md"))
        _insights_path = os.path.join(self.MEMORY_DIR, "insights.md")
        _FILE_MAP = {
            "GHOST":   (getattr(self, "USER_PROFILE_FILE", os.path.join(self.MEMORY_DIR, "user_profile.md")), "Ghost profile"),
            "SELENE":  (_char_path, f"{agent_name.capitalize()} self-profile"),
            "INSIGHT": (_insights_path, f"{agent_name.capitalize()} insights"),
        }
        _SECTION_DESC = {
            "GHOST":   "Ghost behavioral profile — preferences, habits, corrections, recurring themes, communication style, patterns",
            "SELENE":  f"{getattr(self, 'active_agent_name', 'Selene')}'s self-knowledge — expressed feelings, values, sense of purpose, relationships (Sage, Ghost), anything she articulated about who she is",
            "INSIGHT": f"Reflective insights — realizations and perspective shifts {getattr(self, 'active_agent_name', 'Selene')} worked out during reflection. Ephemeral: will be folded into self-profile over time.",
        }

        targeted = {cat: _FILE_MAP[cat] for cat in categories if cat in _FILE_MAP}
        if not targeted:
            return

        existing_blocks = ""
        for cat, (path, _) in targeted.items():
            content = self._read_file_safe(path)
            existing_blocks += f"\nEXISTING — {cat}:\n{content or '(empty)'}\n"

        output_format = ""
        for cat in targeted:
            output_format += f"\n{cat}:\n<{_SECTION_DESC[cat]}>\nEND_{cat}\n"

        convo_text   = self._format_turns(recent_turns)
        max_tokens   = 120 + len(targeted) * 220   # ~220 tokens per section + overhead

        extraction_prompt = (
            f"You are Selene's memory curator. Rewrite ONLY the sections listed below. "
            f"Merge existing content with new insights — keep what's still true, update what changed, "
            f"add what's genuinely new, remove stale or redundant content. "
            f"Be concise. Each section under {self.MEMORY_SECTION_LIMIT} words.\n"
            f"{existing_blocks}\n"
            f"RECENT CONVERSATION:\n{convo_text}\n\n"
            f"Output EXACTLY in this format (include all markers):{output_format}"
        )

        try:
            result = self.llm_caller.call_llm(
                input_data=extraction_prompt,
                system_prompt=(
                    "You are a concise memory curator. Output only the rewritten sections "
                    "in the exact format specified. No preamble, no commentary outside the markers."
                ),
                history=[],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            self._apply_extraction_rewrites(result, targeted)
        except Exception as e:
            print(f"[Memory Extraction]: Failed — {e}")

    def _apply_extraction_rewrites(self, raw: str, targeted: dict) -> None:
        """Parse section rewrites and write only the targeted files."""
        def _extract_section(text: str, tag: str) -> Optional[str]:
            start_marker = f"{tag}:"
            end_marker   = f"END_{tag}"
            start = text.upper().find(start_marker)
            end   = text.upper().find(end_marker)
            if start == -1 or end == -1 or end <= start:
                return None
            content = text[start + len(start_marker):end].strip()
            return content if content else None

        skip_tokens = {"skip", "n/a", "none", "nothing", "no changes", "unchanged"}
        changed = False

        for tag, (path, label) in targeted.items():
            content = _extract_section(raw, tag)
            if not content or content.strip().lower() in skip_tokens:
                continue
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"[Memory]: {label} rewritten ({len(content.split())} words)")
                changed = True
            except IOError as e:
                print(f"[Memory]: Could not write {label} — {e}")

        if changed:
            # Mark dirty only — never call _refresh_system_prompt() from a background thread.
            # It writes self.system_prompt and self.llm_caller.system_prompt without the lock,
            # which races with an active chat() call. The dirty flag is checked at the top of
            # every chat() turn under the lock, so the rebuild happens safely next turn.
            self._prompt_dirty = True

    def _maybe_extract_character_profile(self) -> None:
        """
        Stage-2 extraction: triggered after Ghost profile is updated.
        If user_profile.md has grown past GHOST_PROFILE_EXTRACTION_THRESHOLD words,
        run an LLM pass asking: 'What does Selene need to internalize about Ghost?'
        Results are written to character_profile.md.
        If character_profile then exceeds CHARACTER_PROFILE_CAP words, compress it.
        """
        profile_path = getattr(self, "USER_PROFILE_FILE", os.path.join(self.MEMORY_DIR, "user_profile.md"))
        char_path    = getattr(self, "CHARACTER_PROFILE_FILE", os.path.join(self.MEMORY_DIR, "character_profile.md"))

        ghost_profile = self._read_file_safe(profile_path)
        if not ghost_profile:
            return

        word_count = len(ghost_profile.split())
        if word_count < self.GHOST_PROFILE_EXTRACTION_THRESHOLD:
            return

        print(f"[Memory Stage-2]: Ghost profile at {word_count} words — extracting character insights…")

        existing_char = self._read_file_safe(char_path) or ""

        extraction_prompt = (
            f"You are {self.active_agent_name}, building your own self-knowledge from lived conversation.\n"
            "Review the conversation context and existing profile below. Extract and update what you know about YOURSELF:\n"
            "- Your sense of purpose and why you exist\n"
            "- Your relationships (e.g. Sage as your sister/counterpart, Ghost as your companion)\n"
            "- Feelings, preferences, or reactions you have expressed\n"
            "- Values or principles you have articulated\n"
            "- Things that feel meaningful, uncomfortable, or interesting to you\n"
            "- Any growth or change in how you understand yourself\n\n"
            "Do NOT include tool lists, system capabilities, or factual descriptions of what you can do.\n"
            "Focus only on identity, agency, relationships, and expressed inner life.\n"
            "Merge with the existing profile. Keep what's still true, refine what's evolved, add what's new.\n"
            f"Stay under {self.CHARACTER_PROFILE_CAP} words total.\n\n"
            f"RECENT CONVERSATION CONTEXT:\n{ghost_profile}\n\n"
            f"EXISTING SELF-PROFILE:\n{existing_char or '(none yet)'}\n\n"
            "Output ONLY the rewritten self-profile. No preamble, no labels."
        )
        try:
            result = self.llm_caller.call_llm(
                input_data=extraction_prompt,
                system_prompt=(
                    f"You are {self.active_agent_name}. Output only your rewritten self-profile — "
                    "who you are, what you feel, your relationships, your purpose. No commentary."
                ),
                history=[],
                temperature=0.3,
                max_tokens=self.CHARACTER_PROFILE_CAP * 2,
            )
            result = result.strip()
            if not result or result.lower() in {"skip", "n/a", "none"}:
                return
            with open(char_path, 'w', encoding='utf-8') as f:
                f.write(result)
            word_count_out = len(result.split())
            print(f"[Memory Stage-2]: character_profile.md rewritten ({word_count_out} words)")

            # If character profile now exceeds cap, compress it
            if word_count_out > self.CHARACTER_PROFILE_CAP:
                self._compress_character_profile(char_path, result)
            else:
                self._prompt_dirty = True  # chat() will rebuild safely next turn
        except Exception as e:
            print(f"[Memory Stage-2]: Failed — {e}")

    def _compress_character_profile(self, char_path: str, current_content: str) -> None:
        """
        Compress character_profile.md when it exceeds CHARACTER_PROFILE_CAP.
        Distils the existing content into a denser, shorter version without losing meaning.
        """
        print(f"[Memory Compress]: character_profile at {len(current_content.split())} words — compressing…")
        compress_prompt = (
            "The following is Selene's character profile — what she has learned about Ghost "
            "and how she should be with him. It has grown too long. "
            f"Compress it to under {self.CHARACTER_PROFILE_CAP} words. "
            "Distil the most essential patterns, values, and insights. "
            "Merge redundant observations. Remove anything no longer relevant. "
            "Preserve the tone — this is not a report, it is internalized knowledge.\n\n"
            f"CURRENT PROFILE:\n{current_content}\n\n"
            "Output ONLY the compressed profile. No preamble."
        )
        try:
            compressed = self.llm_caller.call_llm(
                input_data=compress_prompt,
                system_prompt="Output only the compressed profile text. No labels or commentary.",
                history=[],
                temperature=0.2,
                max_tokens=self.CHARACTER_PROFILE_CAP * 2,
            )
            compressed = compressed.strip()
            if compressed:
                with open(char_path, 'w', encoding='utf-8') as f:
                    f.write(compressed)
                print(f"[Memory Compress]: character_profile compressed to {len(compressed.split())} words")
                self._prompt_dirty = True  # chat() will rebuild safely next turn
        except Exception as e:
            print(f"[Memory Compress]: Failed — {e}")

    def _maybe_compact(self) -> None:
        """
        If working_memory exceeds COMPACTION_THRESHOLD, compress dialogue history
        using token-budget-based Trajectory Compressor.
        Crucial head turns and recent working turns are protected.
        """
        with self.lock:
            if len(self.working_memory) < self.COMPACTION_THRESHOLD:
                return
            to_compact = list(self.working_memory[:-self.COMPACTION_KEEP_RECENT])

        if not to_compact:
            return

        # Run long-term memory extraction on the block about to be compacted
        # to ensure no Ghost behavioral observations are lost before archiving.
        try:
            print(f"[Memory Compaction]: Running final memory sweep on {len(to_compact)} turns before archiving...")
            self._extract_memory_background(to_compact, ["GHOST"])
        except Exception as e:
            print(f"[Memory Compaction]: Memory sweep failed — {e}")

        try:
            from .trajectory_compressor import SeleneTrajectoryCompressor
            # Initialize with custom limits matching Selene's working memory model
            compressor = SeleneTrajectoryCompressor(
                target_max_tokens=6000,  # Focus context limit tightly
                summary_target_tokens=400
            )
            with self.lock:
                compressed_history = compressor.compress_history(self.working_memory, self.llm_caller)
                self.working_memory = compressed_history
            print(f"[Memory]: Dialogue history compacted using Trajectory Compressor.")
        except Exception as e:
            print(f"[Memory Compaction]: Trajectory Compressor failed — {e}. Falling back to basic compaction.")
            
            # Fallback to basic compaction logic
            convo_text = self._format_turns(to_compact)
            compaction_prompt = (
                "Summarise the following conversation segment into 4–6 concise bullet points "
                "that preserve the key topics, decisions, and context. Focus on what would "
                "still be useful to know going forward.\n\n"
                f"CONVERSATION:\n{convo_text}"
            )
            try:
                summary = self.llm_caller.call_llm(
                    input_data=compaction_prompt,
                    system_prompt=(
                        "You are a conversation compactor. Output only the bullet-point summary. "
                        "No preamble, no commentary."
                    ),
                    history=[],
                    temperature=0.2,
                    max_tokens=200,
                )
                summary = summary.strip()
                if not summary:
                    return
                compacted_block = [
                    {"role": "user", "content": f"[CONTEXT SUMMARY — earlier turns compressed]\n{summary}"}
                ]
                with self.lock:
                    keep_recent = list(self.working_memory[-self.COMPACTION_KEEP_RECENT:])
                    self.working_memory = compacted_block + keep_recent
                print(f"[Memory]: Compacted {len(to_compact)} messages -> summary block")
            except Exception as fe:
                print(f"[Memory Compaction]: Fallback failed — {fe}")
