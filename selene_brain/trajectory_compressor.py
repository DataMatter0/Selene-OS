# selene_brain/trajectory_compressor.py
"""
Trajectory Compressor for Selene OS.

Zero-dependency, standalone dialogue history compression that:
1. Translates between standard role/content formatting.
2. Identifies a compressible middle region, protecting crucial initial prompts and the latest working turns.
3. Automatically calculates token budgets (using character estimation or tiktoken if available).
4. Summarizes intermediate conversation turns using the active LLMCaller to preserve working state.
"""

import json
import logging
import os
import re
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger("trajectory_compressor")

class SeleneTrajectoryCompressor:
    def __init__(self, target_max_tokens: int = 15250, summary_target_tokens: int = 750):
        self.target_max_tokens = target_max_tokens
        self.summary_target_tokens = summary_target_tokens
        
        # Protected turns settings
        self.protect_first_n_turns = 3  # Protect system prompt, first user message, first assistant response
        self.protect_last_n_turns = 6   # Protect last 6 active working messages
        
        # Try to load tiktoken or transformers for accurate token counting
        self.tokenizer = None
        try:
            import tiktoken
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            try:
                from transformers import AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained("moonshotai/Kimi-K2-Thinking", trust_remote_code=True)
            except Exception:
                pass

    def count_tokens(self, text: str) -> int:
        """Count tokens in text using available tokenizer, falling back to char estimation."""
        if not text:
            return 0
        if self.tokenizer:
            try:
                # Tiktoken support
                if hasattr(self.tokenizer, "encode"):
                    return len(self.tokenizer.encode(text))
            except Exception:
                pass
        # Fallback to character estimate (approx. 4 characters per token)
        return len(text) // 4

    def count_history_tokens(self, history: List[Dict[str, str]]) -> int:
        """Count total tokens in dialogue history."""
        return sum(self.count_tokens(turn.get("content", "")) for turn in history)

    def compress_history(
        self, 
        history: List[Dict[str, str]], 
        llm_caller: Any
    ) -> List[Dict[str, str]]:
        """
        Compresses a dialogue history to fit within target token budget.
        
        Args:
            history: List of conversation turns {"role": ..., "content": ...}
            llm_caller: Active LLMCaller from Selene
            
        Returns:
            Compressed history list
        """
        total_turns = len(history)
        if total_turns <= (self.protect_first_n_turns + self.protect_last_n_turns):
            return history

        turn_tokens = [self.count_tokens(turn.get("content", "")) for turn in history]
        total_tokens = sum(turn_tokens)

        # Skip if already under budget
        if total_tokens <= self.target_max_tokens:
            return history

        # Find compressible middle region
        compress_start = self.protect_first_n_turns
        compress_end = total_turns - self.protect_last_n_turns

        if compress_start >= compress_end:
            return history

        # Calculate how many tokens need to be saved
        tokens_to_save = total_tokens - self.target_max_tokens
        target_tokens_to_compress = tokens_to_save + self.summary_target_tokens

        # Accumulate turns from compress_start until we meet target savings
        accumulated_tokens = 0
        compress_until = compress_start

        for i in range(compress_start, compress_end):
            accumulated_tokens += turn_tokens[i]
            compress_until = i + 1
            if accumulated_tokens >= target_tokens_to_compress:
                break

        # Extract content for summary
        turns_to_summarize = history[compress_start:compress_until]
        convo_parts = []
        for idx, turn in enumerate(turns_to_summarize):
            role = turn.get("role", "system").upper()
            content = turn.get("content", "")
            # Truncate very long turns in summary prompt to avoid overflow
            if len(content) > 3000:
                content = content[:1500] + "\n...[truncated]...\n" + content[-500:]
            convo_parts.append(f"[Turn {idx + 1} - {role}]:\n{content}")

        convo_text = "\n\n".join(convo_parts)

        # Summarize using the active LLMCaller
        logger.info(f"[Compaction]: Compressing {len(turns_to_summarize)} intermediate turns...")
        summary_prompt = (
            f"Summarize the following intermediate conversation segment concisely. "
            f"Write the summary from a neutral perspective describing what the assistant did, learned, and decided. "
            f"Include key facts, code highlights, files discussed, and conclusions.\n\n"
            f"--- CONVERSATION TO SUMMARIZE ---\n{convo_text}\n"
            f"---------------------------------\n\n"
            f"Write a concise, bulleted summary under 500 words. Start directly with the summary."
        )

        try:
            summary = llm_caller.call_llm(
                input_data=summary_prompt,
                system_prompt="You are a concise conversation compactor. Output only a factual summary. No preamble.",
                history=[],
                temperature=0.2,
                max_tokens=self.summary_target_tokens
            )
            # Scrub any raw reasoning/thought blocks from summary
            summary = summary.strip()
            if "<think>" in summary:
                summary = re.sub(r'<think>[\s\S]*?</think>', '', summary).strip()
            if not summary:
                summary = "[Intermediate conversation summarized to save context space]"
        except Exception as e:
            logger.error(f"[Compaction Error]: Failed to generate summary: {e}")
            summary = "[Intermediate conversation turns compressed to free context window space]"

        # Construct compressed history
        head = history[:compress_start]
        tail = history[compress_until:]
        
        # Use "user" role for the summary block, not "system".
        # A mid-conversation system message causes many instruction-tuned models to
        # partially reset their persona on the very next turn. A user-role summary
        # is treated as context the same way prior dialogue is.
        summary_block = {
            "role": "user",
            "content": f"[CONTEXT SUMMARY — earlier turns compressed to fit context window]\n{summary}"
        }

        logger.info(f"[Compaction]: Compacted {total_tokens} tokens down to {self.count_history_tokens(head + [summary_block] + tail)} tokens.")
        return head + [summary_block] + tail
