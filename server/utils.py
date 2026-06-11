"""
server/utils.py — Stateless text-processing helpers
─────────────────────────────────────────────────────
Owns:
  clean_xml_tags()         — strips XML from model output, preserving <think> and
                             <tool_reasoning> blocks
  split_response_chunks()  — splits Selene's response into conversational chunks
  _format_tool_data()      — converts raw tool result data to model-readable string
  extract_presence_decision() — detects silent presence mode in model output
"""

import json
import re
import random
from typing import Any, Optional


def clean_xml_tags(text: str) -> str:
    if not text:
        return ""
    # Preserve <think>...</think> intact — ThoughtBubble in ChatView parses it.
    # tool_reasoning — training data block shown as ThoughtBubble in UI
    # think — legacy reasoning block (backward compat)
    tool_block = ""
    tool_match = re.search(r'<tool_reasoning[^>]*>[\s\S]*?</tool_reasoning>', text, flags=re.IGNORECASE)
    if tool_match:
        tool_block = tool_match.group(0)
        text = text[:tool_match.start()] + "\x00TOOL\x00" + text[tool_match.end():]

    think_block = ""
    think_match = re.search(r'<think>[\s\S]*?</think>', text, flags=re.IGNORECASE)
    if think_match:
        think_block = think_match.group(0)
        text = text[:think_match.start()] + "\x00THINK\x00" + text[think_match.end():]

    # Strip all other XML tags/blocks
    cleaned = re.sub(r'<(?!think\b)([a-zA-Z0-9_\-]+)[^>]*>([\s\S]*?)</\1>', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+/>', '', cleaned)
    cleaned = re.sub(r'<(?!/?think\b)[^>]+>', '', cleaned)
    cleaned = re.sub(r'</?think>', '', cleaned, flags=re.IGNORECASE)

    # Restore preserved blocks
    cleaned = cleaned.replace("\x00TOOL\x00", tool_block)
    cleaned = cleaned.replace("\x00THINK\x00", think_block)

    return cleaned.strip()


def split_response_chunks(text: str) -> list:
    """
    Splits Selene's response into conversational message chunks.

    Groups sentences into chunks of 2-4 sentences each (random per chunk)
    so the delivery feels like natural typed thought rather than one wall
    or one-sentence-at-a-time staccato.

    Sage does NOT use this — she sends one complete structured response.
    """
    if not text:
        return []

    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', text) if s.strip()]

    if len(sentences) <= 2:
        return [text.strip()]

    chunks: list = []
    i = 0
    while i < len(sentences):
        group_size = random.randint(2, 4)
        group = sentences[i:i + group_size]
        chunks.append(" ".join(group))
        i += group_size

    return [c for c in chunks if c.strip()]


def _format_tool_data(data: Any) -> str:
    """
    Convert raw tool result data to a clean, model-readable string.
    Avoids Python repr (single-quoted dicts/lists) by using JSON or
    structured text for list-of-dict results like meta_insight queries.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        if not data:
            return "No results found."
        if isinstance(data[0], dict):
            lines = []
            for i, entry in enumerate(data, 1):
                parts = [f"[{i}]"]
                for k, v in entry.items():
                    if k in ("id",):
                        continue
                    if isinstance(v, dict):
                        v = json.dumps(v)
                    parts.append(f"  {k}: {v}")
                lines.append("\n".join(parts))
            return "\n\n".join(lines)
        return json.dumps(data, ensure_ascii=False)
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, indent=2)
    return str(data)


def extract_presence_decision(text: str) -> Optional[str]:
    """Return observe/ignore when the model chose a silent presence tool."""
    if not text:
        return None
    match = re.search(r'<presence_decision\b[^>]*\bmode=["\']?(observe|ignore)["\']?', text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None
