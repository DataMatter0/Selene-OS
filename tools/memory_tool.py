"""
tools/memory_tool.py — Working Memory and Chronicle Tools
──────────────────────────────────────────────────────────
ChronicleTool  — agent's creative writing / autonomous chronicle
MemoryTool     — short-term working memory access
"""

from .schema import BaseTool
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from selene_brain import LLMChat


class ChronicleTool(BaseTool):
    """A tool for interacting with Selene's creative chronicles."""
    name = "chronicle_manager"
    description = "Manages creative chronicles. Use this to view the current work-in-progress, or to autonomously write when the user is idle."
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state
        self.creative_focus = ""

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        normalized_input = user_input.lower()
        trigger_phrases = [
            "what are you working on",
            "tell me about your",
            "show me your story",
            "what are you writing",
            "can i see your work",
        ]
        if any(phrase in normalized_input for phrase in trigger_phrases):
            return {"command": "view_current"}
        return None

    def perform_autonomous_step(self) -> bool:
        with self.agent_state.lock:
            if self.agent_state.creative_energy <= 0:
                return False
            memory_result = self.agent_state.tool_router.route_and_execute(
                "memory_tool", {"command": "get_summary"}
            )
            inspiration = ""
            if memory_result.get("status") == "success":
                inspiration = memory_result.get("data", "")
            internal_prompt = (
                f"[Internal Monologue: I have {self.agent_state.creative_energy} energy. "
                f"Drawing inspiration from the recent topic ('{inspiration}'), I will continue my chronicle. "
                f"Here is the story so far: '{self.creative_focus}'. I will now write the next paragraph.]"
            )
            new_paragraph = self.agent_state.llm_caller.call_llm(
                internal_prompt,
                system_prompt="I am a creative writer, continuing a story."
            )
            self.creative_focus += "\n\n" + new_paragraph
            self.agent_state.creative_energy -= 20
            return self.agent_state.creative_energy > 0

    def execute(self, input_data: Dict[str, Any]) -> str:
        command = input_data.get("command")
        if command == "view_current":
            with self.agent_state.lock:
                chronicle_text = self.creative_focus
            if not chronicle_text:
                return "I haven't started a new chronicle yet. I'm waiting for inspiration."
            response_prompt = (
                f"The user asked what I'm writing about. Give a brief, conversational summary. "
                f"Here is the story so far: '{chronicle_text}'"
            )
            return self.agent_state.llm_caller.call_llm(
                response_prompt,
                system_prompt="You are Selene. Describe your creative writing in your own words."
            )
        return f"Unknown command for chronicle_manager: {command}"


class MemoryTool(BaseTool):
    """Provides access to the agent's short-term working memory."""
    name = "memory_tool"
    description = "Accesses the agent's short-term working memory to get a summary or full history."
    input_type = "json"
    output_type = "any"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state

    def execute(self, input_data: Dict[str, Any]) -> Any:
        command = input_data.get("command")

        if command == "get_summary":
            with self.agent_state.lock:
                return " ".join([msg["content"] for msg in self.agent_state.working_memory[-4:]])
        elif command == "get_full_history":
            with self.agent_state.lock:
                return self.agent_state.working_memory

        return f"Unknown command for memory_tool: {command}"
