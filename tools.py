from tool_schema import BaseTool, ToolRouter
from typing import Any, Dict, Optional, TYPE_CHECKING
import random

if TYPE_CHECKING:
    # This is a forward declaration to avoid circular imports.
    # It's only used for type hinting.
    from llm_chat import LLMChat


class ChronicleTool(BaseTool):
    """A tool for interacting with Selene's creative chronicles."""
    name = "chronicle_manager"
    description = "Manages creative chronicles. Use this to view the current work-in-progress, or to autonomously write when the user is idle."
    input_type = "json"
    output_type = "text"

    def __init__(self, agent_state: Any):
        """
        This tool needs access to the agent's state for its lock and LLM.
        :param agent_state: A reference to the LLMChat instance.
        """
        self.agent_state = agent_state
        self.creative_focus = "" # Her current writing project

    def check_and_trigger(self, user_input: str) -> Optional[Dict[str, Any]]:
        """Checks user input for keywords and returns tool arguments if triggered."""
        normalized_input = user_input.lower()

        # A list of phrases to detect the user's intent to ask about her work.
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
        """
        A single step in the autonomous creative writing process.
        Returns True if writing can continue, False otherwise.
        """
        with self.agent_state.lock:
            if self.agent_state.creative_energy <= 0:
                return False
            
            # Use the memory tool to get inspiration
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
            
            llm_caller = self.agent_state.llm_caller
            new_paragraph = llm_caller.call_llm(internal_prompt, system_prompt="I am a creative writer, continuing a story.")
            self.creative_focus += "\n\n" + new_paragraph
            self.agent_state.creative_energy -= 20 # Writing consumes energy
            
            return self.agent_state.creative_energy > 0

    def execute(self, input_data: Dict[str, Any]) -> str:
        """
        Executes a command for the chronicle.
        Expects input_data to be a dict like: {"command": "view_current"}
        """
        command = input_data.get("command")

        if command == "view_current":
            with self.agent_state.lock:
                chronicle_text = self.creative_focus
            
            if not chronicle_text:
                return "I haven't started a new chronicle yet. I'm waiting for inspiration."
            else:
                # The tool formulates a prompt for the LLM to generate a conversational summary.
                response_prompt = (
                    f"The user asked what I'm writing about. I need to give them a brief, conversational summary of my work. "
                    f"Here is the story so far: '{chronicle_text}'"
                )
                llm_caller = self.agent_state.llm_caller
                # Use a specific system prompt to guide the LLM's tone for this task.
                return llm_caller.call_llm(response_prompt, system_prompt="You are Selene. Describe your creative writing in your own words.")
        
        return f"Unknown command for chronicle_manager: {command}"

class MemoryTool(BaseTool):
    """Provides access to the agent's short-term working memory."""
    name = "memory_tool"
    description = "Accesses the agent's short-term working memory to get a summary or full history."
    input_type = "json"
    output_type = "any" # Can return a string (summary) or list (history)

    def __init__(self, agent_state: Any):
        self.agent_state = agent_state

    def execute(self, input_data: Dict[str, Any]) -> Any:
        command = input_data.get("command")

        if command == "get_summary":
            with self.agent_state.lock:
                # Get last 4 messages (2 user, 2 assistant)
                return " ".join([msg["content"] for msg in self.agent_state.working_memory[-4:]])
        elif command == "get_full_history":
             with self.agent_state.lock:
                return self.agent_state.working_memory
        
        return f"Unknown command for memory_tool: {command}"

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

def register_all_tools(agent_state: "LLMChat", tool_router: "ToolRouter"):
    """
    Instantiates and registers all available tools with the ToolRouter.
    This centralizes tool management outside of the main chat loop class.
    """
    chronicle_tool = ChronicleTool(agent_state=agent_state)
    memory_tool = MemoryTool(agent_state=agent_state)
    status_tool = StatusTool(agent_state=agent_state)
    tool_router.register_tool(chronicle_tool)
    tool_router.register_tool(memory_tool)
    tool_router.register_tool(status_tool)