import json
import logging
from typing import Any

# Configure basic logging for step 5
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class BaseTool:
    """Base class for all tools to ensure a consistent I/O schema."""
    name: str = "base_tool"
    description: str = "A basic tool."
    input_type: str = "text" # Expected input type (e.g., 'text', 'json')
    output_type: str = "text" # Expected output type

    def execute(self, input_data: Any) -> Any:
        """The core logic of the tool goes here."""
        raise NotImplementedError("Each tool must implement its own execute method.")

class ToolRouter:
    """Routes inputs to the appropriate tools, handling types and edge cases."""
    def __init__(self, llm_caller=None):
        self.llm_caller = llm_caller
        self.tools = {}

    def register_tool(self, tool: BaseTool):
        self.tools[tool.name] = tool
        logging.info(f"Registered tool: {tool.name}")

    def route_and_execute(self, tool_name: str, input_data: Any):
        logging.info(f"Attempting to route to: {tool_name}")
        
        if tool_name not in self.tools:
            error_msg = f"Tool '{tool_name}' not found."
            logging.error(error_msg)
            return {"status": "error", "message": error_msg}

        tool = self.tools[tool_name]
        
        # Handle I/O type parsing (e.g., string to JSON)
        if tool.input_type == "json" and isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                logging.error("Failed to parse input as JSON.")
                return {"status": "error", "message": "Expected JSON string, got plain text."}

        # Execute and wrap for edge cases
        try:
            result = tool.execute(input_data)
            logging.info(f"Execution successful. Output type: {tool.output_type}")
            return {"status": "success", "data": result, "type": tool.output_type}
        except Exception as e:
            logging.error(f"Error during execution of '{tool.name}': {str(e)}")
            return {"status": "error", "message": str(e)}
