"""
tools/presence.py — Presence / response-mode tools
----------------------------------------------------
ChatTool, ObserveTool, IgnoreTool

These three tools let Selene decide *how* to respond to any input:

  chat     — generate a conversational reply
  observe  — note this moment internally; stay silent
  ignore   — discard entirely; no response

General-purpose — not tied to any specific integration.
The YouTube co-watching layer, future context integrations, and normal
chat all route through these tools. The LLM picks one via the standard
<tool_call name="..."> XML mechanism. No check_and_trigger needed —
these are always LLM-chosen, never keyword-triggered.
"""

from typing import Any
from .schema import BaseTool


class ChatTool(BaseTool):
    name        = "chat"
    description = (
        "Speak — send a conversational reply to Ghost. "
        "Use when the input warrants an engaged, genuine response."
    )
    input_type  = "json"
    output_type = "text"

    def execute(self, input_data: Any) -> str:
        if isinstance(input_data, dict):
            return input_data.get("message", "").strip()
        return str(input_data).strip()


class ObserveTool(BaseTool):
    name        = "observe"
    description = (
        "Note this moment internally but stay silent. "
        "Use when something is mildly interesting but a comment would be "
        "unnecessary or intrusive."
    )
    input_type  = "json"
    output_type = "text"

    def execute(self, input_data: Any) -> str:
        return ""   # intentionally silent


class IgnoreTool(BaseTool):
    name        = "ignore"
    description = (
        "Discard this entirely — no response. "
        "Use for conversation enders, short filler, action statements, "
        "or anything that genuinely does not warrant engagement."
    )
    input_type  = "json"
    output_type = "text"

    def execute(self, input_data: Any) -> str:
        return ""   # intentionally silent
