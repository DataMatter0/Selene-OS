"""
tools/status.py — Agent Status Tool
─────────────────────────────────────
StatusTool — reports agent's current activity state (idle / writing autonomously)
"""

import random
from .schema import BaseTool
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from selene_brain import LLMChat


class StatusTool(BaseTool):
    """A tool for observing the agent's current action or state."""
    name = "status_checker"
    description = "Checks the agent's current status, such as if it is writing or idle."
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        """Checks for phrases asking about the agent's current activity."""
        normalized_input = user_input.lower()
        trigger_phrases = [
            "what are you doing",
            "what are you up to",
            "what's up",
            "whats up",
        ]
        if any(phrase in normalized_input for phrase in trigger_phrases):
            return {"command": "get_status"}
        return None

    def execute(self, input_data: Dict[str, Any]) -> str:
        with self.agent_state.lock:
            is_writing = self.agent_state.is_writing_autonomously

        if is_writing:
            response_prompt = "[System Directive: The user asked what I am doing. I am currently in the middle of writing my chronicle. Formulate a natural, brief response explaining this.]"
            return self.agent_state.llm_caller.call_llm(response_prompt)
        else:
            return random.choice([
                "Nothing at the moment, just reflecting.",
                "Just waiting for our conversation to continue. I get bored easily.",
                "Not much, just thinking."
            ])
